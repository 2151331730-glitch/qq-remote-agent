# -*- coding: utf-8 -*-
"""本地语音识别服务（faster-whisper），只监听 127.0.0.1。

为什么是常驻服务而不是每条语音起一次 python：
    whisper 模型加载要好几秒，一条几秒的语音如果每次都重新加载，
    光加载就比识别本身久。常驻之后模型只加载一次。

接口：
    GET  /health      → {"ok": true, "model": "small", "loaded": true/false}
    POST /transcribe  → 请求体是音频原始字节（wav / amr / silk 都行，
                        由 PyAV 解码），返回 {"ok": true, "text": "...", "lang": "zh"}

来源：改编自 wangpx2006/qq-agent-plugins 的 voice-transcribe（MIT），
按本项目目录结构调整了模型路径与启动方式。

踩过的坑（照抄，别再踩一遍）：
  · 模型必须走国内镜像：直连 huggingface.co 在这台机器上不通 → HF_ENDPOINT=hf-mirror.com
  · 新版 huggingface_hub 默认走 xet 加速，镜像站不支持，会以 401 失败
    （cas-server.xethub.hf.co）→ 必须 HF_HUB_DISABLE_XET=1
  · av 19 和 faster-whisper 1.2.x 不兼容，报
    `open() got an unexpected keyword argument 'metadata_errors'` → 锁 av==13.1.0
"""

import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

HERE = os.path.dirname(os.path.abspath(__file__))
# 模型不放在会被文件监听扫到的地方：这里是 bridge/ 下的独立目录，只有本文件会碰
MODEL_DIR = os.environ.get("ASR_MODEL_DIR") or os.path.join(HERE, "asr-models")
MODEL_SIZE = os.environ.get("ASR_MODEL", "small")
# 模型优先用本地目录（asr-models/small）里的那一份：
#   · 下载是**一次性**的，用法见 get_asr_model.py；
#   · 运行时只读本地文件，断网也能转语音；
#   · 只有本地没有时才去下（hf-mirror 时通时不通，别指望它）。
LOCAL_DIR = os.path.join(MODEL_DIR, MODEL_SIZE)
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
PORT = int(os.environ.get("ASR_PORT", "18791"))
# 中文群里偶尔夹英文，强制 zh 比自动检测稳；纯英文句子里英文词照样认得出
LANGUAGE = os.environ.get("ASR_LANG", "zh") or None
PRELOAD = os.environ.get("ASR_PRELOAD", "0") == "1"

_model = None
_model_lock = threading.Lock()   # 加载互斥
_infer_lock = threading.Lock()   # 推理串行（CPU 上并发只会互相拖慢）


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel
                t0 = time.time()
                if os.path.isfile(os.path.join(LOCAL_DIR, "model.bin")):
                    _model = WhisperModel(LOCAL_DIR, device="cpu", compute_type="int8")
                    print(f"[asr] 本地模型 {LOCAL_DIR} 加载完成，用时 {time.time() - t0:.1f}s",
                          flush=True)
                else:
                    print(f"[asr] 本地没有模型（{LOCAL_DIR}），尝试联网下载…", flush=True)
                    _model = WhisperModel(
                        MODEL_SIZE,
                        device="cpu",
                        compute_type="int8",
                        download_root=MODEL_DIR,
                        local_files_only=False,
                    )
                    print(f"[asr] 模型 {MODEL_SIZE} 下载并加载完成，用时 {time.time() - t0:.1f}s",
                          flush=True)
    return _model


def transcribe(audio_bytes):
    model = get_model()
    with _infer_lock:
        segments, info = model.transcribe(
            io.BytesIO(audio_bytes),
            language=LANGUAGE,
            # whisper 默认常吐繁体；给一句简体中文当引子，输出就会跟着用简体
            initial_prompt="以下是普通话的句子，请用简体中文。",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            beam_size=1,
            condition_on_previous_text=False,
        )
        text = "".join(s.text for s in segments).strip()
    return text, getattr(info, "language", "") or ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self._send(200, {"ok": True, "model": MODEL_SIZE,
                                    "loaded": _model is not None, "port": PORT})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/transcribe":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._send(400, {"ok": False, "error": "空请求体"})
            if n > 30 * 1024 * 1024:
                return self._send(413, {"ok": False, "error": "音频太大"})
            audio = b""
            while len(audio) < n:
                chunk = self.rfile.read(min(65536, n - len(audio)))
                if not chunk:
                    break
                audio += chunk
            t0 = time.time()
            text, lang = transcribe(audio)
            return self._send(200, {"ok": True, "text": text, "lang": lang,
                                    "ms": int((time.time() - t0) * 1000)})
        except Exception as e:  # 任何异常都回结构化结果，别让调用方一直等
            import traceback
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会允许两个进程绑同一个端口，请求被随机分走 ——
    # 结果是"有时识别很慢有时正常"，极难查。这里明确关掉：
    # 第二个实例必须绑定失败并立刻退出，保证端口上永远只有一个识别服务。
    allow_reuse_address = False
    daemon_threads = True


def main():
    os.makedirs(MODEL_DIR, exist_ok=True)
    # 先占端口再加载模型：重复实例会在几毫秒内退出，而不是先吃 480MB 内存再打架。
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"[asr] 端口 {PORT} 已被占用，本实例退出：{e}", flush=True)
        return 1
    if PRELOAD:
        # 后台加载，不挡 /health：调用方可以先看到服务在、再等模型就绪
        threading.Thread(target=get_model, daemon=True).start()
    print(f"[asr] listening on 127.0.0.1:{PORT} model={MODEL_SIZE} dir={MODEL_DIR}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
