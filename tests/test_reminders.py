# -*- coding: utf-8 -*-
"""提醒的时间解析与存储语义。"""
import time
import pytest

from reminders import ReminderStore, format_when, parse_reminder


def _local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


NOW = _local(2026, 10, 24, 19, 30)


def test_parse_hhmm_relative_to_now():
    ts, body = parse_reminder("20:30 交作业", now=NOW)
    assert body == "交作业"
    assert ts == _local(2026, 10, 24, 20, 30)


def test_parse_past_time_rolls_to_tomorrow():
    ts, _ = parse_reminder("8:05 起床", now=NOW)
    assert ts == _local(2026, 10, 25, 8, 5)


def test_parse_tomorrow_prefix():
    ts, body = parse_reminder("明天 8:00 开会", now=NOW)
    assert ts == _local(2026, 10, 25, 8, 0) and body == "开会"


def test_parse_date():
    ts, _ = parse_reminder("10-25 20:30 交报告", now=NOW)
    assert ts == _local(2026, 10, 25, 20, 30)


def test_parse_plus_offsets():
    ts, body = parse_reminder("+10m 喝水", now=NOW)
    assert ts == NOW + 600 and body == "喝水"
    ts, _ = parse_reminder("+1h30m 休息", now=NOW)
    assert ts == NOW + 5400


def test_bad_input_raises_with_usage():
    for bad in ("", "20:30", "随便说点什么", "+0m 空", "99:99 x"):
        with pytest.raises(ValueError):
            parse_reminder(bad, now=NOW)


def test_store_lifecycle(tmp_path):
    s = ReminderStore(tmp_path / "r.db")
    rid = s.add("喝水", NOW + 60)
    rid2 = s.add("交作业", NOW + 3600)
    assert [r["id"] for r in s.due(now=NOW)] == []
    due = s.due(now=NOW + 120)
    assert [r["id"] for r in due] == [rid]
    s.mark_sent(rid)
    assert s.due(now=NOW + 120) == []
    pending = s.pending()
    assert [r["id"] for r in pending] == [rid2]
    assert s.cancel(rid2) is True
    assert s.pending() == []
    assert s.cancel(rid2) is False


def test_format_when():
    assert format_when(_local(2026, 10, 24, 20, 30), now=NOW).startswith("今天 20:30")
    assert format_when(_local(2026, 10, 25, 8, 0), now=NOW).startswith("明天 08:00")
    assert format_when(_local(2026, 10, 26, 8, 0), now=NOW).startswith("10-26 08:00")
