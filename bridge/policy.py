# -*- coding: utf-8 -*-
"""群里"要不要接话"的策略。

真人不会只在被点名时说话，也不会每句都插嘴。参考 cloakbot 的 group_policy：
- "mention"：只在被 @（或命中前缀）时回
- "open"：每条都回（适合小群，容易被嫌吵）
- 0~1 的数字：被 @ 必回，其他消息按这个概率插一句
"""
import random


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
