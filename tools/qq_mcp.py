# -*- coding: utf-8 -*-
"""QQ 工具 MCP server：把 NapCat 的接口包装成 agent 可直接调用的工具。

启动方式（由 Claude Code 自动拉起，stdio 传输）：
    python C:\\Users\\win11\\qq-remote\\tools\\qq_mcp.py

暴露的工具：
    qq_set_avatar(path)      换机器人头像（传本地图片绝对路径）
    qq_send_file(path)       把文件发给主人
    qq_get_profile()         看当前登录的账号信息
    qq_send_text(text)       主动给主人发一条消息

为什么要有它：agent 能 curl 任何接口，但没有"说明书"就会瞎试（换头像卡 7 分钟就是这么来的）。
包成 MCP 工具后，模型看到的是带 schema 的函数，调用即命中。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "bridge" / "config.json").read_text(encoding="utf-8"))
WS_URL = CFG.get("napcat_ws", "ws://127.0.0.1:3001")
TOKEN = CFG.get("napcat_token", "")
OWNER = int(CFG.get("owner_uin") or 0)

app = MCPServer("qq-tools")


async def _call(action: str, params: dict, timeout: float = 60.0):
    """连 NapCat 发一个 action，等回执。"""
    from websockets.asyncio.client import connect
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else None
    async with connect(WS_URL, additional_headers=headers, ping_interval=20,
                       max_size=2 ** 24) as ws:
        echo = action + "-1"
        await ws.send(json.dumps({"action": action, "params": params, "echo": echo},
                                 ensure_ascii=False))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("echo") == echo:
                return msg


def _run(action, params):
    msg = asyncio.run(_call(action, params))
    return {"status": msg.get("status"), "retcode": msg.get("retcode"),
            "message": msg.get("message"), "data": msg.get("data")}


@app.tool()
def qq_set_avatar(path: str) -> dict:
    """把 QQ 机器人头像换成指定图片。

    Args:
        path: 图片的本地绝对路径（C:\\... 或 /mnt/c/...）
    """
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    r = _run("set_qq_avatar", {"file": str(p)})
    return {"ok": r["status"] == "ok", **r}


@app.tool()
def qq_send_text(text: str) -> dict:
    """主动给主人发一条 QQ 消息。"""
    if not OWNER:
        return {"ok": False, "error": "config 里没有 owner_uin"}
    r = _run("send_private_msg", {"user_id": OWNER, "message": text})
    return {"ok": r["status"] == "ok", **r}


@app.tool()
def qq_send_file(path: str) -> dict:
    """把本地文件发给主人（QQ 私聊）。

    Args:
        path: 文件绝对路径
    """
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not OWNER:
        return {"ok": False, "error": "config 里没有 owner_uin"}
    r = _run("upload_private_file", {"user_id": OWNER, "file": str(p), "name": p.name},
             timeout=180)
    return {"ok": r["status"] == "ok", **r}


@app.tool()
def qq_get_profile() -> dict:
    """查看当前登录的 QQ 账号信息（号码、昵称）。"""
    login = _run("get_login_info", {})
    friends = _run("get_friend_list", {})
    return {"login": login.get("data"),
            "friend_count": len(friends.get("data") or [])}


if __name__ == "__main__":
    app.run()
