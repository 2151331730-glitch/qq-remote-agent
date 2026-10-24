# -*- coding: utf-8 -*-
"""看板 HTTP 接口：状态 / 任务 / 指标 / 提交与鉴权。"""
import asyncio
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from panel import Panel


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")


def _post(url, payload, token=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Panel-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


@pytest.fixture()
def panel():
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()

    async def submit(text, source="web", cwd=None):
        return {"status": "queued", "id": "abc123", "pos": 1, "echo": text, "cwd": cwd}

    p = Panel(_free_port(), "secret-token", loop,
              state_fn=lambda: {"current": "", "queue": 2, "done": 5},
              tasks_fn=lambda: [{"id": "abc123", "state": "queued", "text": "hi"}],
              submit_coro=submit,
              events_fn=lambda: [{"event": "task_start"}],
              metrics_fn=lambda: "# TYPE x counter\nx 1\n")
    url = p.start()
    yield p, url
    loop.call_soon_threadsafe(loop.stop)


def test_index_page(panel):
    _, url = panel
    status, ctype, body = _get(url)
    assert status == 200
    assert "text/html" in ctype
    assert "QQ Agent 控制台" in body


def test_state_api(panel):
    _, url = panel
    status, _, body = _get(url + "api/state")
    assert status == 200
    assert json.loads(body)["queue"] == 2


def test_tasks_api(panel):
    _, url = panel
    status, _, body = _get(url + "api/tasks")
    assert status == 200
    assert json.loads(body)["tasks"][0]["id"] == "abc123"


def test_metrics_endpoint(panel):
    _, url = panel
    status, ctype, body = _get(url + "metrics")
    assert status == 200
    assert "version=0.0.4" in ctype
    assert "x 1" in body


def test_post_requires_token(panel):
    _, url = panel
    status, body = _post(url + "api/task", {"text": "hello"})
    assert status == 403 and body["error"] == "bad token"


def test_post_with_token_queues(panel):
    _, url = panel
    status, body = _post(url + "api/task", {"text": "hello"}, token="secret-token")
    assert status == 200
    assert body["status"] == "queued" and body["id"] == "abc123"


def test_post_empty_rejected(panel):
    _, url = panel
    status, body = _post(url + "api/task", {"text": "   "}, token="secret-token")
    assert status == 400


def test_404(panel):
    _, url = panel
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(url + "nope")
    assert e.value.code == 404
