# -*- coding: utf-8 -*-
"""队列压测：用假 claude（stub_claude.py）跑满整条队列，不花 token。

跑法：
    python tests/bench_queue.py --n 60 --workers 1 --cwd-mode same
    python tests/bench_queue.py --n 60 --workers 4 --cwd-mode distinct

指标：
- submit 延迟：HTTP POST /api/task 的往返时间
- 排队等待：started_at - created_at（队列真实积压）
- 执行耗时：duration_ms
- 端到端：finished_at - created_at
- 吞吐：完成任务数 / 墙钟时间
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "stub_claude.py"
OUT_DIR = ROOT / "docs" / "bench"
OUT_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "bridge"))


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1)))))
    return vals[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cwd-mode", choices=["same", "distinct"], default="same")
    ap.add_argument("--sleep", type=float, default=0.2, help="stub 每次模拟耗时（秒）")
    ap.add_argument("--port", type=int, default=3096)
    ap.add_argument("--fail", action="store_true", help="让 10%% 任务失败，测错误路径")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="qqbench-"))
    db = tmp / "bench.db"
    cfg = json.loads((ROOT / "bridge" / "config.json").read_text(encoding="utf-8"))
    cfg.update({"instance_lock_port": 0,
        "napcat_ws": "ws://127.0.0.1:1",          # 故意连不上：本次只压队列，不需要 QQ
        "log_dir": str(tmp),
        "db_path": str(db),
        "panel_port": args.port,
        "workers": args.workers,
        "max_queue": 500,
        "owner_uin": 0,
        "claude_cmd": [sys.executable, str(STUB)],
        "task_timeout_minutes": 2,
    })
    cfg_path = tmp / "bench-config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ, QQ_REMOTE_CONFIG=str(cfg_path), QQ_REMOTE_DB=str(db),
               QQ_REMOTE_STATE=str(tmp / "state.json"),
               STUB_SLEEP=str(args.sleep), PYTHONUTF8="1")
    if args.fail:
        env["STUB_FAIL"] = "1"

    proc = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                            cwd=str(ROOT / "bridge"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{args.port}"
    token = cfg["panel_token"]
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/state", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)

        cwd_pool = [str(tmp / f"w{i}") for i in range(max(1, args.workers))]
        for d in cwd_pool:
            Path(d).mkdir(exist_ok=True)

        def submit(i):
            body = {"text": f"bench task {i}"}
            if args.cwd_mode == "distinct":
                body["cwd"] = cwd_pool[i % len(cwd_pool)]
            req = urllib.request.Request(
                base + "/api/task", data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json", "X-Panel-Token": token})
            t0 = time.perf_counter()
            urllib.request.urlopen(req, timeout=30).read()
            return time.perf_counter() - t0

        t_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=16) as ex:
            submit_lat = list(ex.map(submit, range(args.n)))

        # 等全部跑完
        import sqlite3
        deadline = time.time() + 600
        while time.time() < deadline:
            c = sqlite3.connect(db)
            rows = c.execute("SELECT state, created_at, started_at, finished_at, duration_ms"
                             " FROM tasks").fetchall()
            c.close()
            done = [r for r in rows if r[0] in ("succeeded", "failed", "cancelled", "interrupted")]
            if len(done) >= args.n:
                break
            time.sleep(0.2)
        wall = time.perf_counter() - t_start

        waits = [(r[2] - r[1]) * 1000 for r in done if r[2]]
        durs = [r[4] for r in done if r[4] is not None]
        e2e = [(r[3] - r[1]) * 1000 for r in done if r[3]]
        failed = sum(1 for r in done if r[0] == "failed")

        result = {
            "n": args.n, "workers": args.workers, "cwd_mode": args.cwd_mode,
            "stub_sleep_s": args.sleep,
            "wall_s": round(wall, 2),
            "throughput_per_s": round(len(done) / wall, 2) if wall else 0,
            "submit_ms": {"p50": round(pct(submit_lat, 50) * 1000, 1),
                          "p95": round(pct(submit_lat, 95) * 1000, 1)},
            "queue_wait_ms": {"p50": round(pct(waits, 50)), "p95": round(pct(waits, 95)),
                              "max": round(max(waits) if waits else 0)},
            "exec_ms": {"mean": round(statistics.mean(durs)) if durs else 0,
                        "p95": round(pct(durs, 95))},
            "e2e_ms": {"p50": round(pct(e2e, 50)), "p95": round(pct(e2e, 95))},
            "failed": failed,
            "completed": len(done),
        }
        name = f"w{args.workers}-{args.cwd_mode}-n{args.n}"
        (OUT_DIR / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
