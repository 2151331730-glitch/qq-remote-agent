# -*- coding: utf-8 -*-
"""测试脚本读配置的小工具：token 一律从 config.json 读，不写死在代码里。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = Path(os.environ.get("QQ_REMOTE_CONFIG") or (ROOT / "bridge" / "config.json"))


def load_config():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def napcat_token():
    return load_config().get("napcat_token", "")


def panel_token():
    return load_config().get("panel_token", "")
