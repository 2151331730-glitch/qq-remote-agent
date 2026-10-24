# -*- coding: utf-8 -*-
"""用假的 OneBot 服务端把 bridge 跑一遍，不用真 QQ 就能验证逻辑。

跑法：python C:\\Users\\win11\\qq-remote\\tests\\mock_test.py
注意：跑完会临时把 owner_uin 绑成 12345，结束后脚本会把它清掉。
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(r"C:\Users\win11\qq-remote")
from _cfg import napcat_token
TOKEN = napcat_token()
SENT = []


def ev(text):
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": 1, "time": 0,
    }, ensure_ascii=False)


async def handler(ws):
    if ws.request.headers.get("Authorization", "") != f"Bearer {TOKEN}":
        await ws.close(code=4003, reason="bad token")
        return
    print("[mock] bridge 已连接", flush=True)

    async def send_later(delay, text):
        await asyncio.sleep(delay)
        print(f"[mock] 发送: {text}", flush=True)
        await ws.send(ev(text))

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                print(f"[回信] {d['params']['message'][:200]}", flush=True)
            else:
                print(f"[动作] {d.get('action')} {str(d.get('params'))[:120]}", flush=True)
            # OneBot 约定：带 echo 的动作要回执，否则客户端会一直等
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0,
                                          "data": {"message_id": 1},
                                          "echo": d["echo"]}, ensure_ascii=False))

    tasks = [
        asyncio.create_task(receiver()),
        asyncio.create_task(send_later(0.5, "/心跳")),
        asyncio.create_task(send_later(1.5, "/状态")),
        asyncio.create_task(send_later(2.5, "!echo hello-from-shell")),
        asyncio.create_task(send_later(4.0, "只回复三个字：桥可用")),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=120)
    except (asyncio.TimeoutError, Exception):
        pass


async def main():
    async with serve(handler, "127.0.0.1", 3001):
        print("[mock] 服务已起 127.0.0.1:3001", flush=True)
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
    # 清理测试留下的绑定
    state = ROOT / "bridge" / "state.json"
    if state.exists():
        data = json.loads(state.read_text(encoding="utf-8"))
        data["owner_uin"] = 0
        data["session_id"] = ""
        state.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[清理] owner_uin 已重置")
