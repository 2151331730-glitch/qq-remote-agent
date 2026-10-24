# -*- coding: utf-8 -*-
"""群聊接话策略的解析与判定。"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
policy = importlib.import_module("policy")


def test_parse_named_policies():
    assert policy.parse_policy("mention") == "mention"
    assert policy.parse_policy("Open") == "open"
    assert policy.parse_policy(None) == "mention"


def test_parse_numbers_clamped():
    assert policy.parse_policy(0.1) == 0.1
    assert policy.parse_policy("0.25") == 0.25
    assert policy.parse_policy(2) == 1.0
    assert policy.parse_policy(-1) == 0.0


def test_parse_invalid_falls_back():
    assert policy.parse_policy("有时候") == "mention"
    assert policy.parse_policy(True) == "mention"
    assert policy.parse_policy("", default="open") == "open"


def test_should_reply_addressed_always():
    assert policy.should_reply("mention", True) is True
    assert policy.should_reply(0.0, True) is True


def test_should_reply_named():
    assert policy.should_reply("mention", False) is False
    assert policy.should_reply("open", False) is True


def test_should_reply_probability():
    assert policy.should_reply(0.0, False) is False
    assert policy.should_reply(1.0, False) is True
    assert policy.should_reply(0.1, False, rng=lambda: 0.05) is True
    assert policy.should_reply(0.1, False, rng=lambda: 0.5) is False
