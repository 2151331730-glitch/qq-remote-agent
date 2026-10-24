# -*- coding: utf-8 -*-
"""多进程共享队列演示：两个 bridge 同时消费同一个 Redis 队列。

证明三件事：
1. 同一条任务只会被一个进程执行（Lua 原子 claim）；
2. 两个进程都在干活（各自日志里有"任务结束"）；
3. 没有任务丢失或重复（总数对齐）。

真实多机部署时，把 redis_url 指向同一个 Redis 主机即可，代码不用改。
跑法：python tests/demo_multiworker.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "stub_claude.py"
REDIS_URL = "redis://127.0.0.1:6380/3"   # 独立 db，不碰测试库(15)和生产库(0)
N = 40

sys.path.insert(0, str(ROOT / "bridge"))
from store_redis import RedisStore  # noqa: E402


def start_bridge(tmp, port, name):
    cfg = json.loads((ROOT / "bridge" / "config.json").read_text(encoding="utf-8"))
    cfg.update({"instance_lock_port": 0,
        "napcat_ws": "ws://127.0.0.1:1",
        "log_dir": str(tmp / f"logs-{name}"),
        "panel_port": port,
        "workers": 2,
        "max_queue": 500,
        "owner_uin": 0,
        "store_backend": "redis",
        "redis_url": REDIS_URL,
        "claude_cmd": [sys.executable, str(STUB)],
    })
    (tmp / f"logs-{name}").mkdir(exist_ok=True)
    cfg_path = tmp / f"{name}.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    env = dict(os.environ, QQ_REMOTE_CONFIG=str(cfg_path),
               QQ_REMOTE_STATE=str(tmp / f"state-{name}.json"),
               QQ_REMOTE_REDIS=REDIS_URL, STUB_SLEEP="0.1", PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                            cwd=str(ROOT / "bridge"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, cfg["panel_token"], cfg_path


def submit(port, token, i):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/task",
        data=json.dumps({"text": f"multi {i}"}).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Panel-Token": token})
    return urllib.request.urlopen(req, timeout=15).read()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="qqmulti-"))
    store = RedisStore(REDIS_URL, prefix="qq")
    store.r.flushdb()

    a, tok_a, _ = start_bridge(tmp, 3095, "A")
    b, tok_b, _ = start_bridge(tmp, 3094, "B")
    try:
        for port in (3095, 3094):
            for _ in range(60):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.2)

        t0 = time.perf_counter()
        for i in range(N):
            submit(3095 if i % 2 == 0 else 3094, tok_a if i % 2 == 0 else tok_b, i)
        print(f"已提交 {N} 条任务（一半从 A 的看板，一半从 B 的看板）")

        deadline = time.time() + 180
        while time.time() < deadline:
            st = store.stats()
            if st["done"] + st["failed"] >= N:
                break
            time.sleep(0.3)
        wall = time.perf_counter() - t0

        ids = [r["id"] for r in store.recent(500)]
        states = {}
        for tid in ids:
            t = store.get(tid)
            if t:
                states[t["state"]] = states.get(t["state"], 0) + 1

        def finished_count(name):
            log = tmp / f"logs-{name}" / "bridge.log"
            if not log.exists():
                return 0
            return log.read_text(encoding="utf-8", errors="replace").count("任务结束")

        fa, fb = finished_count("A"), finished_count("B")
        print(json.dumps({
            "submitted": N,
            "redis_task_count": len(ids),
            "states": states,
            "wall_s": round(wall, 2),
            "throughput_per_s": round(N / wall, 2),
            "finished_by_A": fa,
            "finished_by_B": fb,
            "both_worked": fa > 0 and fb > 0,
            "no_duplicates": len(ids) == N,
        }, ensure_ascii=False, indent=2))
    finally:
        for p in (a, b):
            p.terminate()
        for p in (a, b):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
