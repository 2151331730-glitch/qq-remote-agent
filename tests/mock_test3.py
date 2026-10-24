# -*- coding: utf-8 -*-
"""场景三：只验证上下文续接（第一条任务不能被绑定逻辑吞掉）。"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(r"C:\Users\win11\qq-remote")
from _cfg import napcat_token
TOKEN = napcat_token()


def ev(text):
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": 1, "time": 0,
    }, ensure_ascii=False)


async def handler(ws):
    print("[mock] bridge 已连接", flush=True)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                print(f"[回信] {d['params']['message'][:300]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, text):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {text}", flush=True)
        await ws.send(ev(text))

    await asyncio.gather(
        asyncio.create_task(receiver()),
        asyncio.create_task(say(0.5, "记住数字 7，收到只回复 OK")),
        asyncio.create_task(say(30, "我刚才让你记的数字是多少？只回复那个数字")),
    )


async def main():
    async with serve(handler, "127.0.0.1", 3001):
        print("[mock] 服务已起", flush=True)
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"))
        await asyncio.sleep(75)
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()


if __name__ == "__main__":
    asyncio.run(main())
    state = ROOT / "bridge" / "state.json"
    if state.exists():
        data = json.loads(state.read_text(encoding="utf-8"))
        data["owner_uin"] = 0
        data["session_id"] = ""
        state.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[清理] 已重置")
