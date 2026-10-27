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


# ---------------- active 模式：攒批 ----------------

def test_parse_policy_accepts_active():
    assert policy.parse_policy("active") == "active"
    assert policy.parse_policy(" ACTIVE ") == "active"


def test_active_never_replies_immediately():
    # active 不是"每条都回"（那是 open），也不是"按概率丢"（那是数字）。
    # 它走攒批那条路，should_reply 不该替它做决定。
    assert policy.should_reply("active", False) is False
    assert policy.should_reply("active", True) is True     # 被叫了照样必回


def test_batch_wait_grows_with_lines():
    # 一条：等 idle；条数越多等越久（但封顶）
    assert policy.batch_wait_ms(1, idle_ms=9000) == 9000
    assert policy.batch_wait_ms(3, idle_ms=9000, step_ms=1500) == 12000
    assert policy.batch_wait_ms(100, idle_ms=9000, max_wait_ms=30000) == 30000


def test_batch_wait_called_is_shorter():
    # 被叫到就得快回，别让人等
    assert policy.batch_wait_ms(1, called=True, called_ms=4000) == 4000
    assert policy.batch_wait_ms(1, called=True) < policy.batch_wait_ms(1, called=False)


def test_batch_ready_holds_in_cooldown():
    lines = ["a：在吗"]
    assert policy.batch_ready(lines, since_last=100, cooldown_seconds=45) is True
    assert policy.batch_ready(lines, since_last=5, cooldown_seconds=45) is False


def test_batch_ready_overridden_when_busy():
    # 群里聊起来了还卡在冷却里，那就是装死：攒够 3 条就破例
    lines = ["a：1", "b：2", "c：3"]
    assert policy.batch_ready(lines, since_last=1, cooldown_seconds=45,
                              override_lines=3) is True


def test_batch_ready_empty():
    assert policy.batch_ready([], since_last=999, cooldown_seconds=45) is False


# ---------------- 响应档位（2026-10-27）----------------

def test_parse_level_clamps_and_defaults():
    assert policy.parse_level(0) == 0
    assert policy.parse_level(3) == 3
    assert policy.parse_level(9) == 3          # 超出上限夹到最高档
    assert policy.parse_level(-2) == 0
    assert policy.parse_level("2") == 2
    assert policy.parse_level("随便") == 0
    assert policy.parse_level(True) == 0       # bool 不是档位
    assert policy.parse_level(None, default=1) == 1
    assert policy.parse_level("", default=1) == 1


def test_keyword_hit():
    assert policy.keyword_hit("小南在吗", ["小南"])
    assert policy.keyword_hit("XiaoNan 你好", ["xiaonan"])   # 英文不分大小写
    assert not policy.keyword_hit("今天吃啥", ["小南"])
    assert not policy.keyword_hit("", ["小南"])
    assert not policy.keyword_hit("随便", [])                 # 空关键词表 = 永不命中
    assert not policy.keyword_hit("随便", ["", "  "])          # 空串不能变成"命中一切"


def test_level_wants_addressed_always_passes():
    # 被 @ / 前缀命中是底线，任何档位都必回（哪怕是 0 档）
    for lvl in range(4):
        assert policy.level_wants(lvl, True, "嗯", []) is True


def test_level_zero_only_mentions():
    assert policy.level_wants(0, False, "小南在吗", ["小南"]) is False
    assert policy.level_wants(0, False, "随便聊聊", []) is False


def test_level_one_keyword_gate():
    # 这一档是省钱的默认：没叫、也没提到名字 → 一次模型调用都不发生
    assert policy.level_wants(1, False, "小南你咋看", ["小南"]) is True
    assert policy.level_wants(1, False, "今天好热", ["小南"]) is False


def test_level_two_adds_probability():
    kw = ["小南"]
    assert policy.level_wants(2, False, "小南呢", kw,
                              probability=0.0) is True      # 关键词命中优先
    assert policy.level_wants(2, False, "今天好热", kw,
                              probability=0.5, rng=lambda: 0.2) is True
    assert policy.level_wants(2, False, "今天好热", kw,
                              probability=0.5, rng=lambda: 0.9) is False
    # 概率写坏/越界不能变成"每条都接"
    assert policy.level_wants(2, False, "今天好热", kw,
                              probability=5, rng=lambda: 0.99) is True
    assert policy.level_wants(2, False, "今天好热", kw,
                              probability="乱写", rng=lambda: 0.0) is False


def test_level_three_everything():
    assert policy.level_wants(3, False, "今天好热", []) is True
    assert policy.level_wants(3, False, "", []) is True
