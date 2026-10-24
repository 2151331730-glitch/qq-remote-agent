# -*- coding: utf-8 -*-
"""场景六：危险操作确认 + 看板/网页入口（3003 端口 + 独立配置，不碰真机）。"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]  # 仓库根目录
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(exist_ok=True)
CONFIG = TMP / "test6-config.json"
STATE = TMP / "test6-state.json"
LOG_DIR = TMP / "logs6"
LOG_DIR.mkdir(exist_ok=True)
PANEL = "http://127.0.0.1:3098"

base_name = "config.json" if (ROOT / "bridge" / "config.json").exists() else "config.example.json"
cfg = json.loads((ROOT / "bridge" / base_name).read_text(encoding="utf-8"))
cfg.update({"workdir": str(TMP),
            "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
            "instance_lock_port": 0,"napcat_ws": "ws://127.0.0.1:3003", "log_dir": str(LOG_DIR),
            "owner_uin": 12345, "panel_port": 3098, "db_path": str(TMP / "test6.db")})
CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
if STATE.exists():
    STATE.unlink()

TOKEN = cfg["napcat_token"]
PANEL_TOKEN = cfg["panel_token"]


_MID = [1000]


def ev(text):
    # 每条消息给唯一 message_id：桥用它做幂等键，重复 ID 会被当成重投丢掉
    _MID[0] += 1
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": _MID[0], "time": 0,
    }, ensure_ascii=False)


REPLIES = []


async def handler(ws):
    print("[mock] 桥已连接", flush=True)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                msg = d["params"]["message"]
                REPLIES.append(msg)
                print(f"[回信] {msg[:160]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, text):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {text}", flush=True)
        await ws.send(ev(text))

    await asyncio.gather(
        asyncio.create_task(receiver()),
        asyncio.create_task(say(0.5, "删除桌面上的所有文件")),
        asyncio.create_task(say(2.0, "/取消")),
        asyncio.create_task(say(3.5, "!Remove-Item C:\\nonexistent\\zzz -Recurse -Force")),
        asyncio.create_task(say(5.0, "/确认")),
        asyncio.create_task(say(9.0, "/状态")),
        asyncio.create_task(say(25.0, "/心跳")),  # 保持连接，避免测试中途断线干扰 HTTP 调用
    )


def http(path, data=None, token=None):
    req = urllib.request.Request(PANEL + path)
    if data is not None:
        req.data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Panel-Token", token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


async def main():
    async with serve(handler, "127.0.0.1", 3003):
        print("[mock] 服务已起 3003", flush=True)
        env = dict(os.environ, QQ_REMOTE_CONFIG=str(CONFIG), QQ_REMOTE_STATE=str(STATE),
                   QQ_REMOTE_DB=str(TMP / "test6.db"))
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env)
        try:
            await asyncio.sleep(8)

            # 1) 面板状态
            code, st = http("/api/state")
            print(f"[面板] state {code} 队列={st.get('queue')} 当前={st.get('current')!r}", flush=True)

            # 2) 无 token 提交应被拒绝
            try:
                http("/api/task", {"text": "echo hi"})
                print("[面板] 无 token 提交：未被拒绝（BUG）", flush=True)
            except urllib.error.HTTPError as e:
                print(f"[面板] 无 token 提交被拒绝 {e.code}（正确）", flush=True)

            # 3) 有 token 提交普通任务
            code, res = http("/api/task", {"text": "只回复两个字：收到"}, PANEL_TOKEN)
            print(f"[面板] 提交 {code} -> {res}", flush=True)

            # 4) 危险任务从网页提交 → 应要求确认
            code, res = http("/api/task", {"text": "把 D 盘格式化掉"}, PANEL_TOKEN)
            print(f"[面板] 危险任务 {code} -> {res}", flush=True)

            # 5) 审计事件
            for attempt in (1, 2, 3):
                try:
                    code, d = http("/api/tasks")
                    print(f"[面板] tasks {code} 事件数={len(d.get('events', []))}", flush=True)
                    break
                except Exception as e:
                    print(f"[面板] tasks 第 {attempt} 次失败：{e}", flush=True)
                    await asyncio.sleep(1)

            await asyncio.sleep(12)
        finally:
            bridge.terminate()
            try:
                bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge.kill()

        print("\n=== 断言 ===", flush=True)
        joined = "\n".join(REPLIES)
        checks = {
            "危险任务被拦下": "回复 /确认" in joined,
            "取消生效": "已取消待确认" in joined,
            "确认后执行命令": "已确认执行命令" in joined,
            "可见审计文件": (LOG_DIR / "audit.jsonl").exists(),
        }
        for k, v in checks.items():
            print(f"  {k}: {v}")
        assert all(checks.values()), f"失败项: {[k for k, v in checks.items() if not v]}"


asyncio.run(main())
