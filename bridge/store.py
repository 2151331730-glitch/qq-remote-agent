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
from contextlib import contextmanager
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
    kind        TEXT NOT NULL DEFAULT 'task',
    profile     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    duration_ms INTEGER,
    exit_code   INTEGER,
    cost_usd    REAL NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    error_kind  TEXT
);

-- 发件箱：结果先落库再发送，发送失败可在重连后补发（不会因为 NapCat 掉线丢结果）
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    created_at  REAL NOT NULL,
    body        TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered_at, id);
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
        with self._db() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c):
        """轻量迁移：老库缺列就补上（CREATE TABLE IF NOT EXISTS 不会改已存在的表）。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        for name, ddl in (("tokens_in", "ALTER TABLE tasks ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0"),
                          ("tokens_out", "ALTER TABLE tasks ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0"),
                          ("error_kind", "ALTER TABLE tasks ADD COLUMN error_kind TEXT"),
                          ("kind", "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'"),
                          # profile：lean（默认，禁 MCP + 工具白名单）或 full（全量）。
                          # 老任务为 NULL = 执行时按当前配置决定。
                          ("profile", "ALTER TABLE tasks ADD COLUMN profile TEXT")):
            if name not in cols:
                c.execute(ddl)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    @contextmanager
    def _db(self):
        """真正关闭连接的上下文：`with conn:` 只提交事务，不会 close，
        长跑时连接和内存会随请求数累积（压测里 RSS 8 分钟涨 16MB 就是它）。"""
        c = self._conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- 写入 ----------

    def enqueue(self, text, source="qq", sender=None, dedup_key=None,
                priority=0, cwd=None, kind="task", profile=None):
        """入队。dedup_key 重复时返回 (已有 task_id, True)。

        kind：task（要动手的活）或 chat（闲聊通道），只影响提示词与会话，不影响排队规则。
        profile：lean / full，决定这个任务加载多少工具与 MCP；None = 执行时取配置默认。
        """
        now = time.time()
        tid = uuid.uuid4().hex[:12]
        with self._db() as c:
            try:
                c.execute(
                    "INSERT INTO tasks(id, source, sender, dedup_key, text, state,"
                    " priority, cwd, kind, profile, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, source, sender, dedup_key, text, "queued", priority, cwd, kind,
                     profile, now))
            except sqlite3.IntegrityError:
                row = c.execute("SELECT id FROM tasks WHERE dedup_key=?", (dedup_key,)).fetchone()
                return (row["id"] if row else None), True
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, tid, "enqueued", json.dumps({"source": source, "text": text[:500],
                                                         "kind": kind}, ensure_ascii=False)))
        return tid, False

    def claim_next(self, exclude_cwds=None):
        """原子地取出下一条排队任务并置为 running。没有任务返回 None。

        exclude_cwds：这些工作目录正被别的 worker 占用，直接跳过——
        比"先取再放回队列"少一轮无谓的状态翻转（压测里这一条让吞吐翻倍）。
        """
        now = time.time()
        exclude = [c for c in (exclude_cwds or []) if c]
        where = "state='queued'"
        params = [now]
        if exclude:
            marks = ",".join("?" * len(exclude))
            where += f" AND (cwd IS NULL OR cwd NOT IN ({marks}))"
            params = [now] + exclude
        with self._db() as c:
            row = c.execute(
                "UPDATE tasks SET state='running', started_at=?, attempts=attempts+1"
                f" WHERE id = (SELECT id FROM tasks WHERE {where}"
                "             ORDER BY priority DESC, created_at LIMIT 1)"
                " RETURNING *", params).fetchone()
            if row is None:
                return None
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, row["id"], "started", None))
            return dict(row)

    def requeue(self, task_id):
        with self._db() as c:
            c.execute("UPDATE tasks SET state='queued', started_at=NULL WHERE id=? AND state='running'",
                      (task_id,))

    def finish(self, task_id, state, result=None, error=None, exit_code=None,
               cost_usd=0.0, duration_ms=None, tokens_in=0, tokens_out=0, error_kind=None):
        assert state in TERMINAL, state
        now = time.time()
        with self._db() as c:
            row = c.execute("SELECT started_at FROM tasks WHERE id=?", (task_id,)).fetchone()
            if duration_ms is None and row and row["started_at"]:
                duration_ms = int((now - row["started_at"]) * 1000)
            c.execute(
                "UPDATE tasks SET state=?, finished_at=?, duration_ms=?, exit_code=?,"
                " cost_usd=cost_usd+?, tokens_in=tokens_in+?, tokens_out=tokens_out+?,"
                " result=?, error=?, error_kind=? WHERE id=?",
                (state, now, duration_ms, exit_code, cost_usd or 0,
                 int(tokens_in or 0), int(tokens_out or 0),
                 (result or "")[:20000], (error or "")[:2000], error_kind, task_id))
            c.execute("INSERT INTO events(ts, task_id, kind, payload) VALUES(?,?,?,?)",
                      (now, task_id, state,
                       json.dumps({"ms": duration_ms, "cost": cost_usd}, ensure_ascii=False)))

    def cancel(self, task_id):
        """取消一条 queued 任务；返回是否真的取消了。"""
        with self._db() as c:
            cur = c.execute("UPDATE tasks SET state='cancelled', finished_at=?"
                            " WHERE id=? AND state='queued'", (time.time(), task_id))
            if cur.rowcount:
                c.execute("INSERT INTO events(ts, task_id, kind) VALUES(?,?,?)",
                          (time.time(), task_id, "cancelled"))
            return bool(cur.rowcount)

    def cancel_all_queued(self):
        with self._db() as c:
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
        with self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE state='running'")]
            if rows:
                c.executemany("UPDATE tasks SET state='interrupted', finished_at=? WHERE id=?",
                              [(now, r["id"]) for r in rows])
                c.execute("INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                          (now, "recovered", json.dumps({"count": len(rows)})))
            return rows

    # ---------- 读取 ----------

    def get(self, task_id):
        with self._db() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def queue_depth(self):
        with self._db() as c:
            return c.execute("SELECT COUNT(*) n FROM tasks WHERE state='queued'").fetchone()["n"]

    def recent(self, limit=30):
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    def stats(self, since_hours=24):
        """总览 + 最近窗口内的时延分位数。"""
        since = time.time() - since_hours * 3600
        with self._db() as c:
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
            tk = c.execute(
                "SELECT COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o FROM tasks"
            ).fetchone()
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
            "tokens_in": tk["i"],
            "tokens_out": tk["o"],
        }

    def events(self, limit=100, task_id=None):
        with self._db() as c:
            if task_id:
                rows = c.execute("SELECT * FROM events WHERE task_id=? ORDER BY ts DESC LIMIT ?",
                                 (task_id, limit))
            else:
                rows = c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    # ---------- 发件箱（结果投递可靠性） ----------

    def outbox_add(self, body, task_id=None):
        with self._db() as c:
            cur = c.execute("INSERT INTO outbox(created_at, task_id, body) VALUES(?,?,?)",
                            (time.time(), task_id, body))
            return cur.lastrowid

    def outbox_pending(self, limit=20):
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY id LIMIT ?", (limit,))]

    def outbox_mark(self, oid, ok, error=None):
        with self._db() as c:
            if ok:
                c.execute("UPDATE outbox SET delivered_at=?, attempts=attempts+1 WHERE id=?",
                          (time.time(), oid))
            else:
                c.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                          ((error or "")[:300], oid))

    def outbox_drop(self, oid, error=None):
        """标记已处理、但以后不再补发。

        闲聊回复属于时效内容：半小时后补发一句旧对话，比丢了更不像人。
        补发通道是 raw `send()`，会绕过 humanize —— 曾经一次重启把 20 条
        带换行和句号的旧回复一口气推给对方。
        """
        with self._db() as c:
            c.execute("UPDATE outbox SET delivered_at=?, attempts=attempts+1, last_error=? "
                      "WHERE id=?", (time.time(), (error or "")[:300], oid))

    def outbox_pending_count(self):
        with self._db() as c:
            return c.execute("SELECT COUNT(*) n FROM outbox WHERE delivered_at IS NULL").fetchone()["n"]

    # ---------- 额度 ----------

    def count_since(self, seconds):
        with self._db() as c:
            return c.execute("SELECT COUNT(*) n FROM tasks WHERE created_at>=?",
                             (time.time() - seconds,)).fetchone()["n"]

    def tokens_since(self, seconds):
        with self._db() as c:
            return c.execute("SELECT COALESCE(SUM(tokens_in+tokens_out),0) v FROM tasks"
                             " WHERE created_at>=?", (time.time() - seconds,)).fetchone()["v"]

    # ---------- 保留策略 ----------

    def prune(self, days=30):
        """删掉过期的终态任务与事件；返回删除条数。"""
        cutoff = time.time() - days * 86400
        with self._db() as c:
            n1 = c.execute("DELETE FROM tasks WHERE state IN ('succeeded','failed','cancelled',"
                           "'interrupted') AND COALESCE(finished_at, created_at)<?",
                           (cutoff,)).rowcount
            n2 = c.execute("DELETE FROM events WHERE ts<?", (cutoff,)).rowcount
            n3 = c.execute("DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at<?",
                           (cutoff,)).rowcount
            return n1 + n2 + n3

    def vacuum(self):
        try:
            c = sqlite3.connect(self.path, timeout=10)
            c.execute("VACUUM")
            c.close()
        except Exception:
            pass

    def daily(self, days=7):
        since = time.time() - days * 86400
        with self._db() as c:
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
