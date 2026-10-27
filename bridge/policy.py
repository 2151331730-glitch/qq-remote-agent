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
    """把配置里的值归一成 "mention" / "open" / "active" / float。非法值退回默认。

    "active" 是 2026-10-26 加的：不再按概率丢掉没被 @ 的消息，而是攒成一批交给
    模型，由它自己决定接不接（回 PASS 就是不接）。参考 qq-agent-plugins 的做法：
    按概率抽样会漏掉话题，攒批让模型判断既活跃又不会每条都开口。
    """
    if value is None:
        return default
    if isinstance(value, bool):          # bool 是 int 的子类，先挡掉
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    s = str(value).strip().lower()
    if s in ("mention", "open", "active"):
        return s
    if s in ("", "default", "none"):
        return default
    try:
        return max(0.0, min(1.0, float(s)))
    except ValueError:
        return default


def should_reply(policy, addressed: bool, rng=random.random) -> bool:
    """被点名必回；否则按策略决定。

    "active" 在这里一律返回 False：没被 @ 的消息要走攒批那条路（见 batch_ready），
    不是"立刻回"，也不该被当成"按概率丢掉"。
    """
    if addressed:
        return True
    if policy == "open":
        return True
    if policy in ("mention", "active"):
        return False
    try:
        return rng() < float(policy)
    except (TypeError, ValueError):
        return False


# ---------------- 攒批（active 模式） ----------------
# 为什么要攒：群里一秒钟能冒三条，逐条各调一次模型既贵又抢不上话。
# 攒成一批一次问，模型看着上下文决定插哪句，比按概率抽一条准得多。

def batch_wait_ms(count, called=False, idle_ms=9000, called_ms=4000,
                  max_wait_ms=30000, step_ms=1500) -> int:
    """这批再等多久才拿去看。

    被叫到就等短的（回慢了没人等你），没被叫就多等一会儿（别抢话、也能多攒几条）；
    条数越多等得越久，但封顶——攒太久话头就过了，省下的 token 不值当。
    """
    base = called_ms if called else idle_ms
    wait = base + max(0, int(count) - 1) * step_ms
    return int(min(wait, max_wait_ms))


def batch_ready(lines, since_last, cooldown_seconds, override_lines=3) -> bool:
    """现在能不能把这一批拿去问模型。

    刚说过话就先晾着（真人不会一句接一句），但攒够 override_lines 条就破例——
    群里聊起来了你还卡在冷却里，那就是装死。
    """
    if not lines:
        return False
    if since_last >= cooldown_seconds:
        return True
    return len(lines) >= max(1, int(override_lines))


# 模型"这句我不接"的暗号。必须卡得很死：整条回复只有 PASS（外面允许包一层
# 括号、引号、句号），否则一句正常的英文里带 pass 也会被当成沉默。
_PASS_RE = re.compile(r"^[\s\"'“”‘’()\[\]（）：:。.!！?？~～【】]*pass"
                      r"[\s\"'“”‘’()\[\]（）：:。.!！?？~～【】]*$", re.I)


def is_pass(text) -> bool:
    """这条回复是不是「没什么好说的」。PASS → 不发言。"""
    return bool(_PASS_RE.match(str(text or "")))


# ---------------- 响应档位（2026-10-27） ----------------
# 参考 QQ-agent 的"响应档位滑条"。档位决定"什么样的消息才值得跑一次模型"：
# 最省的那档，没被叫的消息在**进模型之前**就被丢掉 —— 省的是整次请求，不是几个 token。
# 而且这不只是省钱：真人本来就不接每一句，少开口反而更像人、更不容易被判定成机器人。
LEVELS = (
    "只回 @（或前缀）",
    "@ + 关键词",
    "@ + 关键词 + 概率插话",
    "每条都回",
)


def parse_level(value, default=0) -> int:
    """把配置里的档位归一成 0~3。没写 / 写坏都退回默认档。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):          # bool 是 int 的子类，先挡掉
        return default
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(len(LEVELS) - 1, n))


def keyword_hit(text, keywords=()) -> bool:
    """关键词命中算不算"在叫我"。英文不分大小写，中文直接子串。"""
    t = (text or "").lower()
    if not t:
        return False
    for k in keywords or ():
        k = str(k or "").strip().lower()
        if k and k in t:
            return True
    return False


def level_wants(level, addressed: bool, text: str, keywords=(),
                probability=0.0, rng=random.random) -> bool:
    """这一条消息要不要交给模型。

    被 @ / 命中前缀一律 True（应声是底线）；其余按档位：
    0 不接、1 只认关键词、2 关键词或概率、3 全接。
    返回 False 时调用方必须直接 return —— 不能"先调模型再让它说 PASS"，
    那样钱已经花了，档位就白设了。
    """
    if addressed:
        return True
    lvl = parse_level(level)
    if lvl == 0:
        return False
    if keyword_hit(text, keywords):
        return True
    if lvl == 1:
        return False
    if lvl == 2:
        try:
            p = max(0.0, min(1.0, float(probability)))
        except (TypeError, ValueError):
            p = 0.0
        return rng() < p
    return True


HELLO_LINES = ("在呢", "嗯？咋了", "在的，说呗", "咋了")


def pick_hello(rng=None):
    """被点名却没话说时的一句应声：像真人那样只嗯一声，不解释。"""
    return (rng or random).choice(HELLO_LINES)
