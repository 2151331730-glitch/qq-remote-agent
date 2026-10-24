# -*- coding: utf-8 -*-
"""流式进度：stub 发一条 tool_use，桥要能把它转成进度回调。

这个测试直接 import bridge 模块（会读环境变量里的临时配置），
所以必须在改动 env 之后 reload，避免拿到别的测试留下的配置。
"""
import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "stub_claude.py"


@pytest.fixture()
def bridge_mod(tmp_path, monkeypatch):
    cfg = json.loads((ROOT / "bridge" / "config.json").read_text(encoding="utf-8"))
    cfg.update({
        "napcat_ws": "ws://127.0.0.1:1",
        "log_dir": str(tmp_path),
        "db_path": str(tmp_path / "t.db"),
        "panel_port": 3093,
        "stream_progress": True,
        "progress_min_interval": 0,          # 测试里每次都发
        "claude_cmd": [sys.executable, str(STUB)],
        "max_tasks_per_hour": 0,
        "max_tokens_per_day": 0,
    })
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QQ_REMOTE_CONFIG", str(cfg_path))
    monkeypatch.setenv("QQ_REMOTE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("QQ_REMOTE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("STUB_SLEEP", "0.05")
    if "bridge" in sys.modules:
        del sys.modules["bridge"]
    return importlib.import_module("bridge")


def test_stream_progress_and_result(bridge_mod):
    seen = []

    async def on_progress(msg):
        seen.append(msg)

    ok, text, cost, code, tin, tout, kind = asyncio.run(
        bridge_mod.run_claude("测试流式", "tid-stream", str(ROOT), on_progress=on_progress))

    assert ok is True and kind == "ok"
    assert text.startswith("stub-ok")
    assert tin == 1200 and tout == 40
    # stub 发了一条 Read 工具调用，必须被转成进度
    assert any("Read" in m for m in seen), seen


def test_non_stream_mode_still_works(bridge_mod, monkeypatch):
    bridge_mod.STREAM_PROGRESS = False
    ok, text, *rest = asyncio.run(
        bridge_mod.run_claude("普通模式", "tid-json", str(ROOT)))
    assert ok is True and text.startswith("stub-ok")


def test_error_kind_is_cli_error(bridge_mod, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "always")
    ok, text, cost, code, tin, tout, kind = asyncio.run(
        bridge_mod.run_claude("x", "tid-err", str(ROOT)))
    assert ok is False and kind == "cli_error"
