# -*- coding: utf-8 -*-
"""Redis 版任务队列：与 store.TaskStore 同接口，用于多进程/多机共享队列。

为什么需要它：SQLite 版是**单机单进程**的（文件锁跨机器不通用）。把队列放到 Redis 后，
多台机器上的 bridge 可以共同消费同一个队列，靠 Lua 脚本保证"同一条任务只被一个 worker 抢到"。

数据结构：
    qq:tasks      HASH   id -> 任务 JSON（状态机全在里面）
    qq:queue      ZSET   member=id, score=-priority*1e12+created_at（优先级高、先来先出）
    qq:running    SET    正在执行的任务 id
    qq:dedup      HASH   dedup_key -> id（幂等）
    qq:recent     LIST   最近任务 id（LPUSH + LTRIM）
    qq:events     LIST   事件流（LPUSH + LTRIM）
    qq:durations  ZSET   member=id, score=duration_ms（算分位数，保留最近 2000 条）
    qq:stats      HASH   计数器（total/succeeded/failed/cancelled/interrupted/queue/running）
    qq:daily      HASH   key=YYYY-MM-DD, value=JSON{count,ok,cost}

一致性：claim 用 Lua 脚本做"检查状态 + 改状态 + 出队"的原子操作，
避免两个 worker 同时抢到同一条（这是分布式队列最容易踩的坑）。
"""
import json
import time
import uuid

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

