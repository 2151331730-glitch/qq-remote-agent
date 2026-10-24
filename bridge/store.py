# -*- coding: utf-8 -*-
"""SQLite 持久化任务队列。

为什么不用内存队列（上一版的做法）：
- 进程重启/崩溃后排队中的任务全丢；
- 没有状态机，无法回答"这条任务到底跑完了没有"；
- QQ 重投同一条消息会重复执行，没有幂等键。

设计：
- 单文件 SQLite + WAL，够本地单机用，零外部依赖（对比 Redis 免去部署）。
- 每次操作新开连接（本地文件，开销微秒级），避免跨线程共享连接的问题。
- 状态机：queued → running → succeeded | failed | cancelled | interrupted
- `dedup_key` 唯一索引：同一个 QQ message_id 只会入队一次（至少一次投递 → 恰好一次执行）。
- 启动时把残留的 running 标成 interrupted，由上层通知主人重发。

所有时间用 Unix 秒（REAL），面板/统计按需格式化。
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    sender      TEXT,
    dedup_key   TEXT UNIQUE,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    cwd         TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    duration_ms INTEGER,
    exit_code   INTEGER,
    cost_usd    REAL NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    task_id TEXT,
    kind    TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
"""

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class TaskStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ---------- 写入 ----------

    def enqueue(self, text, source="qq", sender=None, dedup_key=None,
                priority=0, cwd=None):
        """入队。dedup_key 重复时返回 (已有 task_id, True)。"""
        now = time.time()
        tid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO tasks(id, source, sender, dedup_key, text, state,"
                    " priority, cwd, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (tid, source, sender, dedup_key, text, "queued", priority, cwd, now))
            except sqlite3.IntegrityError:
                row = c.execute("SELECT id FROM tasks WHERE dedup_key=?", (dedup_key,)).fetchone()
                return (row["id"] if row else None), True
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, tid, "enqueued", json.dumps({"source": source, "text": text[:500]},
                                                        ensure_ascii=False)))
        return tid, False

    def claim_next(self):
        """原子地取出下一条排队任务并置为 running。没有任务返回 None。"""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "UPDATE tasks SET state='running', started_at=?, attempts=attempts+1"
                " WHERE id = (SELECT id FROM tasks WHERE state='queued'"
                "             ORDER BY priority DESC, created_at LIMIT 1)"
                " RETURNING *", (now,)).fetchone()
            if row is None:
                return None
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, row["id"], "started", None))
            return dict(row)

    def requeue(self, task_id):
        with self._conn() as c:
            c.execute("UPDATE tasks SET state='queued', started_at=NULL WHERE id=? AND state='running'",
                      (task_id,))

    def finish(self, task_id, state, result=None, error=None, exit_code=None,
               cost_usd=0.0, duration_ms=None):
        assert state in TERMINAL, state
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT started_at FROM tasks WHERE id=?", (task_id,)).fetchone()
            if duration_ms is None and row and row["started_at"]:
                duration_ms = int((now - row["started_at"]) * 1000)
            c.execute(
                "UPDATE tasks SET state=?, finished_at=?, duration_ms=?, exit_code=?,"
                " cost_usd=cost_usd+?, result=?, error=? WHERE id=?",
                (state, now, duration_ms, exit_code, cost_usd or 0,
                 (result or "")[:20000], (error or "")[:2000], task_id))
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, task_id, state,
                       json.dumps({"ms": duration_ms, "cost": cost_usd}, ensure_ascii=False)))

    def cancel(self, task_id):
        """取消一条 queued 任务；返回是否真的取消了。"""
        with self._conn() as c:
            cur = c.execute("UPDATE tasks SET state='cancelled', finished_at=?"
                            " WHERE id=? AND state='queued'", (time.time(), task_id))
            if cur.rowcount:
                c.execute("INSERT INTO events(ts, task_id, kind) VALUES(?,?,?)",
                          (time.time(), task_id, "cancelled"))
            return bool(cur.rowcount)

    def cancel_all_queued(self):
        with self._conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM tasks WHERE state='queued'")]
            if ids:
                now = time.time()
                c.executemany("UPDATE tasks SET state='cancelled', finished_at=? WHERE id=?",
                              [(now, i) for i in ids])
                c.execute("INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                          (now, "cancelled_all", json.dumps({"count": len(ids)})))
            return len(ids)

    def recover_interrupted(self):
        """启动时调用：把上次残留的 running 标成 interrupted，返回这些任务。"""
        now = time.time()
        with self._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE state='running'")]
            if rows:
                c.executemany("UPDATE tasks SET state='interrupted', finished_at=? WHERE id=?",
                              [(now, r["id"]) for r in rows])
                c.execute("INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                          (now, "recovered", json.dumps({"count": len(rows)})))
            return rows

    # ---------- 读取 ----------

    def get(self, task_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def queue_depth(self):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM tasks WHERE state='queued'").fetchone()["n"]

    def recent(self, limit=30):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    def stats(self, since_hours=24):
        """总览 + 最近窗口内的时延分位数。"""
        since = time.time() - since_hours * 3600
        with self._conn() as c:
            counts = {r["state"]: r["n"] for r in c.execute(
                "SELECT state, COUNT(*) n FROM tasks GROUP BY state")}
            agg = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
                "       COALESCE(AVG(duration_ms),0) avg_ms"
                " FROM tasks WHERE state IN ('succeeded','failed') AND created_at>=?",
                (since,)).fetchone()
            durs = [r["duration_ms"] for r in c.execute(
                "SELECT duration_ms FROM tasks WHERE state IN ('succeeded','failed')"
                " AND created_at>=? AND duration_ms IS NOT NULL ORDER BY duration_ms", (since,))]
            cost_today = c.execute(
                "SELECT COALESCE(SUM(cost_usd),0) v FROM tasks WHERE created_at>=?",
                (since,)).fetchone()["v"]
            cost_total = c.execute(
                "SELECT COALESCE(SUM(cost_usd),0) v FROM tasks").fetchone()["v"]
        return {
            "counts": counts,
            "done": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "queue": counts.get("queued", 0),
            "window_hours": since_hours,
            "window_runs": agg["n"],
            "window_avg_ms": int(agg["avg_ms"]),
            "window_cost": round(cost_today, 4),
            "p50_ms": _pct(durs, 50),
            "p95_ms": _pct(durs, 95),
            "total_cost": round(cost_total, 4),
        }

    def events(self, limit=100, task_id=None):
        with self._conn() as c:
            if task_id:
                rows = c.execute("SELECT * FROM events WHERE task_id=? ORDER BY ts DESC LIMIT ?",
                                 (task_id, limit))
            else:
                rows = c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def daily(self, days=7):
        since = time.time() - days * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT date(created_at,'unixepoch','localtime') d,"
                "       COUNT(*) n,"
                "       SUM(CASE WHEN state='succeeded' THEN 1 ELSE 0 END) ok,"
                "       COALESCE(SUM(cost_usd),0) cost,"
                "       COALESCE(AVG(duration_ms),0) avg_ms"
                " FROM tasks WHERE created_at>=? GROUP BY d ORDER BY d", (since,))
            return [dict(r) for r in rows]


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1)))))
    return int(sorted_vals[k])
