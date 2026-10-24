# -*- coding: utf-8 -*-
"""场景二：验证上下文续接（--resume）和 /停止 强杀。"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(r"C:\Users\win11\qq-remote")
TOKEN = "a7f3c9d21b8e4650"


def ev(text):
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": 1, "time": 0,
    }, ensure_ascii=False)


async def handler(ws):
    if ws.request.headers.get("Authorization", "") != f"Bearer {TOKEN}":
        await ws.close(code=4003)
        return
    print("[mock] bridge 已连接", flush=True)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                print(f"[回信] {d['params']['message'][:200]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, text):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {text}", flush=True)
        await ws.send(ev(text))

    tasks = [
        asyncio.create_task(receiver()),
        asyncio.create_task(say(0.5, "记住数字 7，后面我会问。只回复 OK")),
        asyncio.create_task(say(14, "我刚才让你记的数字是多少？只回复那个数字")),
        asyncio.create_task(say(32, "在 PowerShell 里执行 ping -n 20 127.0.0.1，把结果告诉我")),
        asyncio.create_task(say(45, "/停止")),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=110)
    except Exception as e:
        print("[mock] 结束:", type(e).__name__, flush=True)


async def main():
    async with serve(handler, "127.0.0.1", 3001):
        print("[mock] 服务已起", flush=True)
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"))
        await asyncio.sleep(70)
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
        print("[清理] owner_uin / session 已重置")
