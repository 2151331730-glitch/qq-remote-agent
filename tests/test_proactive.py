# -*- coding: utf-8 -*-
"""主动私聊：时间判定 + "由头不进历史"这两件事。

判定是纯函数，所以全部用固定时间戳，不 sleep、不联网。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import direct  # noqa: E402
import proactive  # noqa: E402


def _ts(y, mo, d, h, mi=0):
    """本地时间的 timestamp（和代码里 time.localtime 的时区一致）。"""
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


NOON = _ts(2026, 10, 27, 12, 0)
CFG = proactive.normalize({"quiet_hours": "23-9"})


class _Rng:
    """确定性随机：uniform 一律返回下界。"""

    def uniform(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]


# ---------------- 时间窗 ----------------

def test_parse_window_forms():
    assert proactive.parse_window("23-9") == (23, 9)
    assert proactive.parse_window("1~7") == (1, 7)
    assert proactive.parse_window(" 8 - 22 ") == (8, 22)
    assert proactive.parse_window("１-７") == (1, 7)    # int() 认全角数字，顺手支持


def test_parse_window_invalid_falls_back():
    assert proactive.parse_window("") is None
    assert proactive.parse_window(None) is None
    assert proactive.parse_window("晚上") is None
    assert proactive.parse_window("25-9") is None


def test_quiet_wraps_midnight():
    win = proactive.parse_window("23-9")
    assert proactive.in_quiet(_ts(2026, 10, 27, 23, 30), win) is True
    assert proactive.in_quiet(_ts(2026, 10, 27, 3, 0), win) is True
    assert proactive.in_quiet(_ts(2026, 10, 27, 8, 59), win) is True
    assert proactive.in_quiet(_ts(2026, 10, 27, 9, 0), win) is False
    assert proactive.in_quiet(_ts(2026, 10, 27, 22, 59), win) is False


def test_quiet_same_day_range():
    win = proactive.parse_window("13-15")
    assert proactive.in_quiet(_ts(2026, 10, 27, 14, 0), win) is True
    assert proactive.in_quiet(_ts(2026, 10, 27, 12, 59), win) is False


# ---------------- 该不该开口 ----------------

def _state(**kw):
    base = {"last_user": NOON, "last_sent": 0,
            "next_at": NOON + 240 * 60, "day": "2026-10-27", "count": 0}
    base.update(kw)
    return base


def test_disabled_never_fires():
    ok, why = proactive.due(NOON + 99999, _state(), {"enabled": False})
    assert ok is False and "关" in why


def test_never_chatted_never_fires():
    ok, why = proactive.due(NOON + 99999, _state(last_user=0), CFG)
    assert ok is False and "还没聊过" == why


def test_not_due_yet():
    ok, why = proactive.due(NOON + 60, _state(next_at=NOON + 3600), CFG)
    assert ok is False and "分钟" in why


def test_fires_when_due():
    st = _state()
    ok, why = proactive.due(st["next_at"] + 1, st, CFG)
    assert ok is True and why == ""


def test_quiet_hours_block_it():
    st = _state(next_at=NOON)
    night = _ts(2026, 10, 28, 2, 0)
    ok, why = proactive.due(night, st, CFG)
    assert ok is False and "免打扰" in why


def test_daily_cap():
    st = _state(next_at=NOON, count=2)
    ok, why = proactive.due(NOON + 3600, st, CFG)
    assert ok is False and "今天" in why


def test_min_gap_blocks_second_message():
    # 到点了，但 10 分钟前刚主动说过，min_gap 是 180 分钟
    st = _state(next_at=NOON, last_sent=NOON - 600)
    ok, why = proactive.due(NOON, st, CFG)
    assert ok is False and "隔够" in why


def test_day_rollover_resets_count():
    st = _state(count=2, day="2026-10-27")
    tomorrow = _ts(2026, 10, 28, 12, 0)
    ok, why = proactive.due(tomorrow, st, CFG)
    assert st["count"] == 0          # 跨天就地清零
    assert ok is True


# ---------------- 计时怎么推进 ----------------

def test_note_user_restarts_timer():
    st = _state()
    nxt = proactive.note_user(st, CFG, now=NOON + 100, rng=_Rng())
    assert st["last_user"] == NOON + 100
    assert nxt == NOON + 100 + 240 * 60      # rng 下界 = 0 抖动


def test_note_sent_counts_and_pushes_back():
    st = _state(count=1)
    nxt = proactive.note_sent(st, CFG, now=NOON, rng=_Rng())
    assert st["count"] == 2 and st["last_sent"] == NOON
    assert nxt == NOON + 240 * 60


def test_jitter_is_bounded():
    class R(_Rng):
        def uniform(self, a, b):
            return b

    st = _state()
    nxt = proactive.note_user(st, proactive.normalize({"jitter_minutes": 30}),
                              now=NOON, rng=R())
    assert nxt == NOON + (240 + 30) * 60


def test_state_roundtrip(tmp_path):
    p = tmp_path / "proactive_state.json"
    st = _state(count=3)
    assert proactive.save(p, st) is True
    assert proactive.load(p) == st


def test_load_missing_returns_empty(tmp_path):
    assert proactive.load(tmp_path / "nope.json") == {}


# ---------------- 由头 ----------------

def test_scratch_tells_time_and_gap():
    text = proactive.build_scratch(NOON, 300, rng=_Rng())
    assert "12:00" in text
    assert "5 小时" in text
    assert "在吗" in text            # 提示词里明确禁止问这句
    assert text.startswith("（") and text.endswith("）")


# ---------------- 由头不进历史 ----------------

class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mkchat(monkeypatch, reply="刚看到个东西想问你"):
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k", "history_turns": 4})
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _Resp(json.dumps({"content": [{"type": "text", "text": reply}],
                                 "usage": {"input_tokens": 10, "output_tokens": 5}},
                                ensure_ascii=False))
    monkeypatch.setattr(direct.urllib.request, "urlopen", fake_urlopen)
    return c, sent


def test_scratch_reaches_model_but_not_history(monkeypatch):
    c, sent = _mkchat(monkeypatch)
    reply, ok = c.ask("owner", "（由头）", "S", store_user=False)
    assert ok and reply == "刚看到个东西想问你"
    # 模型看到了这句
    assert sent[0]["messages"][-1]["content"] == "（由头）"
    # 历史里只有它自己说的那句
    assert [m["role"] for m in c.history_for("owner")] == ["assistant"]


def test_scratch_failure_leaves_history_untouched(monkeypatch):
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})

    def boom(req, timeout=None):
        raise OSError("网络断了")
    monkeypatch.setattr(direct.urllib.request, "urlopen", boom)
    reply, ok = c.ask("owner", "（由头）", "S", store_user=False)
    assert (reply, ok) == ("", False)
    assert c.history_for("owner") == []


def test_normal_ask_still_stores_user_turn(monkeypatch):
    c, _ = _mkchat(monkeypatch)
    c.ask("owner", "在干嘛", "S")
    assert [m["role"] for m in c.history_for("owner")] == ["user", "assistant"]
