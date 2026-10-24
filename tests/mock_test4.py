# -*- coding: utf-8 -*-
"""场景四：断线重连。故意在第一条连接上断开，看桥能不能自己连回来并继续干活。

用 3002 端口 + 临时配置，不影响正在跑的 NapCat(3001)。
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]  # 仓库根目录
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(exist_ok=True)
CONFIG = TMP / "test-config.json"
LOG_DIR = TMP / "logs"
LOG_DIR.mkdir(exist_ok=True)

base_cfg = json.loads((ROOT / "bridge" / "config.json").read_text(encoding="utf-8"))
base_cfg.update({
    "napcat_ws": "ws://127.0.0.1:3002",
    "log_dir": str(LOG_DIR),
    "owner_uin": 0,
})
CONFIG.write_text(json.dumps(base_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

STATE = ROOT / "bridge" / "state.json"
TOKEN = base_cfg["napcat_token"]
CONN = {"n": 0}


def ev(text):
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "message_id": 1, "time": 0,
    }, ensure_ascii=False)


async def handler(ws):
    CONN["n"] += 1
    n = CONN["n"]
    print(f"[mock] 第 {n} 次连接", flush=True)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            if d.get("action") == "send_private_msg":
                print(f"[回信#{n}] {d['params']['message'][:120]}", flush=True)
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    asyncio.create_task(receiver())
    await asyncio.sleep(0.3)
    await ws.send(ev("/心跳"))
    if n == 1:
        await asyncio.sleep(1.5)
        print("[mock] 主动断开第 1 条连接", flush=True)
        await ws.close()
        return
    await asyncio.sleep(6)
    await ws.send(ev("/心跳"))


async def main():
    async with serve(handler, "127.0.0.1", 3002):
        print("[mock] 服务已起 3002", flush=True)
        env = dict(os.environ, QQ_REMOTE_CONFIG=str(CONFIG))
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env)
        await asyncio.sleep(18)
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()


asyncio.run(main())
print("=== 桥日志 ===")
log = LOG_DIR / "bridge.log"
print(log.read_text(encoding="utf-8") if log.exists() else "(无)")
