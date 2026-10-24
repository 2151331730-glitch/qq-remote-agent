# -*- coding: utf-8 -*-
"""闲聊/任务分流 + 提醒的端到端回归（3002 端口 + 独立 state/db，不碰真机）。

跑法：python tests/mock_test_chat.py（约 30 秒）
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

# Windows 控制台默认 GBK，打印 emoji / 日志里的替换字符会抛 UnicodeEncodeError，
# 而 print 抛错会直接把 mock 的收信协程带走 → 看起来像"桥没回"。统一改成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(exist_ok=True)
CONFIG = TMP / "chat-config.json"
STATE = TMP / "chat-state.json"
DB = TMP / "chat.db"
REMINDERS = TMP / "chat-reminders.db"
LOG_DIR = TMP / "logs-chat"
LOG_DIR.mkdir(exist_ok=True)

base_name = "config.json" if (ROOT / "bridge" / "config.json").exists() else "config.example.json"
cfg = json.loads((ROOT / "bridge" / base_name).read_text(encoding="utf-8"))
cfg.update({"napcat_token": "test-token", "panel_token": "test-panel",
            "workdir": str(TMP),
            "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
            "instance_lock_port": 0, "napcat_ws": "ws://127.0.0.1:3002",
            "log_dir": str(LOG_DIR), "owner_uin": 0, "panel_port": 3096,
            "db_path": str(DB), "reminder_check_seconds": 2,
            "persona_path": str(ROOT / "bridge" / "persona.md"),
            "memory_path": str(TMP / "chat-memory.md")})
CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
for p in (STATE, DB, REMINDERS, TMP / "chat-memory.md"):
    if p.exists():
        p.unlink()

_MID = [3000]
REPLIES = []
SCHEDULED = {"done": False}


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

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                REPLIES.append(d["params"]["message"])
                print(f"[回信] {d['params']['message'][:180]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, text):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {text}", flush=True)
        await ws.send(ev(text))

    async def schedule():
        await asyncio.sleep(0.5)
        print("\n[mock] >>> 在吗", flush=True)
        await ws.send(ev("在吗"))                                       # 闲聊
        await asyncio.sleep(5.5)
        print("[mock] >>> 帮我列一下桌面文件", flush=True)
        await ws.send(ev("帮我列一下桌面文件"))                          # 任务
        await asyncio.sleep(6)
        print("[mock] >>> /聊 帮我看看这个", flush=True)
        await ws.send(ev("/聊 帮我看看这个"))                            # 强制闲聊
        await asyncio.sleep(2.5)
        print("[mock] >>> /记住 测试记忆-芝麻", flush=True)
        await ws.send(ev("/记住 测试记忆-芝麻"))
        await asyncio.sleep(1)
        print("[mock] >>> /记忆", flush=True)
        await ws.send(ev("/记忆"))
        await asyncio.sleep(2.5)
        print("[mock] >>> /提醒 +2s 喝水", flush=True)
        await ws.send(ev("/提醒 +2s 喝水"))
        await asyncio.sleep(7)
        print("[mock] >>> /详情", flush=True)
        await ws.send(ev("/详情"))
        await asyncio.sleep(2)
        print("[mock] >>> /提醒列表", flush=True)
        await ws.send(ev("/提醒列表"))

    recv = asyncio.create_task(receiver())
    # 只在第一次连接时跑场景；桥重连后只继续收信，避免同一个场景被投多遍
    if not SCHEDULED["done"]:
        SCHEDULED["done"] = True
        await asyncio.gather(recv, asyncio.create_task(schedule()))
    else:
        await recv


async def main():
    async with serve(handler, "127.0.0.1", 3002):
        print("[mock] 服务已起 3002", flush=True)
        env = dict(os.environ, QQ_REMOTE_CONFIG=str(CONFIG), QQ_REMOTE_STATE=str(STATE),
                   QQ_REMOTE_DB=str(DB), QQ_REMOTE_REMINDERS=str(REMINDERS))
        bridge_log = open(TMP / "bridge-chat.log", "w", encoding="utf-8")
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env,
                                  stdout=bridge_log, stderr=subprocess.STDOUT)
        await asyncio.sleep(29)
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()


asyncio.run(main())

print("\n=== 数据库里的任务 ===")
c = sqlite3.connect(DB)
rows = list(c.execute("SELECT id, state, kind, substr(text,1,24) FROM tasks ORDER BY created_at"))
for row in rows:
    print(row)


def dump_logs():
    print("\n=== 桥 stdout/日志（排查用）===")
    for f in (TMP / "bridge-chat.log", LOG_DIR / "bridge.log"):
        if f.exists():
            print(f"--- {f} ---")
            print(f.read_text(encoding="utf-8", errors="replace")[-3000:])


def check(cond, msg):
    if not cond:
        dump_logs()
        print("\n=== 所有回信 ===")
        for r in REPLIES:
            print("-", r[:200])
        raise AssertionError(msg)


kinds = [r[2] for r in rows]
check(kinds == ["chat", "task", "chat"], f"分流结果应为 chat/task/chat，实际 {kinds}")
check(all(r[1] == "succeeded" for r in rows), "三条都应执行成功")

# 闲聊：没有 ✅ 前缀、没有 ⏱ 尾巴；任务：有 ✅
chat_replies = [r for r in REPLIES if "stub-ok" in r and "人格设定" in r]
task_replies = [r for r in REPLIES if r.startswith("✅")]
check(len(chat_replies) >= 2, f"应有 2 条闲聊回复，实际 {len(chat_replies)}")
check(not any("⏱" in r for r in chat_replies), f"闲聊回复不该带耗时尾巴：{chat_replies}")
check(len(task_replies) == 1, f"应有 1 条带 ✅ 的任务回复，实际 {task_replies}")

check(any(r.startswith("⏰ 喝水") for r in REPLIES), "提醒应到点推送")
check(any("记住了" in r and "测试记忆-芝麻" in r for r in REPLIES), "/记住 应写入并确认")
check(any("测试记忆-芝麻" in r and "我还记得" in r for r in REPLIES), "/记忆 应读回刚记的内容")
check((TMP / "chat-memory.md").exists()
      and "测试记忆-芝麻" in (TMP / "chat-memory.md").read_text(encoding="utf-8"),
      "记忆应落到文件里")
check(any("上次任务" in r and "输入" in r for r in REPLIES), "/详情 应显示上次任务的 token")
check(any("没有待提醒" in r for r in REPLIES), "提醒发出后 /提醒列表 应为空")

print("\n✅ 断言通过：分流 chat/task/chat、闲聊无任务后缀、任务带 ✅、提醒到点、/详情 有效")
