# -*- coding: utf-8 -*-
"""通过 NapCat 设置机器人头像：python tests/set_avatar.py <图片路径>"""
import asyncio
import json
import sys
from pathlib import Path

from websockets.asyncio.client import connect

from _cfg import napcat_token
TOKEN = napcat_token()
IMG = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "tmp" / "avatar.png")


async def main():
    async with connect("ws://127.0.0.1:3001",
                       additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        await ws.send(json.dumps({"action": "set_qq_avatar", "params": {"file": IMG},
                                  "echo": "set"}, ensure_ascii=False))
        while True:
            m = json.loads(await ws.recv())
            if m.get("echo") == "set":
                print("set_qq_avatar ->", m.get("status"), m.get("retcode"), m.get("message"))
                break


asyncio.run(main())
