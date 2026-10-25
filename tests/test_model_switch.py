# -*- coding: utf-8 -*-
"""模型档案的纯函数层（modelprofiles.py）。

这些用例刻意不 import bridge：那边一导入就读真配置、建队列。这里只测
「读 bee 配置 / 探代理端口 / 存取状态」这三件会出错的事。
"""
import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import modelprofiles as mp  # noqa: E402


def _bee(tmp_path, base="https://beeapi.ai", key="sk-test-not-real"):
    p = tmp_path / "settings-bee.json"
    p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": base,
                                     "ANTHROPIC_AUTH_TOKEN": key}}),
                 encoding="utf-8")
    return p


# ---------------- 档案表 ----------------

def test_spec_known_and_case_insensitive():
    assert mp.spec_for("grok")["model"] == "grok-4.7"
    assert mp.spec_for(" GROK ")["provider"] == "bee"
    assert mp.spec_for("flash")["provider"] == "default"


def test_deepseek_alias_maps_to_flash():
    """/model deepseek 和 /model flash 是同一个东西（手机上更顺手）。"""
    assert mp.canonical("DeepSeek") == "flash"
    assert mp.canonical(" 默认 ") == "flash"
    assert mp.canonical("gpt-9") == "gpt-9"          # 未知原样返回，交给 spec_for 判空


def test_only_chat_profiles_exist():
    """任务恒走 DeepSeek，所以档案里不该再有 pro/opus 这种"给任务换模型"的入口。"""
    assert set(mp.MODEL_PROFILES) == {"flash", "grok"}
    assert mp.spec_for("pro") is None
    assert mp.spec_for("opus") is None


def test_spec_unknown():
    assert mp.spec_for("gpt-9") is None
    assert mp.spec_for("") is None


# ---------------- bee 配置读取 ----------------

def test_bee_credentials_ok(tmp_path):
    cred = mp.bee_credentials(_bee(tmp_path, "https://beeapi.ai/"))
    assert cred["base_url"] == "https://beeapi.ai"      # 尾部斜杠去掉，免得拼出 //v1
    assert cred["api_key"] == "sk-test-not-real"


def test_bee_credentials_missing_file(tmp_path):
    assert mp.bee_credentials(tmp_path / "nope.json") == {}


def test_bee_credentials_broken_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert mp.bee_credentials(p) == {}


def test_bee_credentials_without_key(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://beeapi.ai"}}),
                 encoding="utf-8")
    assert mp.bee_credentials(p) == {}


# ---------------- 代理探测 ----------------

def test_proxy_port_default():
    assert mp.proxy_port("http://127.0.0.1:7892") == 7892
    assert mp.proxy_port("garbage") == 7892           # 解析不出来就退回默认


def test_proxy_alive_false_on_closed_port():
    # 9 是 discard 端口，本机没有监听；连不上必须是 False 而不是抛异常
    assert mp.proxy_alive("http://127.0.0.1:9", timeout=0.2) is False


def test_ensure_proxy_no_autostart_does_not_spawn():
    assert mp.ensure_proxy("http://127.0.0.1:9", autostart=False) is False


# ---------------- 状态落盘 ----------------

def test_state_roundtrip(tmp_path):
    p = tmp_path / "model_state.json"
    assert mp.save_state(p, "grok", "grok-4.7", "bee") is True
    assert mp.load_state(p) == {"profile": "grok", "model": "grok-4.7", "provider": "bee"}


def test_state_has_no_secret(tmp_path):
    p = tmp_path / "model_state.json"
    mp.save_state(p, "grok", "grok-4.7", "bee")
    raw = p.read_text(encoding="utf-8")
    assert "key" not in raw.lower() and "token" not in raw.lower()


def test_state_missing_or_broken(tmp_path):
    assert mp.load_state(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("[1,2,3]", encoding="utf-8")
    assert mp.load_state(bad) == {}                   # 不是 dict 就当没有，别让桥起不来


def test_state_save_creates_parent(tmp_path):
    p = tmp_path / "deep" / "dir" / "model_state.json"
    assert mp.save_state(p, "flash", "", "default") is True
    assert p.exists()
