# -*- coding: utf-8 -*-
"""语音条（TTS）模块的用例。

原则：**不真的调 PowerShell、不连网**。合成的两条路都用 monkeypatch 替身，
这样 CI（Linux）也能跑。真实链路靠手机上发一条 `/语音` 验。
"""
import asyncio
from pathlib import Path

import tts


def _run(coro):
    return asyncio.run(coro)


# ---------------- clean_text：念出来不狼狈 ----------------

def test_clean_strips_url():
    assert "链接" in tts.clean_text("看这个 https://example.com/a?b=1 挺好")
    assert "https" not in tts.clean_text("看这个 https://example.com/a?b=1 挺好")


def test_clean_strips_markers_and_code():
    out = tts.clean_text("哈哈 @@表情:猫猫@@ 就这 `ls -l` 也行")
    assert "@@" not in out and "`" not in out
    assert "哈哈" in out and "也行" in out


def test_clean_caps_and_breaks_at_punctuation():
    text = "第一句话。" * 100
    out = tts.clean_text(text, limit=50)
    assert len(out) <= 50
    assert out.endswith("第一句话")


def test_clean_keeps_normal_chinese():
    s = "今天好热 我先去吃饭了"
    assert tts.clean_text(s) == s


# ---------------- segment ----------------

def test_segment_is_record_with_file_uri():
    seg = tts.segment(r"C:\Users\win11\qq-remote\bridge\tmp\tts\x.wav")
    assert seg["type"] == "record"
    assert seg["data"]["file"].startswith("file:///")


# ---------------- 缓存与降级 ----------------

def test_synth_disabled_returns_empty(tmp_path):
    t = tts.TTS({"enabled": False}, cache_dir=tmp_path)
    assert not t.available()
    assert _run(t.synth("你好")) == ("", "")


def test_synth_too_short(tmp_path):
    t = tts.TTS({}, cache_dir=tmp_path)
    assert _run(t.synth("嗯")) == ("", "")


def test_synth_falls_back_to_sapi(tmp_path, monkeypatch):
    t = tts.TTS({"engine": "auto"}, cache_dir=tmp_path)
    monkeypatch.setattr(t, "_edge_importable", lambda: False)
    calls = []

    def fake_sapi(text, out):
        calls.append(text)
        out.write_bytes(b"\0" * 4096)

    monkeypatch.setattr(t, "_sapi", fake_sapi)
    path, eng = _run(t.synth("你好 在吗"))
    assert eng == "sapi" and Path(path).exists()
    # 第二次走缓存：不再合成
    path2, eng2 = _run(t.synth("你好 在吗"))
    assert path2 == path and eng2 == "sapi"
    assert len(calls) == 1


def test_synth_edge_failure_falls_back(tmp_path, monkeypatch):
    t = tts.TTS({"engine": "auto"}, cache_dir=tmp_path)
    monkeypatch.setattr(t, "_edge_importable", lambda: True)

    async def boom(text, out):
        raise RuntimeError("no network")

    monkeypatch.setattr(t, "_edge", boom)
    monkeypatch.setattr(t, "_sapi", lambda text, out: out.write_bytes(b"\0" * 4096))
    path, eng = _run(t.synth("喂 试一下"))
    assert eng == "sapi" and Path(path).exists()


def test_synth_engine_sapi_only_skips_edge(tmp_path, monkeypatch):
    t = tts.TTS({"engine": "sapi"}, cache_dir=tmp_path)
    monkeypatch.setattr(t, "_edge", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(t, "_sapi", lambda text, out: out.write_bytes(b"\0" * 4096))
    assert _run(t.synth("只说本地"))[1] == "sapi"


def test_status_line_shows_engine(tmp_path, monkeypatch):
    t = tts.TTS({}, cache_dir=tmp_path)
    monkeypatch.setattr(t, "_edge_importable", lambda: False)
    assert "SAPI" in t.status_line()
    assert "关闭" in tts.TTS({"enabled": False}, cache_dir=tmp_path).status_line()
