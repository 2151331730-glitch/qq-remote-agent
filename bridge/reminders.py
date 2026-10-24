# -*- coding: utf-8 -*-
"""提醒事项：时间解析 + 本地 SQLite 存储。

为什么单独一个库：任务队列可能换成 Redis 后端（多进程/多机），但提醒是本机个人
日程，和队列后端无关；独立文件也避免动已经有 70 多个测试覆盖的 tasks.db。

支持的时间写法：
    /提醒 20:30 交作业          → 今天或明天 20:30（过了就顺延）
    /提醒 明天 8:00 开会
    /提醒 10-25 20:30 交报告
    /提醒 2026-10-25 09:00 出发
    /提醒 +10m 喝水             → 10 分钟后（m/h/d，可叠加 1h30m）
"""
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    fire_at       REAL NOT NULL,
    text          TEXT NOT NULL,
    sent_at       REAL,
    cancelled_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent_at, cancelled_at, fire_at);
"""

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _mktime(y, mo, d, h=0, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def _valid_hm(h, mi):
    return 0 <= h <= 23 and 0 <= mi <= 59


def parse_reminder(raw: str, now: float | None = None):
    """解析 "/提醒" 后面的内容，返回 (fire_at, 文本)。写错时抛 ValueError（含用法提示）。"""
    now = time.time() if now is None else now
    s = (raw or "").strip()
    if not s:
        raise ValueError("用法：/提醒 20:30 交作业，或 /提醒 +10m 喝水，或 明天 8:00 开会")

    m = re.match(r"^\+\s*((?:\d+\s*[smhdwSMHDW]\s*)+)(.*)$", s)
    if m:
        secs = sum(int(n) * _UNITS[u.lower()]
                   for n, u in re.findall(r"(\d+)\s*([smhdwSMHDW])", m.group(1)))
        body = m.group(2).strip()
        if secs <= 0:
            raise ValueError("时间要大于 0 分钟")
        if not body:
            raise ValueError("提醒内容不能为空，例：/提醒 +10m 喝水")
        return now + secs, body

    rest = s
    day_off = 0
    for word, off in (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)):
        if rest.startswith(word):
            day_off = off
            rest = rest[len(word):].strip()
            break

    lt = time.localtime(now)

    # 完整日期：2026-10-25 20:30 / 10-25 20:30 / 10/25 09:00
    m = re.match(r"^(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})"
                 r"(?:\s+(\d{1,2})[:：](\d{2}))?\s*(.*)$", rest)
    if m:
        year = int(m.group(1) or lt.tm_year)
        mon, day = int(m.group(2)), int(m.group(3))
        h = int(m.group(4)) if m.group(4) else 9
        mi = int(m.group(5)) if m.group(5) else 0
        body = (m.group(6) or "").strip()
        if not _valid_hm(h, mi):
            raise ValueError("时间不对，例：/提醒 10-25 20:30 交报告")
        try:
            ts = _mktime(year, mon, day, h, mi)
        except (OverflowError, ValueError):
            raise ValueError("日期不对，例：/提醒 10-25 20:30 交报告")
        # 校验回读，挡掉 2 月 30 日这种会被 mktime 悄悄进位的日期
        back = time.localtime(ts)
        if (back.tm_mon, back.tm_mday) != (mon, day):
            raise ValueError("日期不存在，例：/提醒 10-25 20:30 交报告")
        if not body:
            raise ValueError("提醒内容不能为空，例：/提醒 20:30 交作业")
        return ts, body

    # 只有时间：20:30
    m = re.match(r"^(\d{1,2})[:：](\d{2})\s*(.*)$", rest)
    if m:
        h, mi, body = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if not _valid_hm(h, mi):
            raise ValueError("时间不对，例：/提醒 20:30 交作业")
        ts = _mktime(lt.tm_year, lt.tm_mon, lt.tm_mday + day_off, h, mi)
        # 没写"明天"且时间已经过了 → 顺延到明天
        if day_off == 0 and ts <= now + 30:
            ts = _mktime(lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, mi)
        if not body:
            raise ValueError("提醒内容不能为空，例：/提醒 20:30 交作业")
        return ts, body

    raise ValueError("没看懂时间。例：/提醒 20:30 交作业 / 明天 8:00 开会 / +10m 喝水")


def human_delta(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return "不到 1 分钟"
    if s < 3600:
        return f"{s // 60} 分钟"
    if s < 86400:
        h, m = s // 3600, (s % 3600) // 60
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    return f"{s // 86400} 天"


def format_when(ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    lt, ln = time.localtime(ts), time.localtime(now)
    # 按本地日历天算差值（不能直接用 86400 整除：跨时区/夏令时会差一天）
    days = int(round((_mktime(lt.tm_year, lt.tm_mon, lt.tm_mday)
                      - _mktime(ln.tm_year, ln.tm_mon, ln.tm_mday)) / 86400))
    hm = time.strftime("%H:%M", lt)
    if days == 0:
        return f"今天 {hm}（{human_delta(ts - now)}后）"
    if days == 1:
        return f"明天 {hm}（{human_delta(ts - now)}后）"
    return f"{time.strftime('%m-%d', lt)} {hm}（{human_delta(ts - now)}后）"


class ReminderStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    @contextmanager
    def _db(self):
        c = self._conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def add(self, text, fire_at):
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO reminders(created_at, fire_at, text) VALUES(?,?,?)",
                (time.time(), float(fire_at), text.strip()))
            return cur.lastrowid

    def due(self, now=None):
        now = time.time() if now is None else now
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL"
                " AND fire_at<=? ORDER BY fire_at", (now,))]

    def pending(self, limit=20):
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL"
                " ORDER BY fire_at LIMIT ?", (limit,))]

    def mark_sent(self, rid):
        with self._db() as c:
            c.execute("UPDATE reminders SET sent_at=? WHERE id=? AND sent_at IS NULL",
                      (time.time(), rid))

    def cancel(self, rid):
        with self._db() as c:
            cur = c.execute(
                "UPDATE reminders SET cancelled_at=? WHERE id=? AND sent_at IS NULL"
                " AND cancelled_at IS NULL", (time.time(), rid))
            return bool(cur.rowcount)

    def get(self, rid):
        with self._db() as c:
            row = c.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
            return dict(row) if row else None

    def prune(self, days=90):
        cutoff = time.time() - days * 86400
        with self._db() as c:
            return c.execute(
                "DELETE FROM reminders WHERE fire_at<? AND (sent_at IS NOT NULL OR"
                " cancelled_at IS NOT NULL)", (cutoff,)).rowcount
