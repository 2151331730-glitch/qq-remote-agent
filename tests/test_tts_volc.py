# -*- coding: utf-8 -*-
"""火山豆包语音合成：引擎顺序、缓存键、请求头、把流式 JSON 行拼回 mp3。

全部离线——urlopen 被换成一个假的，只看发出去的请求长什么样、
返回的假数据能不能被正确解出来。
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import tts  # noqa: E402

AUDIO = b"\xff\xfb\x90\x00" + b"x" * 3000          # 够长，冒充 mp3


class _Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_stream(parts=(AUDIO,), code=0, message="OK"):
    """把音频切成几块，做成 v3 那种一行一个 JSON 的返回体。"""
    lines = [json.dumps({"header": {"code": code, "message": message}})]
    for p in parts:
        lines.append(json.dumps({"data": base64.b64encode(p).decode()}))
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def secret(tmp_path):
    p = tmp_path / "volc.json"
    p.write_text(json.dumps({"appid": "1234567890", "api_key": "k" * 36}),
                 encoding="utf-8")
    return p


def _mk(secret_path=None, **volc):
    cfg = {"volc": dict({"secret_file": str(secret_path) if secret_path else ""},
                        **volc)}
    return tts.TTS(cfg, cache_dir=str(Path(tempfile.gettempdir()) / "qq-remote-ttscache"))


# ---------------- 引擎选择 ----------------

def test_auto_order_prefers_volc(secret):
    t = _mk(secret)
    assert t._engines() == ["volc", "edge", "sapi"]
    assert t.volc_ready() is True


def test_volc_disabled_drops_out(secret):
    t = _mk(secret, enabled=False)
    assert t._engines() == ["edge", "sapi"]


def test_no_secret_means_not_ready(tmp_path):
    t = _mk(tmp_path / "missing.json")
    assert t.volc_ready() is False
    # 但引擎顺序里仍然有它——只是会被跳过，不会崩
    assert "volc" in t._engines()


def test_secret_missing_fields_is_not_ready(tmp_path):
    p = tmp_path / "half.json"
    p.write_text(json.dumps({"appid": "123"}), encoding="utf-8")
    assert _mk(p).volc_ready() is False


def test_fixed_engine_list():
    t = tts.TTS({"engine": "volc"})
    assert t._engines() == ["volc"]


def test_status_line_mentions_volc(secret):
    assert "火山" in _mk(secret).status_line()


# ---------------- 缓存 ----------------

def test_cache_ext_per_engine(secret):
    t = _mk(secret)
    assert t._cache_path("你好", "volc").suffix == ".mp3"
    assert t._cache_path("你好", "edge").suffix == ".mp3"
    assert t._cache_path("你好", "sapi").suffix == ".wav"


def test_cache_isolated_by_voice(secret):
    a = _mk(secret, voice="zh_female_xiaohe_uranus_bigtts")
    b = _mk(secret, voice="zh_male_m191_uranus_bigtts")
    assert a._cache_path("你好", "volc") != b._cache_path("你好", "volc")


# ---------------- 请求 ----------------

def test_request_headers_and_body(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["headers"] = {k.lower(): v for k, v in req.headers.items()}
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(_fake_stream())

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    out = tts.volc_request("在吗", ("1234567890", "key"), voice="zh_female_vv_uranus_bigtts")

    assert out == AUDIO
    assert sent["url"] == tts.VOLC_URL
    assert sent["headers"]["x-api-key"] == "key"              # 不是 access-key
    assert sent["headers"]["x-api-app-id"] == "1234567890"
    assert sent["headers"]["x-api-resource-id"] == "seed-tts-2.0"
    assert sent["body"]["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"
    assert sent["body"]["req_params"]["text"] == "在吗"


def test_multiple_data_chunks_are_joined(monkeypatch):
    monkeypatch.setattr(tts.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(_fake_stream((AUDIO[:1000], AUDIO[1000:]))))
    assert tts.volc_request("在吗", ("1", "k")) == AUDIO


def test_error_code_raises(monkeypatch):
    monkeypatch.setattr(tts.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(_fake_stream(code=45000010,
                                                                     message="load grant")))
    with pytest.raises(RuntimeError, match="45000010"):
        tts.volc_request("在吗", ("1", "k"))


def test_empty_response_raises(monkeypatch):
    monkeypatch.setattr(tts.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b""))
    with pytest.raises(RuntimeError, match="没拿到音频"):
        tts.volc_request("在吗", ("1", "k"))


# ---------------- 合成落盘 ----------------

def test_synth_writes_mp3(monkeypatch, tmp_path, secret):
    monkeypatch.setattr(tts.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(_fake_stream()))
    t = tts.TTS({"engine": "volc", "volc": {"secret_file": str(secret)}},
                cache_dir=str(tmp_path))
    path, eng = __import__("asyncio").run(t.synth("在吗，今天怎么样"))
    assert eng == "volc"
    assert Path(path).suffix == ".mp3" and Path(path).stat().st_size == len(AUDIO)


def test_synth_falls_through_when_volc_unconfigured(monkeypatch, tmp_path):
    """没配密钥时应该直接跳到下一个引擎，而不是整条语音失败。"""
    called = []

    async def fake_edge(self, text, out):
        called.append("edge")
        out.write_bytes(b"y" * 2000)

    monkeypatch.setattr(tts.TTS, "_edge", fake_edge)
    monkeypatch.setattr(tts.TTS, "_edge_importable", lambda self: True)
    t = tts.TTS({"engine": "auto", "volc": {"secret_file": str(tmp_path / "no.json")}},
                cache_dir=str(tmp_path))
    path, eng = __import__("asyncio").run(t.synth("在吗，今天怎么样"))
    assert called == ["edge"] and eng == "edge"
    assert t.last_error == ""
