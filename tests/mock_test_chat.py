# -*- coding: utf-8 -*-
"""端到端回归（3002 端口 + 独立 state/db，不碰真机）。

覆盖：
- 主人私聊：闲聊 / 任务分流、/记住 /记忆、/提醒、/详情
- 陌生人私聊：只陪聊（不带人格与记忆、不执行 ! 命令）
- 群聊：@ 才回、不回 @ 以外的话
- 好友申请：陌生人不动、主人重新加回同意

跑法：python tests/mock_test_chat.py（约 45 秒）
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

OWNER, SELF, STRANGER, GROUP = 12345, 999, 54321, 5555

base_name = "config.json" if (ROOT / "bridge" / "config.json").exists() else "config.example.json"
cfg = json.loads((ROOT / "bridge" / base_name).read_text(encoding="utf-8"))
cfg.update({"napcat_token": "test-token", "panel_token": "test-panel",
            "workdir": str(TMP),
            "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
            "instance_lock_port": 0, "napcat_ws": "ws://127.0.0.1:3002",
            "log_dir": str(LOG_DIR), "owner_uin": OWNER, "panel_port": 3096,
            "db_path": str(DB), "reminder_check_seconds": 2,
            "persona_path": str(ROOT / "bridge" / "persona.md"),
            "memory_path": str(TMP / "chat-memory.md"),
            "public_chat": True, "public_cooldown_seconds": 1,
            "public_max_per_hour": 50})
CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
for p in (STATE, DB, REMINDERS, TMP / "chat-memory.md"):
    if p.exists():
        p.unlink()

_MID = [3000]
REPLIES = []          # 发给主人的私聊
PUBLIC_REPLIES = []   # (uid, 文本) 发给陌生人的私聊
GROUP_REPLIES = []    # (gid, 文本)
FRIEND_ACTIONS = []   # set_friend_add_request 的参数
SCHEDULED = {"done": False}


def ev(text, mid=None, uid=OWNER):
    if mid is None:
        _MID[0] += 1
        mid = _MID[0]
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": SELF, "user_id": uid, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": mid, "time": 0,
    }, ensure_ascii=False)


def ev_group(text, at=True, uid=STRANGER, gid=GROUP, mid=None):
    if mid is None:
        _MID[0] += 1
        mid = _MID[0]
    segs = []
    raw = text
    if at:
        segs.append({"type": "at", "data": {"qq": str(SELF)}})
        raw = f"[CQ:at,qq={SELF}] {text}"
    segs.append({"type": "text", "data": {"text": (" " if at else "") + text}})
    return json.dumps({
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": SELF, "user_id": uid, "group_id": gid, "raw_message": raw,
        "message": segs, "message_id": mid, "time": 0,
        "sender": {"user_id": uid, "nickname": "路人", "card": ""},
    }, ensure_ascii=False)


def ev_friend(uid, flag):
    return json.dumps({"post_type": "request", "request_type": "friend",
                       "self_id": SELF, "user_id": uid, "flag": flag,
                       "comment": "交个朋友"}, ensure_ascii=False)


async def handler(ws):
    print("[mock] 桥已连接", flush=True)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            action, params = d.get("action"), d.get("params") or {}
            if action == "send_private_msg":
                msg = params.get("message", "")
                if int(params.get("user_id") or 0) == OWNER:
                    REPLIES.append(msg)
                    print(f"[回信] {msg[:180]}", flush=True)
                else:
                    PUBLIC_REPLIES.append((int(params.get("user_id") or 0), msg))
                    print(f"[公开回信→{params.get('user_id')}] {msg[:120]}", flush=True)
            elif action == "send_group_msg":
                GROUP_REPLIES.append((int(params.get("group_id") or 0), params.get("message", "")))
                print(f"[群回信→{params.get('group_id')}] {params.get('message', '')[:120]}", flush=True)
            elif action == "set_friend_add_request":
                FRIEND_ACTIONS.append(dict(params))
                print(f"[好友申请处理] {params}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def send(evt, note=""):
        if note:
            print(f"\n[mock] >>> {note}", flush=True)
        await ws.send(evt)

    async def schedule():
        await asyncio.sleep(0.5)
        await send(ev("在吗"), "在吗")                                   # 闲聊
        await asyncio.sleep(5.5)
        await send(ev("帮我列一下桌面文件"), "帮我列一下桌面文件")          # 任务
        await asyncio.sleep(6)
        await send(ev("/聊 帮我看看这个"), "/聊 帮我看看这个")              # 强制闲聊
        await asyncio.sleep(2.5)
        await send(ev("/记住 测试记忆-芝麻"), "/记住 测试记忆-芝麻")
        await asyncio.sleep(1)
        await send(ev("/记忆"), "/记忆")
        await asyncio.sleep(2.5)
        await send(ev("/提醒 +2s 喝水"), "/提醒 +2s 喝水")
        await asyncio.sleep(7)
        await send(ev("/详情"), "/详情")
        await asyncio.sleep(2)
        await send(ev("/提醒列表"), "/提醒列表")
        await asyncio.sleep(2)
        await send(ev("你是谁", uid=STRANGER), "陌生人私聊：你是谁")
        await asyncio.sleep(2.5)
        await send(ev_group("讲个笑话"), "群里 @ 机器人：讲个笑话")
        await asyncio.sleep(2.5)
        await send(ev_group("", at=True), "群里只 @、不说话")
        await asyncio.sleep(2.5)
        await send(ev_group("随便说说", at=False), "群里没 @：随便说说")
        await asyncio.sleep(2.5)
        await send(ev("!dir C:\\", uid=STRANGER), "陌生人发 !dir（必须只陪聊）")
        await asyncio.sleep(2)
        await send(ev_friend(STRANGER, "f-stranger"), "陌生人好友申请")
        await asyncio.sleep(1.5)
        await send(ev_friend(OWNER, "f-owner"), "主人重新加好友")

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
        await asyncio.sleep(48)
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
        print("=== 公开 ===", PUBLIC_REPLIES)
        print("=== 群 ===", GROUP_REPLIES)
        print("=== 好友申请 ===", FRIEND_ACTIONS)
        raise AssertionError(msg)


kinds = [r[2] for r in rows]
check(kinds == ["chat", "task", "chat"], f"主人分流结果应为 chat/task/chat，实际 {kinds}")
check(all(r[1] == "succeeded" for r in rows), "三条都应执行成功")

# 主人闲聊：没有 ✅ 前缀、没有 ⏱ 尾巴；任务：有 ✅
chat_replies = [r for r in REPLIES if "stub-ok" in r and "人格设定" in r]
task_replies = [r for r in REPLIES if r.startswith("✅")]
check(len(chat_replies) >= 2, f"应有 2 条主人闲聊回复，实际 {len(chat_replies)}")
check(not any("⏱" in r for r in chat_replies), f"闲聊回复不该带耗时尾巴：{chat_replies}")
check(len(task_replies) == 1, f"应有 1 条带 ✅ 的任务回复，实际 {task_replies}")

check(any(r.startswith("⏰ 喝水") for r in REPLIES), "提醒应到点推送")
check(any("记住了" in r and "测试记忆-芝麻" in r for r in REPLIES), "/记住 应写入并确认")
check(any("测试记忆-芝麻" in r and "我还记得" in r for r in REPLIES), "/记忆 应读回刚记的内容")
check((TMP / "chat-memory.md").exists()
      and "测试记忆-芝麻" in (TMP / "chat-memory.md").read_text(encoding="utf-8"),
      "记忆应落到文件里")
check(any("上次任务" in r and "输入" in r for r in REPLIES), "/详情 应显示上次任务的 token")

# 陌生人私聊：只陪聊
check(len(PUBLIC_REPLIES) >= 2, f"陌生人应有 2 条回复（你是谁 / !dir），实际 {PUBLIC_REPLIES}")
check(all(uid == STRANGER for uid, _ in PUBLIC_REPLIES), "公开回复应发给陌生人")
check(all(msg.startswith("stub-ok") for _, msg in PUBLIC_REPLIES),
      f"陌生人请求应走公开闲聊，实际 {PUBLIC_REPLIES}")
check(not any("人格设定" in msg or "测试记忆" in msg for _, msg in PUBLIC_REPLIES),
      "公开回复不能带主人的人格和记忆")
check(not any("Directory" in msg or "Volume" in msg for _, msg in PUBLIC_REPLIES),
      "陌生人发 !dir 不能真的执行命令")

# 群聊：@ 才回；只 @ 不说话也要有回音（否则看起来像没上线）
check(len(GROUP_REPLIES) == 2, f"群聊应回复「@+文字」和「只 @」两条，实际 {GROUP_REPLIES}")
check(any(msg.startswith("stub-ok") for _, msg in GROUP_REPLIES), "带文字的 @ 应走公开闲聊")
check(any("直接说" in msg for _, msg in GROUP_REPLIES), "只 @ 不说话应回一句提示")
check(all(gid == GROUP for gid, _ in GROUP_REPLIES), "群回复应发回原群")
check(not any("人格设定" in msg for _, msg in GROUP_REPLIES), "群回复不能带主人的人格")

# 好友申请：陌生人不动，主人同意
check(len(FRIEND_ACTIONS) == 1, f"只应处理主人的好友申请，实际 {FRIEND_ACTIONS}")
check(FRIEND_ACTIONS[0].get("flag") == "f-owner" and FRIEND_ACTIONS[0].get("approve") is True,
      "应同意主人的好友申请")
check(any("申请加好友" in r and str(STRANGER) in r for r in REPLIES),
      "陌生人好友申请应通知主人")

print("\n✅ 断言通过：分流、公开陪聊、群聊 @、好友申请策略全部符合预期")
