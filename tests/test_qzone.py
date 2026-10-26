# -*- coding: utf-8 -*-
"""说说（Qzone）模块的用例：参数拼装、草稿过期、发布结果判定。

真发靠手机上 `/说说` → `/说说发` 验（NapCat 的 send_qzone_msg 是扩展动作，
假客户端只能验我们自己这层的逻辑）。
"""
import asyncio
import time

import qzone


def _run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, res=None, exc=None):
        self.res = res
        self.exc = exc
        self.calls = []

    async def call(self, action, params, timeout=30):
        self.calls.append((action, params))
        if self.exc:
            raise self.exc
        return self.res


# ---------------- build_params ----------------

def test_build_params_minimal():
    p = qzone.build_params("今天好热")
    assert p == {"content": "今天好热", "ugc_right": 4}


def test_build_params_empty_images_dropped():
    p = qzone.build_params("x", images=[])
    assert "images" not in p


def test_build_params_images_and_uins():
    p = qzone.build_params("x", images=[r"C:\a.png"], ugc_right=16, target_uins=["123"])
    assert p["images"] == [r"C:\a.png"]
    assert p["target_uins"] == [123]
    assert p["ugc_right"] == 16


def test_build_params_rejects_blank_and_long():
    for bad in ("", "   ", "字" * (qzone.MAX_CONTENT + 1)):
        try:
            qzone.build_params(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝：{bad[:10]!r}")


def test_right_label():
    assert qzone.right_label(4) == "好友"
    assert qzone.right_label("64") == "仅自己"
    assert "1024" in qzone.right_label(1024)


# ---------------- 草稿 ----------------

def test_draft_ok_missing_and_fresh_and_expired():
    ok, why = qzone.draft_ok({})
    assert not ok and "草稿" in why

    now = time.time()
    ok, _ = qzone.draft_ok({"content": "你好", "at": now - 10}, now=now)
    assert ok

    ok, why = qzone.draft_ok({"content": "你好", "at": now - qzone.DRAFT_TTL - 1}, now=now)
    assert not ok and "过期" in why


# ---------------- publish ----------------

def test_publish_success():
    c = FakeClient({"status": "ok", "retcode": 0, "data": {"tid": "abc"}})
    ok, msg = _run(qzone.publish(c, "今天好热", ugc_right=64))
    assert ok and "仅自己" in msg
    assert c.calls[0][0] == "send_qzone_msg"
    assert c.calls[0][1]["ugc_right"] == 64


def test_publish_business_error():
    c = FakeClient({"status": "failed", "retcode": 1400, "message": "参数错误"})
    ok, msg = _run(qzone.publish(c, "x"))
    assert not ok and "1400" in msg and "参数错误" in msg


def test_publish_exception_is_reported():
    c = FakeClient(exc=ConnectionError("ws closed"))
    ok, msg = _run(qzone.publish(c, "x"))
    assert not ok and "ConnectionError" in msg


def test_publish_validates_before_calling():
    c = FakeClient({"retcode": 0})
    ok, _ = _run(qzone.publish(c, ""))
    assert not ok and not c.calls
