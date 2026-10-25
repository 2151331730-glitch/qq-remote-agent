# -*- coding: utf-8 -*-
"""语音转文字的客户端（bridge 这一侧）。

分工：
    bridge.py  负责从 QQ 把音频拿下来（NapCat get_record / 直链），
    voice.py   负责「把音频变成文字」—— 拉起本地 faster-whisper 常驻服务并调用。
分开的理由：拿音频要用 OneBot 的 WS 连接，识别只要一段字节，两者失败方式完全不同。

失败策略（照抄 qq-agent-plugins 的经验）：
    任何一步出问题都返回 None，绝不抛异常打断消息入库。
    上层看到 None 就把这条语音当「没转成文字」处理，其余消息照常处理。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "asr_server.py"


def deps_ok() -> bool:
    """faster-whisper 装没装。只查模块在不在，不 import（import 要十几秒）。"""
    import importlib.util
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


class Voice:
    def __init__(self, cfg: dict | None = None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.port = int(cfg.get("port") or 18791)
        self.model = str(cfg.get("model") or "small")
        self.language = str(cfg.get("language") or "zh")
        self.timeout = float(cfg.get("timeout_seconds") or 180)
        self.python = str(cfg.get("python") or sys.executable)
        self.model_dir = str(cfg.get("model_dir") or (HERE / "asr-models"))
        self._proc = None
        self._starting = False
        self._last_fail = 0.0
        self._lock = threading.Lock()

    # ---- 服务生命周期 ----
    def _ping(self, timeout=1.5) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health",
                                        timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")).get("ok") is True
        except Exception:
            return False

    def ensure_server(self) -> bool:
        """保证识别服务在跑。起不来返回 False（调用方当成「没转成文字」）。"""
        if not self.enabled:
            return False
        if self._ping():
            return True
        with self._lock:
            if self._ping():
                return True
            # 5 秒内刚失败过就别再拉，避免每条语音都白等一轮
            if time.time() - self._last_fail < 5:
                return False
            if self._starting:
                return False
            if not deps_ok():
                self._last_fail = time.time()
                return False
            self._starting = True
            try:
                env = dict(os.environ)
                env.update({
                    "ASR_PORT": str(self.port),
                    "ASR_MODEL": self.model,
                    "ASR_LANG": self.language,
                    "ASR_MODEL_DIR": self.model_dir,
                    "ASR_PRELOAD": "1",          # 提前加载模型，第一条语音不用等
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1",
                })
                flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
                self._proc = subprocess.Popen(
                    [self.python, str(SERVER)],
                    cwd=str(HERE), env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=flags)
                deadline = time.time() + 25
                while time.time() < deadline:
                    if self._ping(1.0):
                        return True
                    if self._proc is not None and self._proc.poll() is not None:
                        break            # 子进程自己退了（端口被占/模型缺失），别空等 25 秒
                    time.sleep(0.4)
            except Exception:
                pass
            finally:
                self._starting = False
            # 没起来就收掉自己拉起的东西：否则每失败一次留一个孤儿进程，
            # 攒几个之后端口上到底是谁在服务就说不清了。
            self.stop()
            self._last_fail = time.time()
            return False

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    # ---- 识别 ----
    def transcribe(self, audio: bytes) -> str | None:
        """音频字节 → 文字。失败一律 None。"""
        if not audio or not self.ensure_server():
            return None
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/transcribe", data=audio,
                headers={"Content-Type": "application/octet-stream"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = str(data.get("text") or "").strip()
            return text or None
        except Exception:
            return None
