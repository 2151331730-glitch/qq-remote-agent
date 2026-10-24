# -*- coding: utf-8 -*-
"""pytest 公共配置：把 bridge 目录放进 sys.path，测试直接 import 模块。"""
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
