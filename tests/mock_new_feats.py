# -*- coding: utf-8 -*-
"""新能力回归：连发合并 + 语音转文字（3003 端口 + 独立 state/db，不碰真机）。

验证两件在单测里测不到的事：
  1. 主人连发两句纯文字 → 只产生 **一条** 任务，且两句话都在里面；
  2. 一条语音消息 → 走 get_record 拿音频 → 本地 faster-whisper 转成文字 → 进任务正文。

语音用 Windows 自带的 Huihui 先合成一句中文（不需要人真的说话），
桥那一侧的识别服务照常起在 18797 端口，和真机 18791 不冲突。
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(exist_ok=True)
CONFIG = TMP / "newfeat-config.json"
STATE = TMP / "newfeat-state.json"
DB = TMP / "newfeat.db"
LOG_DIR = TMP / "logs-newfeat"
LOG_DIR.mkdir(exist_ok=True)
ASR_PORT = 18797
SAY = "帮我看看那个项目是不是挂了啊"

base_name = "config.json" if (ROOT / "bridge" / "config.json").exists() else "config.example.json"
cfg = json.loads((ROOT / "bridge" / base_name).read_text(encoding="utf-8"))
cfg.update({
    "napcat_token": "test-token", "panel_token": "test-panel",
    "workdir": str(TMP), "log_dir": str(LOG_DIR),
    "claude_cmd": [sys.executable, str(ROOT / "tests" / "stub_claude.py")],
    "instance_lock_port": 0, "napcat_ws": "ws://127.0.0.1:3003",
    "owner_uin": 12345, "panel_port": 3098, "db_path": str(DB),
    # 合并窗口调短，测试不用等 20 秒
    "owner_merge_ms": 1200, "owner_merge_max_ms": 4000,
    "voice": {"enabled": True, "port": ASR_PORT, "model": "small", "language": "zh",
              "timeout_seconds": 180},
    # 关掉直连：让闲聊也走 stub，避免真去调 API
    "chat_direct": {},
})
CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
for p in (STATE, DB):
    if p.exists():
        p.unlink()

REPLIES = []
_MID = [3000]


def ev(text=None, mid=None, record=None):
    if mid is None:
        _MID[0] += 1
        mid = _MID[0]
    segs = []
    raw = text or ""
    if record:
        segs.append({"type": "record", "data": {"file": record}})
        raw = f"[CQ:record,file={record}]"
    elif text:
        segs.append({"type": "text", "data": {"text": text}})
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 999, "user_id": 12345, "raw_message": raw, "message": segs,
        "message_id": mid, "time": 0,
    }, ensure_ascii=False)


def make_speech_wav(path: Path) -> bool:
    """用系统 TTS 合成一句中文；没有中文语音就返回 False（测试跳过语音部分）。"""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SelectVoice('Microsoft Huihui Desktop'); "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{SAY}'); $s.Dispose()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=120)
        return r.returncode == 0 and path.exists() and path.stat().st_size > 2000
    except Exception:
        return False


async def handler(ws):
    print("[mock] 桥已连接", flush=True)
    import urllib.request
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cfg['panel_port']}/api/state",
                                   timeout=1).read()
            print("[mock] 看板就绪", flush=True)
            break
        except Exception:
            await asyncio.sleep(0.5)

    async def receiver():
        async for raw in ws:
            d = json.loads(raw)
            action = d.get("action")
            if action == "send_private_msg":
                REPLIES.append(d["params"]["message"])
                print(f"[回信] {str(d['params']['message'])[:180]}", flush=True)
            if action == "get_record":
                # 假装 NapCat 取到了语音：直接回 base64
                print("[mock] get_record → 返回测试音频", flush=True)
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": d.get("echo"),
                    "data": {"base64": AUDIO_B64}}, ensure_ascii=False))
                continue
            if d.get("echo") is not None:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                          "echo": d["echo"]}, ensure_ascii=False))

    async def say(delay, payload):
        await asyncio.sleep(delay)
        print(f"\n[mock] >>> {payload[:80]}", flush=True)
        await ws.send(payload)

    tasks = [asyncio.create_task(receiver())]
    # 1) 连发两句（间隔 0.6 秒）→ 应该只变成一条任务
    tasks.append(asyncio.create_task(say(1.0, ev("帮我看看第一句"))))
    tasks.append(asyncio.create_task(say(1.6, ev("还有第二句"))))
    # 2) 语音消息 → get_record → 本地转写
    tasks.append(asyncio.create_task(say(10.0, ev(record="test-voice.amr"))))
    await asyncio.gather(*tasks)


AUDIO_B64 = ""
B = __import__("base64")


def kill_port(port: int) -> None:
    """按端口杀掉监听进程（测试收尾用：桥拉起的 asr_server 在桥死后是孤儿）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            subprocess.run(["taskkill", "/PID", parts[4], "/F"], capture_output=True)