CLAIM_LUA = """
local now = tonumber(ARGV[1])
local max = tonumber(ARGV[2])
local excl = {}
for i = 3, #ARGV do excl[ARGV[i]] = true end
local ids = redis.call('ZRANGE', KEYS[1], 0, max - 1)
for _, id in ipairs(ids) do
    local blob = redis.call('HGET', KEYS[2], id)
    if blob then
        local t = cjson.decode(blob)
        if t.state == 'queued' and not (t.cwd and excl[t.cwd]) then
            t.state = 'running'
            t.started_at = now
            t.attempts = (t.attempts or 0) + 1
            redis.call('HSET', KEYS[2], id, cjson.encode(t))
            redis.call('ZREM', KEYS[1], id)
            redis.call('SADD', KEYS[3], id)
            return cjson.encode(t)
        end
    else
        redis.call('ZREM', KEYS[1], id)
    end
end
return nil
"""

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class RedisStore:
    def __init__(self, url="redis://127.0.0.1:6380/0", prefix="qq",
                 sentinel_nodes=None, master_name="mymaster"):
        """两种连法：
        - 直连：url="redis://host:port/db"
        - 哨兵：sentinel_nodes=[("127.0.0.1", 26380), ...] + master_name，
          主库挂了哨兵会选新主，客户端下一次请求自动切过去。
        """
        # protocol=2：redis-py 8 默认 RESP3（HELLO 3），Redis < 6.0 不认
        self._sentinel_nodes = [tuple(n) for n in (sentinel_nodes or [])]
        self._master_name = master_name
        if self._sentinel_nodes:
            # 不用 redis.sentinel.Sentinel：它不接受 protocol=2，会对 Redis 5 发 HELLO 3。
            # 自己做"问哨兵要主库地址 → 直连"，断开时重新发现。
            self._connect_master()
        else:
            self.r = redis.Redis.from_url(url, decode_responses=True, protocol=2,
                                          socket_connect_timeout=1,
                                          retry=Retry(NoBackoff(), 0))
        self.p = prefix
        self._claim = self.r.register_script(CLAIM_LUA)
        self.r.ping()

    # ---------- 哨兵：主库发现与重连 ----------

    def _discover_master(self):
        last = None
        for host, port in self._sentinel_nodes:
            try:
                s = redis.Redis(host=host, port=int(port), decode_responses=True,
                                protocol=2, socket_timeout=2)
                addr = s.execute_command("SENTINEL", "get-master-addr-by-name", self._master_name)
                if addr:
                    return addr[0], int(addr[1])
            except Exception as e:
                last = e
        raise ConnectionError(f"哨兵都不可用: {last}")

    def _connect_master(self):
        host, port = self._discover_master()
        self.master_addr = (host, port)
        # 连接超时必须短：主库刚挂时，卡在死地址上会拖慢整个切换
        # retry=0：关掉 redis-py 内置的指数退避（默认 3 次≈7s），
        # 故障转移时由我们的 _resilient 立即重连，切换时间从 ~15s 降到 1s 级。
        self.r = redis.Redis(host=host, port=port, decode_responses=True, protocol=2,
                             socket_timeout=3, socket_connect_timeout=1,
                             retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
        self._claim = self.r.register_script(CLAIM_LUA)

    def _resilient(fn):  # noqa: N805（装饰器）
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
                if not self._sentinel_nodes:
                    raise
                # 主库挂了：重新问哨兵，再试一次
                self._connect_master()
                return fn(self, *args, **kwargs)
            except redis.exceptions.ResponseError as e:
                # 故障转移窗口里会短暂连到"已经降级为从库"的旧主，写操作报 READONLY
                if not self._sentinel_nodes or "READONLY" not in str(e):
                    raise
                self._connect_master()
                return fn(self, *args, **kwargs)
        return wrapper

    # ---------- key 工具 ----------
    def _k(self, name):
        return f"{self.p}:{name}"

    # ---------- 写入 ----------

    def enqueue(self, text, source="qq", sender=None, dedup_key=None,
                priority=0, cwd=None, kind="task", profile=None):
        tid = uuid.uuid4().hex[:12]
        if dedup_key:
            if not self.r.hsetnx(self._k("dedup"), dedup_key, tid):
                return self.r.hget(self._k("dedup"), dedup_key), True
        now = time.time()
        task = {
            "id": tid, "source": source, "sender": sender, "dedup_key": dedup_key,
            "text": text, "state": "queued", "priority": priority, "cwd": cwd,
            "kind": kind, "profile": profile,
            "attempts": 0, "created_at": now, "started_at": None, "finished_at": None,
            "duration_ms": None, "exit_code": None, "cost_usd": 0,
            "tokens_in": 0, "tokens_out": 0, "result": None, "error": None,
        }
        pipe = self.r.pipeline()
        pipe.hset(self._k("tasks"), tid, json.dumps(task, ensure_ascii=False))
        pipe.zadd(self._k("queue"), {tid: -priority * 1e12 + now})
        pipe.lpush(self._k("recent"), tid)
        pipe.ltrim(self._k("recent"), 0, 199)
        pipe.hincrby(self._k("stats"), "queue", 1)
        pipe.zadd(self._k("created"), {tid: now})
        pipe.lpush(self._k("events"), json.dumps(
            {"ts": now, "task_id": tid, "kind": "enqueued",
             "payload": {"source": source, "text": text[:500]}}, ensure_ascii=False))
        pipe.ltrim(self._k("events"), 0, 4999)
        pipe.execute()
        return tid, False

    def claim_next(self, exclude_cwds=None):
        raw = self._claim(keys=[self._k("queue"), self._k("tasks"), self._k("running")],
                          args=[time.time(), 200] + list(exclude_cwds or []))
        if not raw:
            return None
        task = json.loads(raw)
        self.r.hincrby(self._k("stats"), "running", 1)
        self.r.hincrby(self._k("stats"), "queue", -1)
        self.r.lpush(self._k("events"), json.dumps(
            {"ts": time.time(), "task_id": task["id"], "kind": "started"}, ensure_ascii=False))
        return task

    def finish(self, task_id, state, result=None, error=None, exit_code=None,
               cost_usd=0.0, duration_ms=None, tokens_in=0, tokens_out=0):
        assert state in TERMINAL, state
        raw = self.r.hget(self._k("tasks"), task_id)
        if not raw:
            return
        t = json.loads(raw)
        now = time.time()
        if duration_ms is None and t.get("started_at"):
            duration_ms = int((now - t["started_at"]) * 1000)
        t.update({"state": state, "finished_at": now, "duration_ms": duration_ms,
                  "exit_code": exit_code, "cost_usd": float(cost_usd or 0),
                  "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                  "result": (result or "")[:20000], "error": (error or "")[:2000]})
        pipe = self.r.pipeline()
        pipe.hset(self._k("tasks"), task_id, json.dumps(t, ensure_ascii=False))
        pipe.srem(self._k("running"), task_id)
        pipe.hincrby(self._k("stats"), "running", -1)
        pipe.hincrby(self._k("stats"), state, 1)
        pipe.hincrby(self._k("stats"), "tokens_in", int(tokens_in or 0))
        pipe.hincrby(self._k("stats"), "tokens_out", int(tokens_out or 0))
        pipe.hincrbyfloat(self._k("stats"), "cost_usd", float(cost_usd or 0))
        if duration_ms is not None:
            pipe.zadd(self._k("durations"), {task_id: duration_ms})
            pipe.zremrangebyrank(self._k("durations"), 0, -2001)
        day = time.strftime("%Y-%m-%d", time.localtime(t["created_at"]))
        pipe.hincrby(self._k("daily"), f"{day}:n", 1)
        if state == "succeeded":
            pipe.hincrby(self._k("daily"), f"{day}:ok", 1)
        pipe.zadd(self._k("finished"), {task_id: now})
        pipe.hincrby(self._k("tokens"), "in", int(tokens_in or 0))
        pipe.hincrby(self._k("tokens"), "out", int(tokens_out or 0))
        pipe.lpush(self._k("events"), json.dumps(
            {"ts": now, "task_id": task_id, "kind": state,
             "payload": {"ms": duration_ms, "cost": cost_usd}}, ensure_ascii=False))
        pipe.execute()

    def requeue(self, task_id):
        raw = self.r.hget(self._k("tasks"), task_id)
        if not raw:
            return
        t = json.loads(raw)
        if t["state"] != "running":
            return
        t["state"] = "queued"
        t["started_at"] = None
        pipe = self.r.pipeline()
        pipe.hset(self._k("tasks"), task_id, json.dumps(t, ensure_ascii=False))
        pipe.srem(self._k("running"), task_id)
        pipe.zadd(self._k("queue"), {task_id: -t.get("priority", 0) * 1e12 + t["created_at"]})
        pipe.hincrby(self._k("stats"), "running", -1)
        pipe.hincrby(self._k("stats"), "queue", 1)
        pipe.execute()

    def cancel(self, task_id):
        raw = self.r.hget(self._k("tasks"), task_id)
        if not raw:
            return False
        t = json.loads(raw)
        if t["state"] != "queued":
            return False
        t["state"] = "cancelled"
        t["finished_at"] = time.time()
        pipe = self.r.pipeline()
        pipe.hset(self._k("tasks"), task_id, json.dumps(t, ensure_ascii=False))
        pipe.zrem(self._k("queue"), task_id)
        pipe.hincrby(self._k("stats"), "queue", -1)
        pipe.hincrby(self._k("stats"), "cancelled", 1)
        pipe.execute()
        return True

    def cancel_all_queued(self):
        ids = self.r.zrange(self._k("queue"), 0, -1)
        n = 0
        for tid in ids:
            if self.cancel(tid):
                n += 1
        return n

    def recover_interrupted(self):
        rows = []
        for tid in list(self.r.smembers(self._k("running"))):
            raw = self.r.hget(self._k("tasks"), tid)
            if not raw:
                self.r.srem(self._k("running"), tid)
                continue
            t = json.loads(raw)
            if t["state"] != "running":
                self.r.srem(self._k("running"), tid)
                continue
            t["state"] = "interrupted"
            t["finished_at"] = time.time()
            self.r.hset(self._k("tasks"), tid, json.dumps(t, ensure_ascii=False))
            self.r.srem(self._k("running"), tid)
            self.r.hincrby(self._k("stats"), "interrupted", 1)
            self.r.hincrby(self._k("stats"), "running", -1)
            rows.append(t)
        return rows

    # ---------- 读取 ----------

    def get(self, task_id):
        raw = self.r.hget(self._k("tasks"), task_id)
        return json.loads(raw) if raw else None

    def queue_depth(self):
        return int(self.r.zcard(self._k("queue")))

    def recent(self, limit=30):
        ids = self.r.lrange(self._k("recent"), 0, limit - 1)
        out = []
        for tid in ids:
            raw = self.r.hget(self._k("tasks"), tid)
            if raw:
                out.append(json.loads(raw))
        return out

    def stats(self, since_hours=24):
        s = self.r.hgetall(self._k("stats"))
        get = lambda k: int(float(s.get(k, 0)))  # noqa: E731
        durs = [int(score) for _, score in
                self.r.zrange(self._k("durations"), 0, -1, withscores=True)]
        succeeded, failed = get("succeeded"), get("failed")
        return {
            "counts": {k: get(k) for k in ("queued", "running", "succeeded", "failed",
                                           "cancelled", "interrupted")},
            "done": succeeded, "failed": failed, "queue": self.queue_depth(),
            "window_hours": since_hours, "window_runs": succeeded + failed,
            "window_cost": round(float(s.get("cost_usd", 0)), 4),
            "p50_ms": _pct(durs, 50), "p95_ms": _pct(durs, 95),
            "total_cost": round(float(s.get("cost_usd", 0)), 4),
            "tokens_in": get("tokens_in"), "tokens_out": get("tokens_out"),
        }

    def events(self, limit=100, task_id=None):
        raw = self.r.lrange(self._k("events"), 0, 4999)
        out = []
        for line in raw:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if task_id and e.get("task_id") != task_id:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    # ---------- 发件箱 ----------

    def outbox_add(self, body, task_id=None):
        oid = self.r.incr(self._k("outbox:seq"))
        self.r.hset(self._k("outbox"), oid, json.dumps(
            {"id": oid, "task_id": task_id, "body": body, "created_at": time.time(),
             "attempts": 0}, ensure_ascii=False))
        self.r.zadd(self._k("outbox:pending"), {str(oid): oid})
        return oid

    def outbox_pending(self, limit=20):
        ids = self.r.zrange(self._k("outbox:pending"), 0, limit - 1)
        out = []
        for i in ids:
            raw = self.r.hget(self._k("outbox"), i)
            if raw:
                out.append(json.loads(raw))
        return out

    def outbox_mark(self, oid, ok, error=None):
        if ok:
            self.r.zrem(self._k("outbox:pending"), str(oid))
            raw = self.r.hget(self._k("outbox"), oid)
            if raw:
                d = json.loads(raw)
                d["attempts"] = d.get("attempts", 0) + 1
                d["delivered_at"] = time.time()
                self.r.hset(self._k("outbox"), oid, json.dumps(d, ensure_ascii=False))
        else:
            raw = self.r.hget(self._k("outbox"), oid)
            if raw:
                d = json.loads(raw)
                d["attempts"] = d.get("attempts", 0) + 1
                d["last_error"] = (error or "")[:300]
                self.r.hset(self._k("outbox"), oid, json.dumps(d, ensure_ascii=False))

    def outbox_pending_count(self):
        return int(self.r.zcard(self._k("outbox:pending")))

    # ---------- 额度 ----------

    def count_since(self, seconds):
        return int(self.r.zcount(self._k("created"), time.time() - seconds, "+inf"))

    def tokens_since(self, seconds):
        # Redis 侧按累计值近似（窗口 < 1 天时精度足够做额度拦截）
        h = self.r.hgetall(self._k("tokens"))
        return int(h.get("in", 0)) + int(h.get("out", 0))

    # ---------- 保留策略 ----------

    def prune(self, days=30):
        cutoff = time.time() - days * 86400
        ids = self.r.zrangebyscore(self._k("finished"), "-inf", cutoff)
        n = 0
        for tid in ids:
            self.r.hdel(self._k("tasks"), tid)
            self.r.zrem(self._k("finished"), tid)
            self.r.zrem(self._k("created"), tid)
            self.r.lrem(self._k("recent"), 0, tid)
            self.r.zrem(self._k("queue"), tid)
            self.r.srem(self._k("running"), tid)
            n += 1
        return n

    def vacuum(self):
        return None

    def daily(self, days=7):
        h = self.r.hgetall(self._k("daily"))
        keys = sorted({k.split(":")[0] for k in h})
        out = []
        for d in keys[-days:]:
            n = int(h.get(f"{d}:n", 0))
            out.append({"d": d, "n": n, "ok": int(h.get(f"{d}:ok", 0)),
                        "cost": 0, "avg_ms": 0})
        return out


def _pct(vals, p):
    if not vals:
        return 0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
    return int(vals[k])


# 统一给公开方法加上"主库挂了就重新发现"的重连逻辑（放类外批量套，避免二十个装饰器）
for _name in ("enqueue", "claim_next", "finish", "requeue", "cancel", "cancel_all_queued",
              "recover_interrupted", "get", "queue_depth", "recent", "stats", "events",
              "daily", "outbox_add", "outbox_pending", "outbox_mark", "outbox_pending_count",
              "count_since", "tokens_since", "prune"):
    setattr(RedisStore, _name, RedisStore._resilient(getattr(RedisStore, _name)))
