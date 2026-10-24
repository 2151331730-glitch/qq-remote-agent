# -*- coding: utf-8 -*-
"""长时压测：持续提交任务，每 10 秒采样内存/队列/完成数，最后出曲线数据。

跑法：python tests/soak_test.py --minutes 10 --rate 2 --workers 2
不消耗模型额度（用 stub_claude.py 假 CLI）。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "stub_claude.py"
OUT = ROOT / "docs" / "soak"
OUT.mkdir(parents=True, exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--rate", type=float, default=2.0, help="每秒提交任务数")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--port", type=int, default=3092)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="qqsoak-"))
    db = tmp / "soak.db"
    cfg = json.loads((ROOT / "bridge" / "config.example.json").read_text(encoding="utf-8"))
    cfg.update({
        "instance_lock_port": 0,
        "napcat_ws": "ws://127.0.0.1:1",
        "log_dir": str(tmp),
        "db_path": str(db),
        "panel_port": args.port,
        "panel_token": "soak-token",
        "workers": args.workers,
        "max_queue": 2000,
        "max_tasks_per_hour": 0,
        "max_tokens_per_day": 0,
        "owner_uin": 0,
        "workdir": str(tmp),
        "claude_cmd": [sys.executable, str(STUB)],
    })
    cfg_path = tmp / "cfg.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    env = dict(os.environ, QQ_REMOTE_CONFIG=str(cfg_path), QQ_REMOTE_DB=str(db),
               QQ_REMOTE_STATE=str(tmp / "state.json"), STUB_SLEEP="0.4",
               PYTHONUTF8="1")

    proc = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                            cwd=str(ROOT / "bridge"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{args.port}"
    token = cfg["panel_token"]

    def submit(i):
        body = json.dumps({"text": f"soak {i}"}).encode()
        req = urllib.request.Request(base + "/api/task", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Panel-Token": token})
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception:
            return False

    samples = []
    sent = [0]
    stop = threading.Event()

    def sender():
        interval = 1.0 / args.rate
        nxt = time.perf_counter()
        i = 0
        while not stop.is_set():
            if submit(i):
                sent[0] += 1
            i += 1
            nxt += interval
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/state", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)

    th = threading.Thread(target=sender, daemon=True)
    th.start()

    ps = psutil.Process(proc.pid)
    t0 = time.time()
    end = t0 + args.minutes * 60
    try:
        while time.time() < end:
            time.sleep(10)
            try:
                rss = ps.memory_info().rss / 1048576
                cpu = ps.cpu_percent(interval=None)
            except Exception:
                rss, cpu = 0, 0
            c = sqlite3.connect(db)
            done = c.execute("SELECT COUNT(*) FROM tasks WHERE state IN"
                             " ('succeeded','failed','cancelled')").fetchone()[0]
            failed = c.execute("SELECT COUNT(*) FROM tasks WHERE state='failed'").fetchone()[0]
            c.close()
            try:
                st = json.loads(urllib.request.urlopen(base + "/api/state", timeout=3).read())
                q = st.get("queue", 0)
                p95 = st.get("p95_ms", 0)
            except Exception:
                q, p95 = -1, 0
            samples.append({"t": round(time.time() - t0), "rss_mb": round(rss, 1),
                            "cpu": cpu, "queue": q, "done": done, "failed": failed,
                            "p95_ms": p95, "sent": sent[0]})
            print(f"[{samples[-1]['t']:>4}s] rss={rss:6.1f}MB cpu={cpu:4.1f}% "
                  f"queue={q:3d} done={done:4d} failed={failed:2d} p95={p95}ms", flush=True)
    finally:
        stop.set()
        th.join(timeout=5)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    rss_vals = [s["rss_mb"] for s in samples]
    done_final = samples[-1]["done"] if samples else 0
    result = {
        "minutes": args.minutes, "rate": args.rate, "workers": args.workers,
        "submitted": sent[0], "completed": done_final,
        "failed": samples[-1]["failed"] if samples else 0,
        "throughput_per_s": round(done_final / (args.minutes * 60), 2),
        "rss_start_mb": rss_vals[0] if rss_vals else 0,
        "rss_end_mb": rss_vals[-1] if rss_vals else 0,
        "rss_peak_mb": max(rss_vals) if rss_vals else 0,
        "rss_growth_mb": round((rss_vals[-1] - rss_vals[0]), 1) if len(rss_vals) > 1 else 0,
        "max_queue": max((s["queue"] for s in samples), default=0),
        "samples": samples,
    }
    name = f"soak-{int(args.minutes)}min-{args.rate}rps-w{args.workers}"
    (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
