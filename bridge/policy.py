# -*- coding: utf-8 -*-
"""群里"要不要接话"的策略。

真人不会只在被点名时说话，也不会每句都插嘴。参考 cloakbot 的 group_policy：
- "mention"：只在被 @（或命中前缀）时回
- "open"：每条都回（适合小群，容易被嫌吵）
- 0~1 的数字：被 @ 必回，其他消息按这个概率插一句
"""
import random
import re


def parse_policy(value, default="mention"):
    """把配置里的值归一成 "mention" / "open" / float。非法值退回默认。"""
    if value is None:
        return default
    if isinstance(value, bool):          # bool 是 int 的子类，先挡掉
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    s = str(value).strip().lower()
    if s in ("mention", "open"):
        return s
    if s in ("", "default", "none"):
        return default
    try:
        return max(0.0, min(1.0, float(s)))
    except ValueError:
        return default


def should_reply(policy, addressed: bool, rng=random.random) -> bool:
    """被点名必回；否则按策略决定。"""
    if addressed:
        return True
    if policy == "open":
        return True
    if policy == "mention":
        return False
    try:
        return rng() < float(policy)
    except (TypeError, ValueError):
        return False


# 模型"这句我不接"的暗号。必须卡得很死：整条回复只有 PASS（外面允许包一层
# 括号、引号、句号），否则一句正常的英文里带 pass 也会被当成沉默。
_PASS_RE = re.compile(r"^[\s\"'“”‘’()\[\]（）：:。.!！?？~～【】]*pass"
                      r"[\s\"'“”‘’()\[\]（）：:。.!！?？~～【】]*$", re.I)


def is_pass(text) -> bool:
    """这条回复是不是「没什么好说的」。PASS → 不发言。"""
    return bool(_PASS_RE.match(str(text or "")))


HELLO_LINES = ("在呢", "嗯？咋了", "在的，说呗", "咋了")


def pick_hello(rng=None):
    """被点名却没话说时的一句应声：像真人那样只嗯一声，不解释。"""
    return (rng or random).choice(HELLO_LINES)
