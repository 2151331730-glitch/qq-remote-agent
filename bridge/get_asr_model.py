# -*- coding: utf-8 -*-
"""一次性下载 faster-whisper 模型到 bridge/asr-models/<size>/。

为什么单独一个脚本：模型 480MB（small），只在装机时下一次。
运行时（asr_server.py）只读本地文件，断网也能转语音。

用法：
    python bridge\\get_asr_model.py            # small（默认，推荐）
    python bridge\\get_asr_model.py medium     # 更准更慢更大

网络说明（实测）：
    · hf-mirror.com 目前会 308 跳回 huggingface.co，直连不通；
    · 本机走 Pigeon 代理（127.0.0.1:7892）能下；
    · 所以脚本会自动读 HTTPS_PROXY，没设就用 127.0.0.1:7892 试一次。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
}
PATTERNS = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt",
            "preprocessor_config.json"]


def main():
    size = (sys.argv[1] if len(sys.argv) > 1 else "small").strip()
    repo = MODELS.get(size)
    if not repo:
        print(f"不认识的模型：{size}（可选：{', '.join(MODELS)}）")
        return 2
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # 镜像站不支持 xet
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    if not any(k.endswith("_PROXY") for k in os.environ):
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7892"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:7892"
        print("未检测到代理环境变量，先按本机 Pigeon 的 127.0.0.1:7892 试")
    dest = os.path.join(HERE, "asr-models", size)
    from huggingface_hub import snapshot_download
    t0 = time.time()
    path = snapshot_download(repo, local_dir=dest, allow_patterns=PATTERNS)
    print(f"完成：{path}（{time.time() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
