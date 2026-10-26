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


# ---------------- PASS：模型说自己不接这条 ----------------

def test_is_pass_basic_forms():
    for s in ("PASS", "pass", " Pass ", "（PASS）", "PASS。", "[PASS]", "“PASS”"):
        assert policy.is_pass(s), s


def test_is_pass_rejects_real_text():
    for s in ("pass 这个词也是正常英文", "我觉得 PASS 吧", "", "  ", None,
              "pass the salt", "PASSPORT", "不PASS"):
        assert not policy.is_pass(s), repr(s)


def test_pick_hello_is_one_of_the_short_lines():
    seen = {policy.pick_hello() for _ in range(50)}
    assert seen <= set(policy.HELLO_LINES)
    assert all(len(x) <= 6 for x in policy.HELLO_LINES)
