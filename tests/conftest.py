# -*- coding: utf-8 -*-
"""pytest 公共配置：把 bridge 目录放进 sys.path，测试直接 import 模块。

## 为什么要在导入前准备一份配置

`bridge.py` 是模块级读配置的（`with open(CONFIG_PATH)`），也就是说
**import 它就会去读 config.json**。而 `config.json` 含 token、在 `.gitignore` 里，
CI 上根本不存在 —— 于是"导入失败"会被 pytest 报成测试失败，看起来像代码坏了。

本地一直绿是因为本地有真配置。这类"只在 CI 才炸"的问题已经踩过两次
（另一次是闲聊通道的回退分支本地从未被执行），所以这里统一处理：
没有真配置就生成一份**指向 tmp 目录**的最小配置，测试永远是隔离的。

真配置存在时不动它 —— 但注意这意味着本地跑的路径和 CI 不完全一样，
端到端测试（mock_test5/6）会打印用的是哪份配置来提醒这一点。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))


def _ensure_config() -> None:
    """没有 QQ_REMOTE_CONFIG 且没有 bridge/config.json 时，造一份临时的。"""
    if os.environ.get("QQ_REMOTE_CONFIG"):
        return
    if (BRIDGE / "config.json").exists():
        return            # 本地开发：用真配置，和 CI 的差异由端到端测试显式提示
    example = BRIDGE / "config.example.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="qq-remote-test-"))
    data.update({
        "log_dir": str(tmp / "logs"),
        "db_path": str(tmp / "tasks.db"),
        "workdir": str(tmp),
        # 0 会被 bridge 解释成"我不占端口"；用一个高位端口避免和真机冲突
        "panel_port": 3098,
        "napcat_token": "test-token",
        "panel_token": "test-panel",
    })
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.environ["QQ_REMOTE_CONFIG"] = str(cfg)
    os.environ["QQ_REMOTE_STATE"] = str(tmp / "state.json")
    os.environ["QQ_REMOTE_DB"] = str(tmp / "tasks.db")


_ensure_config()
