# -*- coding: utf-8 -*-
"""Redis 后端与 SQLite 后端行为一致（没有 Redis 时自动跳过）。

用独立 db 15，测试前后 flush，不会碰到正在跑的生产队列。
"""
import pytest

redis = pytest.importorskip("redis")

URL = "redis://127.0.0.1:6380/15"


def _available():
    try:
        redis.Redis.from_url(URL, decode_responses=True, protocol=2).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="本机没有 Redis（127.0.0.1:6380）")


@pytest.fixture()
def store():
    from store_redis import RedisStore
    s = RedisStore(URL, prefix="testqq")
    s.r.flushdb()
    yield s
    s.r.flushdb()


def test_enqueue_and_claim(store):
    tid, dup = store.enqueue("hello", source="qq")
    assert dup is False and len(tid) == 12
    assert store.queue_depth() == 1
    row = store.claim_next()
    assert row["id"] == tid and row["state"] == "running"
    assert store.queue_depth() == 0
    assert store.claim_next() is None


def test_dedup_is_idempotent(store):
    a, d1 = store.enqueue("x", dedup_key="qq:1")
    b, d2 = store.enqueue("x", dedup_key="qq:1")
    assert (d1, d2) == (False, True) and a == b
    assert store.queue_depth() == 1


def test_priority(store):
    low, _ = store.enqueue("low", priority=0)
    high, _ = store.enqueue("high", priority=9)
    assert store.claim_next()["id"] == high
    assert store.claim_next()["id"] == low


def test_exclude_busy_cwd(store):
    store.enqueue("a", cwd=r"C:\w1")
    store.enqueue("b", cwd=r"C:\w2")
    first = store.claim_next(exclude_cwds=[r"C:\w1"])
    assert first["cwd"] == r"C:\w2"
    second = store.claim_next(exclude_cwds=[r"C:\w2"])
    assert second["cwd"] == r"C:\w1"


def test_finish_and_stats(store):
    tid, _ = store.enqueue("x")
    store.claim_next()
    store.finish(tid, "succeeded", result="ok", duration_ms=1234,
                 tokens_in=100, tokens_out=7, cost_usd=0.01)
    row = store.get(tid)
    assert row["state"] == "succeeded" and row["tokens_in"] == 100
    st = store.stats()
    assert st["done"] == 1 and st["tokens_in"] == 100 and st["p50_ms"] == 1234


def test_cancel_and_cancel_all(store):
    a, _ = store.enqueue("a")
    store.claim_next()
    assert store.cancel(a) is False
    store.enqueue("b")
    store.enqueue("c")
    assert store.cancel_all_queued() == 2


def test_recover_interrupted(store):
    tid, _ = store.enqueue("boom")
    store.claim_next()
    rows = store.recover_interrupted()
    assert [r["id"] for r in rows] == [tid]
    assert store.get(tid)["state"] == "interrupted"
    assert store.recover_interrupted() == []


def test_recent_and_events(store):
    tid, _ = store.enqueue("e")
    store.claim_next()
    store.finish(tid, "failed", error="x")
    assert store.recent(5)[0]["id"] == tid
    kinds = [e["kind"] for e in store.events(10, task_id=tid)]
    assert set(kinds) == {"enqueued", "started", "failed"}


def test_claim_is_atomic_across_clients(store):
    """两个独立客户端抢同一条任务，只能有一个成功。"""
    from store_redis import RedisStore
    other = RedisStore(URL, prefix="testqq")
    store.enqueue("only-one")
    got = [store.claim_next(), other.claim_next()]
    assert sum(1 for r in got if r) == 1
