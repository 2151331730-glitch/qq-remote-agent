# -*- coding: utf-8 -*-
"""agent 任务在 WSL 里执行（没有 WSL 或没装 Linux 侧 CLI 时跳过）。

用一个 Linux 侧假 CLI（~/bin/claude-stub）验证机制：
桥必须把提示词写文件、用 wsl 调起、拿回 stream-json 结果。
"""
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUB_NAME = "claude-stub"


def _wsl_ok():
    if not shutil.which("wsl"):
        return False
    try:
        return subprocess.run(["wsl", "-e", "true"], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _wsl_ok(), reason="本机没有可用的 WSL")


@pytest.fixture()
def bridge_mod(tmp_path, monkeypatch):
    env = dict(os.environ, WSL_UTF8="1")
    home = subprocess.run(["wsl", "-e", "bash", "-lc", "echo $HOME"],
                          capture_output=True, encoding="utf-8", errors="replace",
                          env=env, timeout=20).stdout.strip().splitlines()[-1]
    stub = f"{home}/bin/{STUB_NAME}"
    if subprocess.run(["wsl", "-e", "bash", "-lc", f"test -x {stub}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"WSL 里没有 {stub}（先手工建一个假 CLI）")

    cfg = json.loads((ROOT / "bridge" / "config.example.json").read_text(encoding="utf-8"))
    cfg.update({
        "napcat_ws": "ws://127.0.0.1:1",
        "log_dir": str(tmp_path),
        "db_path": str(tmp_path / "t.db"),
        "workdir": str(ROOT),
        "panel_port": 3091,
        "instance_lock_port": 0,
        "stream_progress": True,
        "progress_min_interval": 0,
        "agent_sandbox": "wsl",
        "agent_sandbox_cmd": [stub],
    })
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QQ_REMOTE_CONFIG", str(p))
    monkeypatch.setenv("QQ_REMOTE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("QQ_REMOTE_STATE", str(tmp_path / "state.json"))
    if "bridge" in sys.modules:
        del sys.modules["bridge"]
    return importlib.import_module("bridge")


def test_agent_runs_inside_wsl(bridge_mod):
    seen = []

    async def on_progress(msg):
        seen.append(msg)

    ok, text, cost, code, tin, tout, kind = asyncio.run(
        bridge_mod.run_claude("沙箱测试", "tid-wsl", str(ROOT), on_progress=on_progress))
    assert ok is True and kind == "ok", (ok, kind, text)
    assert "wsl-stub" in text and "Linux" in text, text
    assert tin == 100 and tout == 5
    assert any("Bash" in m for m in seen), seen


def test_sandbox_probe(bridge_mod):
    assert bridge_mod.check_agent_sandbox() is True
