# -*- coding: utf-8 -*-
"""任务队列状态机与幂等语义。"""
import time

import pytest

from store import TaskStore


@pytest.fixture()
def store(tmp_path):
    return TaskStore(tmp_path / "t.db")


def test_enqueue_and_claim(store):
    tid, dup = store.enqueue("hello", source="qq")
    assert dup is False and len(tid) == 12
    assert store.queue_depth() == 1

    row = store.claim_next()
    assert row["id"] == tid and row["state"] == "running"
    assert store.queue_depth() == 0
    assert store.claim_next() is None


def test_dedup_key_is_idempotent(store):
    tid1, dup1 = store.enqueue("a", dedup_key="qq:100")
    tid2, dup2 = store.enqueue("a", dedup_key="qq:100")
    assert (dup1, dup2) == (False, True)
    assert tid1 == tid2
    assert store.queue_depth() == 1


def test_priority_order(store):
    low, _ = store.enqueue("low", priority=0)
    high, _ = store.enqueue("high", priority=5)
    assert store.claim_next()["id"] == high
    assert store.claim_next()["id"] == low


def test_finish_records_duration_and_cost(store):
    tid, _ = store.enqueue("x")
    store.claim_next()
    store.finish(tid, "succeeded", result="ok", cost_usd=0.25, duration_ms=1234)
    row = store.get(tid)
    assert row["state"] == "succeeded"
    assert row["duration_ms"] == 1234
    assert row["cost_usd"] == pytest.approx(0.25)
    assert row["result"] == "ok"


def test_invalid_terminal_state_rejected(store):
    tid, _ = store.enqueue("x")
    with pytest.raises(AssertionError):
        store.finish(tid, "queued")


def test_cancel_only_queued(store):
    a, _ = store.enqueue("a")
    b, _ = store.enqueue("b")
    store.claim_next()          # a 变成 running
    assert store.cancel(a) is False   # running 的不能这样取消
    assert store.cancel(b) is True
    assert store.get(b)["state"] == "cancelled"


def test_cancel_all_queued(store):
    for i in range(3):
        store.enqueue(f"t{i}")
    assert store.cancel_all_queued() == 3
    assert store.queue_depth() == 0


def test_recover_interrupted(store):
    tid, _ = store.enqueue("boom")
    store.claim_next()
    rows = store.recover_interrupted()
    assert [r["id"] for r in rows] == [tid]
    assert store.get(tid)["state"] == "interrupted"
    # 再调一次不会重复返回
    assert store.recover_interrupted() == []


def test_stats_percentiles(store):
    durations = [100, 200, 300, 400, 5000]
    for i, d in enumerate(durations):
        tid, _ = store.enqueue(f"t{i}")
        store.claim_next()
        store.finish(tid, "succeeded", duration_ms=d, cost_usd=0.01)
    st = store.stats()
    assert st["done"] == 5
    assert st["p50_ms"] == 300
    assert st["p95_ms"] == 5000
    assert st["total_cost"] == pytest.approx(0.05)


def test_events_are_logged(store):
    tid, _ = store.enqueue("e")
    store.claim_next()
    store.finish(tid, "failed", error="boom")
    kinds = [e["kind"] for e in store.events(task_id=tid)]
    assert set(kinds) == {"enqueued", "started", "failed"}


def test_daily_buckets(store):
    tid, _ = store.enqueue("d")
    store.claim_next()
    store.finish(tid, "succeeded", duration_ms=10)
    days = store.daily(7)
    assert len(days) == 1 and days[0]["n"] == 1 and days[0]["ok"] == 1
