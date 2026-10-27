# -*- coding: utf-8 -*-
"""主动私聊：主人很久没说话时，自己先开个话头。

和群聊的"攒批"是两件事：那边是别人说话时我插不插嘴，这边是没人理我时我开不开口。

实现上刻意"一次算好下次时间"（next_at），不在每个巡检里掷骰子：
- 判定只看一个时间点，日志能说清"为什么发 / 为什么不发"
- 重启后行为一致，不会因为重启就跳过，也不会因为重启就提前补一句
- 随机量只在算 next_at 时抽一次并落盘，同一天不会忽早忽晚

状态文件（默认 bridge/proactive_state.json）：
    {"last_user": 1729..., "last_sent": ..., "next_at": ...,
     "day": "2026-10-27", "count": 1}
"""
import json
import os
import random
import time
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "idle_minutes": 240,       # 主人静默多久之后才考虑开口
    "jitter_minutes": 60,      # 到点后再随机拖 0~这么多分钟，别每天都卡在同一分钟
    "min_gap_minutes": 180,    # 两次主动之间至少隔这么久
    "max_per_day": 2,          # 一天最多主动几次——他不理你，你也别刷屏
    "quiet_hours": "23-9",     # 这段时间不开口，可以跨零点；写空串等于没有免打扰
    "check_seconds": 60,       # 巡检间隔
}


def normalize(cfg=None) -> dict:
    """把配置里这一段补全成完整字典；None 值当没写（用默认）。"""
    out = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            out[k] = v
    return out


def parse_window(s):
    """'23-9' → (23, 9)。写不出两个数字就返回 None（视为没有免打扰）。

    分隔符 - 和 ~ 都认；全角冒号是常见手误，先归一掉。
    """
    if not s:
        return None
    txt = str(s).strip().replace("：", ":").replace("—", "-").replace("－", "-")
    for sep in ("-", "~"):
        if sep in txt:
            a, _, b = txt.partition(sep)
            try:
                start, end = int(a.strip()), int(b.strip())
            except ValueError:
                return None
            if not (0 <= start <= 24 and 0 <= end <= 24):
                return None
            return start, end
    return None


def in_quiet(now, window) -> bool:
    """现在是不是免打扰时段。start > end 表示跨零点（23-9 就是 23:00~08:59）。"""
    if not window:
        return False
    start, end = window
    if start == end:
        return False
    hour = time.localtime(now).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _roll_day(st: dict, now: float) -> str:
    """跨天就把当日计数清零，返回今天。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if st.get("day") != today:
        st["day"] = today
        st["count"] = 0
    return today


def _plan(st: dict, cfg: dict, now: float, rng=None) -> float:
    """算下一次最早能开口的时间：静默时长 +（可选）随机延迟。"""
    r = rng or random
    idle = max(0.0, float(cfg["idle_minutes"])) * 60
    jitter = max(0.0, float(cfg["jitter_minutes"])) * 60
    extra = r.uniform(0, jitter) if jitter else 0.0
    st["next_at"] = now + idle + extra
    return st["next_at"]


def due(now: float, st: dict, cfg=None) -> tuple[bool, str]:
    """现在该不该主动开口。返回 (要不要发, 不发时的一句话原因)。"""
    c = normalize(cfg)
    if not c["enabled"]:
        return False, "关着"
    if float(st.get("last_user") or 0) <= 0:
        return False, "还没聊过"
    if in_quiet(now, parse_window(c["quiet_hours"])):
        return False, "免打扰时段"
    _roll_day(st, now)                      # 跨零点后计数清零（可能刚过 00:00）
    if int(st.get("count") or 0) >= int(c["max_per_day"]):
        return False, f"今天已经主动 {st['count']} 次"
    next_at = float(st.get("next_at") or 0)
    if next_at <= 0:
        return False, "还没排上时间"
    if now < next_at:
        return False, f"还差 {(next_at - now) / 60:.0f} 分钟"
    last_sent = float(st.get("last_sent") or 0)
    gap = max(0, int(c["min_gap_minutes"])) * 60
    if last_sent and now - last_sent < gap:
        return False, "上一次主动还没隔够"
    return True, ""


def note_user(st: dict, cfg=None, now: float | None = None, rng=None) -> float:
    """主人说话了：从现在重新计时。返回新的 next_at。

    任何一条主人消息都算（包括命令）——他在用这台机器，就不该再主动打扰。
    """
    c = normalize(cfg)
    now = time.time() if now is None else now
    _roll_day(st, now)
    st["last_user"] = now
    return _plan(st, c, now, rng)


def note_sent(st: dict, cfg=None, now: float | None = None, rng=None) -> float:
    """刚主动发过一条：记下来，并把下一次往后推。"""
    c = normalize(cfg)
    now = time.time() if now is None else now
    _roll_day(st, now)
    st["last_sent"] = now
    st["count"] = int(st.get("count") or 0) + 1
    return _plan(st, c, now, rng)


_HINTS = (
    "提一件你们之前聊过的事，问一句后来怎么样了",
    "说一件你刚想到的、跟他有点关系的小事",
    "问一句具体的（在弄什么、吃了什么），别问「在吗」",
    "吐槽一句你自己刚遇到的事，让他接话",
)


def build_scratch(now: float, elapsed_minutes: float, rng=None) -> str:
    """拼那一行"由头"，代替主人没发来的消息。

    走 user 角色发出去（Anthropic 格式要求最后一条是 user），但**不写进历史**
    （`DirectChat.ask(store_user=False)`）——否则下一轮它会把这段括号当成
    主人说过的开场白来模仿，越聊越像两个机器人在念提示词。
    """
    r = rng or random
    m = max(0, int(elapsed_minutes))
    span = f"{m // 60} 小时" if m >= 60 else f"{m} 分钟"
    if m >= 60 and m % 60:
        span += f" {m % 60} 分钟"
    when = time.strftime("%H:%M", time.localtime(now))
    return (
        f"（现在 {when}，主人已经 {span} 没跟你说话了。没人问你，是你自己想先开口。"
        "只说一句，别问「在吗」，别提现在几点，别说「好久没聊」，别问「吃饭了吗」。"
        f"{r.choice(_HINTS)}。）"
    )


def load(path) -> dict:
    """读状态；文件不存在或坏了都返回空字典（当第一次运行）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(path, st: dict) -> bool:
    """原子写（临时文件 + os.replace），断电不会留下半个 json。"""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        return False
