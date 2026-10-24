# -*- coding: utf-8 -*-
"""直接问 NapCat：登录信息、好友列表，并试发一条消息给自己。"""
import asyncio
import json

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:3001"
from _cfg import napcat_token
TOKEN = napcat_token()


async def main():
    async with connect(URL, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        async def call(action, params=None, echo=None):
            echo = echo or action
            await ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            while True:
                d = json.loads(await ws.recv())
                if d.get("echo") == echo:
                    return d

        info = await call("get_login_info", echo="login")
        print("登录信息:", json.dumps(info.get("data"), ensure_ascii=False))

        friends = await call("get_friend_list", echo="friends")
        data = friends.get("data") or []
        print(f"好友数: {len(data)}")
        for f in data[:5]:
            print("  -", f.get("user_id"), f.get("nickname"))

        me = (info.get("data") or {}).get("user_id")
        r = await call("send_private_msg", {"user_id": me, "message": "自检消息：通道正常"}, echo="send")
        print("发给自己:", r.get("status"), r.get("retcode"), r.get("message"))


asyncio.run(main())
