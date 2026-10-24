# -*- coding: utf-8 -*-
"""提示词拼装：任务模板 / 闲聊模板 + 人格 + 长期记忆。

bridge.py 在 import 时读配置、建队列，所以这里先用环境变量把路径指向临时目录，
再首次导入（同一个进程里只会真正加载一次）。
"""
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))


def _load(tmp_path, monkeypatch):
    cfg = json.loads((BRIDGE / "config.example.json").read_text(encoding="utf-8"))
    persona = tmp_path / "persona.md"
    persona.write_text("PERSONA-X 你是测试人格。", encoding="utf-8")
    public_persona = tmp_path / "public_persona.md"
    public_persona.write_text("PUBLIC-PERSONA 你只能聊天。", encoding="utf-8")
    mem = tmp_path / "memory.md"
    mem.write_text("# 标题行\n- 2026-10-24 该用户喜欢喝美式\n", encoding="utf-8")
    cfg.update({
        "claude_exe": "claude", "workdir": str(tmp_path), "log_dir": str(tmp_path / "logs"),
        "db_path": str(tmp_path / "t.db"), "persona_path": str(persona),
        "memory_path": str(mem), "chat_prompt_template": "",
        "public_persona_path": str(public_persona),
        "task_prompt_template": "任务：{task}", "instance_lock_port": 0,
        "panel_port": 0, "owner_uin": 0,
    })
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QQ_REMOTE_CONFIG", str(cfg_path))
    monkeypatch.setenv("QQ_REMOTE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("QQ_REMOTE_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("QQ_REMOTE_REMINDERS", str(tmp_path / "reminders.db"))
    monkeypatch.setenv("QQ_REMOTE_NO_LOCK", "1")
    sys.modules.pop("bridge", None)
    return importlib.import_module("bridge")


def test_task_prompt_has_no_persona_or_memory(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    p = b.build_prompt("task", "帮我列目录")
    assert p == "任务：帮我列目录"


def test_chat_prompt_injects_persona_then_memory_then_text(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    p = b.build_prompt("chat", "在吗")
    assert p.startswith("PERSONA-X")
    assert "该用户喜欢喝美式" in p
    assert p.rstrip().endswith("在吗")
    assert p.index("PERSONA-X") < p.index("该用户喜欢喝美式") < p.index("在吗")
    # 标题行不应该被塞进提示词，白花 token
    assert "标题行" not in p


def test_chat_prompt_includes_history_before_text(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    p = b.build_prompt("chat", "在吗", history="甲: 晚上吃啥")
    assert "甲: 晚上吃啥" in p
    assert p.index("甲: 晚上吃啥") < p.index("在吗")


def test_task_prompt_ignores_history(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    p = b.build_prompt("task", "帮我列目录", history="甲: 晚上吃啥")
    assert p == "任务：帮我列目录"


def test_public_system_comes_from_file(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    assert b.load_public_system().startswith("PUBLIC-PERSONA")


def test_public_system_falls_back_when_file_missing(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    b.PUBLIC_PERSONA_PATH = tmp_path / "missing.md"
    text = b.load_public_system()
    assert "只能聊天" in text and "PUBLIC-PERSONA" not in text


def test_should_reset_chat_session_before_running(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    b.STATE["chat_session_id"] = "s1"
    b.STATE["chat_last_tokens_in"] = b.CHAT_SESSION_LIMIT + 1
    assert b.should_reset_session("chat") is True
    b.STATE["chat_last_tokens_in"] = 100
    assert b.should_reset_session("chat") is False
    b.STATE["chat_session_id"] = ""
    assert b.should_reset_session("chat") is False


def test_reload_config_hot_updates_group_policy(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    cfg = json.loads(b.CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["group_policy"] = 0.3
    b.CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    info = b.reload_config()
    assert info["group_policy"] == 0.3
    assert b.GROUP_POLICY == 0.3
