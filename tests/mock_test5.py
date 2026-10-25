# -*- coding: utf-8 -*-
"""v2 回归：持久化队列 + 幂等 + 命令（3002 端口 + 独立 state/db，不碰真机）。"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]  # 仓库根目录
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(exist_ok=True)
CONFIG = TMP / "test5-config.json"
STATE = TMP / "test5-state.json"
DB = TMP / "test5.db"
LOG_DIR = TMP / "logs5"
LOG_DIR.mkdir(exist_ok=True)

# 刻意记录用的是哪份配置：CI 上只有 config.example.json（没有 key → 走 claude 回退通道），
# 本地有 config.json（有 key → 走直连通道）。两条通道的差异曾经让一个 TypeError
# 只在 CI 炸、本地全绿。跑完看这一行就知道这次覆盖的是哪条路。
base_name = "config.json" if (ROOT / "bridge" / "config.json").exists() else "config.example.json"
print(f"[mock] 配置来源：bridge/{base_name}")
cfg = json.loads((ROOT / "bridge" / base_name).read_text(encoding="utf-8"))
cfg.update({"napcat_token": "test-token", "panel_token": "test-panel",
            "workdir": str(TMP),
            "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
            "instance_lock_port": 0,"napcat_ws": "ws://127.0.0.1:3002", "log_dir": str(LOG_DIR),
            "owner_uin": 12345, "panel_port": 3097, "db_path": str(DB)})
CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
for p in (STATE, DB):
    if p.exists():
        p.unlink()

TOKEN = cfg["napcat_token"]
_MID = [2000]
REPLIES = []


def ev(text, mid=None):
    if mid is None:
        _MID[0] += 1
        mid = _MID[0]
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": mid, "time": 0,
    }, ensure_ascii=False)


async def handler(ws):
    print("[mock] 桥已连接", flush=True)
    # 等看板就绪：CI 上冷启动慢，太早提交会全部失败
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cfg['panel_port']}/api/state",
                                   timeout=1).read()
            print("[mock] 看板就绪", flush=True)
            break
        except Exception:
            await asyncio.sleep(0.5)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                msg = d["params"]["message"]
                REPLIES.append(msg)
                print(f"[回信] {msg[:180]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, text, mid=None):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {text}", flush=True)
        await ws.send(ev(text, mid))

    await asyncio.gather(
        asyncio.create_task(receiver()),
        asyncio.create_task(say(0.5, "/心跳")),
        asyncio.create_task(say(2.0, "!echo 新队列可用")),
        asyncio.create_task(say(4.0, "只回复三个字：桥可用", mid=2100)),
        # 同一条 message_id 再投一次 → 必须被幂等丢掉，不能跑第二遍
        asyncio.create_task(say(16.0, "只回复三个字：桥可用", mid=2100)),
        asyncio.create_task(say(18.0, "/任务 5")),
        asyncio.create_task(say(20.0, "/状态")),
        asyncio.create_task(say(30.0, "/心跳")),
    )


async def main():
    async with serve(handler, "127.0.0.1", 3002):
        print("[mock] 服务已起 3002", flush=True)
        env = dict(os.environ, QQ_REMOTE_CONFIG=str(CONFIG), QQ_REMOTE_STATE=str(STATE),
                   QQ_REMOTE_DB=str(DB))
        bridge_log = open(TMP / "bridge-stdout.log", "w", encoding="utf-8")
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env,
                                  stdout=bridge_log, stderr=subprocess.STDOUT)

        # 不再固定睡 38 秒：CI 机器慢的时候任务还没跑完就被 terminate，
        # 表现为"任务卡在 running"，其实是测试自己把它杀了。
        # 改成轮询"任务是否已进入终态"，最多等 90 秒。
        import sqlite3 as _sq
        import time as _t

        def _settled():
            try:
                c = _sq.connect(DB)
                rows = list(c.execute("SELECT state FROM tasks"))
                c.close()
            except Exception:
                return False
            if not rows:
                return False
            return all(r[0] in ("succeeded", "failed", "cancelled", "interrupted")
                       for r in rows)

        deadline = _t.monotonic() + 90
        while _t.monotonic() < deadline:
            if _settled():
                print("[mock] 任务已进入终态", flush=True)
                # 再给几秒让最后一条回复发出去
                await asyncio.sleep(3)
                break
            if bridge.poll() is not None:
                print(f"[mock] bridge 提前退出，code={bridge.returncode}", flush=True)
                break
            await asyncio.sleep(1)
        else:
            print("[mock] 等待超时，按当前状态终止", flush=True)

        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()


asyncio.run(main())
print("\n=== 数据库里的任务 ===")
import sqlite3  # noqa: E402

c = sqlite3.connect(DB)
for row in c.execute("SELECT id, state, source, substr(text,1,24) FROM tasks ORDER BY created_at"):
    print(row)

# ---- 断言（CI 用）：幂等 + 执行成功 ----
rows = list(c.execute("SELECT id, state, tokens_in FROM tasks"))
if not rows:
    print("\n=== 桥 stdout/日志（排查用）===")
    for f in (TMP / "bridge-stdout.log", LOG_DIR / "bridge.log"):
        if f.exists():
            print(f"--- {f} ---")
            print(f.read_text(encoding="utf-8", errors="replace")[-2000:])
assert len(rows) == 1, f"期望 1 条任务（重复投递应被幂等丢弃），实际 {len(rows)}"
assert rows[0][1] == "succeeded", f"任务状态应为 succeeded，实际 {rows[0][1]}"
# worker 一旦因异常静默退出，后面的任务会永远停在 queued/running。
# 这条断言把"worker 还活着"显式化，而不是等到下一条任务超时才发现。
assert rows[0][1] != "running", "worker 可能在执行中异常退出了"
print("\n✅ 断言通过：1 条任务、succeeded、重复投递被丢弃")