async def main():
    global AUDIO_B64
    wav = TMP / "newfeat-voice.wav"
    if not (wav.exists() and wav.stat().st_size > 2000):
        if not make_speech_wav(wav):
            print("[mock] 没有中文语音，跳过语音断言")
    if wav.exists():
        AUDIO_B64 = B.b64encode(wav.read_bytes()).decode()

    async with serve(handler, "127.0.0.1", 3003):
        print("[mock] 服务已起 3003", flush=True)
        env = dict(os.environ, QQ_REMOTE_CONFIG=str(CONFIG), QQ_REMOTE_STATE=str(STATE),
                   QQ_REMOTE_DB=str(DB))
        log = open(TMP / "newfeat-bridge.log", "w", encoding="utf-8")
        bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge" / "bridge.py")],
                                  cwd=str(ROOT / "bridge"), env=env,
                                  stdout=log, stderr=subprocess.STDOUT)
        import sqlite3
        import time

        def settled(min_rows):
            try:
                c = sqlite3.connect(DB)
                rows = list(c.execute("SELECT state FROM tasks"))
                c.close()
            except Exception:
                return False
            return len(rows) >= min_rows and all(
                r[0] in ("succeeded", "failed", "cancelled", "interrupted") for r in rows)

        want = 2 if AUDIO_B64 else 1
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if settled(want):
                await asyncio.sleep(3)
                break
            if bridge.poll() is not None:
                print(f"[mock] bridge 提前退出 code={bridge.returncode}", flush=True)
                break
            await asyncio.sleep(1)
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()
        # 桥拉起的识别服务是它的子进程，桥死后会变成孤儿（一个 ~390MB 的 whisper），
        # 下一次跑 e2e 还会因为 18797 被占而误判。按端口收掉。
        kill_port(ASR_PORT)


asyncio.run(main())

print("\n=== 数据库里的任务 ===")
import sqlite3  # noqa: E402

c = sqlite3.connect(DB)
rows = list(c.execute("SELECT id, state, text FROM tasks ORDER BY created_at"))
for r in rows:
    print(r[0], r[1], repr(r[2][:80]))

texts = [r[2] for r in rows]
merged = [t for t in texts if "第一句" in t]
assert merged, f"没有找到合并后的任务：{texts}"
assert "第二句" in merged[0], f"合并任务里缺第二句：{merged[0]!r}"
assert len(merged) == 1, f"连发两句应该只产生一条任务，实际 {len(merged)} 条"
print("\n✅ 连发合并通过：两句并成一条任务")

if AUDIO_B64:
    spoken = [t for t in texts if "语音转文字" in t]
    assert spoken, f"语音没有转成文字：{texts}"
    assert any("项目" in t or "帮我" in t for t in spoken), f"转写内容不对：{spoken}"
    print("✅ 语音转文字通过：", repr(spoken[0][:60]))
