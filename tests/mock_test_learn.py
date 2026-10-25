# -*- coding: utf-8 -*-
r"""端到端验证：从一条 QQ 消息到学到规则。

跑法（不碰真机 3001）：
    python tests/mock_test_learn.py

流程：
1. 起一个假 OneBot 服务端（3003）
2. 起一份桥的副本，配置指向临时目录（独立 state/db/rules）
3. 发一条"下次把这句话省了"，等它回执
4. 检查 rules.json 里是否真的多了规则
5. 再发一条会触发规则的消息，把它回的话拿出来，看规则有没有生效
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp" / "learn"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

CONFIG = TMP / "config.json"
STATE = TMP / "state.json"
DB = TMP / "tasks.db"
RULES = TMP / "rules.json"
LOGS = TMP / "logs"
LOGS.mkdir(exist_ok=True)

base = json.loads((ROOT / "bridge" / "config.example.json").read_text(encoding="utf-8"))
base.update({
    "napcat_token": "t", "panel_token": "p", "workdir": str(TMP),
    # 用一个"永远回带反问的句子"的假 CLI，方便验证规则有没有真的改掉它
    "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
    "instance_lock_port": 0,
    "napcat_ws": "ws://127.0.0.1:3003",
    "log_dir": str(LOGS),
    "owner_uin": 12345,
    "panel_port": 3096,
    "db_path": str(DB),
    "rules_path": str(RULES),
    # 关键：关掉直连，逼它走 claude 通道（stub 可控），这样验证的是规则层而不是模型
    "chat_direct": {"enabled": False},
})
CONFIG.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
for p in (STATE, DB, RULES):
    if p.exists():
        p.unlink()

_MID = [5000]
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
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{base['panel_port']}/api/state",
                                   timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.5)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                REPLIES.append(d["params"]["message"])
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}))

    async def say(delay, text, mid=None):
        await asyncio.sleep(delay)
        print(f"[mock] >>> {text}", flush=True)
        await ws.send(ev(text, mid))

    await asyncio.gather(
        asyncio.create_task(receiver()),
        # 第 1 句就是他的原话
        asyncio.create_task(say(1.0, "下次把这句话省了")),
        # 第 2 句随便聊一句，看规则有没有影响回话
        asyncio.create_task(say(14.0, "在干嘛")),
    )


async def main():
    async with serve(handler, "127.0.0.1", 3003):
        env = dict(os.environ,
                   QQ_REMOTE_CONFIG=str(CONFIG), QQ_REMOTE_STATE=str(STATE),
                   QQ_REMOTE_DB=str(DB), QQ_REMOTE_RULES=str(RULES),
                   PYTHONUTF8="1")
        blog = open(TMP / "bridge-out.log", "w", encoding="utf-8")
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env,
                                  stdout=blog, stderr=subprocess.STDOUT)

        for _ in range(50):
            await asyncio.sleep(1)
            if RULES.exists():
                break
        await asyncio.sleep(12)
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()


asyncio.run(main())

print("\n=== 收到的所有回信 ===")
for r in REPLIES:
    print(" ", repr(r[:120]))

print("\n=== rules.json ===")
if RULES.exists():
    print(RULES.read_text(encoding="utf-8"))
else:
    print("(不存在 —— 没学到)")

print("\n=== bridge 日志（学习相关）===")
logf = LOGS / "bridge.log"
if logf.exists():
    for line in logf.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(k in line for k in ("学到", "规则", "闲聊", "收到")):
            print(" ", line)

# ---- 断言 ----
ok = True
if not RULES.exists():
    print("\n❌ 没有生成 rules.json")
    ok = False
else:
    data = json.loads(RULES.read_text(encoding="utf-8"))
    kinds = [r.get("kind") for r in data]
    print(f"\n学到的规则类型：{kinds}")
    if "no_open_ended" not in kinds:
        print("❌ 应该学到 no_open_ended")
        ok = False
    else:
        print("✅ 学到的规则包含 no_open_ended")

print("\n✅ 通过" if ok else "\n❌ 失败")
sys.exit(0 if ok else 1)
