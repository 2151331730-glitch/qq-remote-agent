# -*- coding: utf-8 -*-
"""新增能力的最小单测：表情库、语音客户端、配置默认值。

这些模块刻意不依赖 bridge.py（那边一 import 就会碰真机的 tasks.db），
所以这里只测纯函数与「失败一律 None」的边界。
"""
import json
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(BRIDGE))

import stickers  # noqa: E402
import voice  # noqa: E402


# ---------------- 表情 ----------------

def test_extract_plain():
    assert stickers.extract("好啊") == ("好啊", "")


def test_extract_marker():
    text, name = stickers.extract("笑死\n@@表情:猫猫震惊@@")
    assert text == "笑死"
    assert name == "猫猫震惊"


def test_extract_fullwidth_colon_and_spaces():
    _, name = stickers.extract("@@ 表情 ： 无语 @@")
    assert name == "无语"


def test_extract_only_first():
    text, name = stickers.extract("a@@表情:一@@@@表情:二@@")
    assert name == "一"
    assert "@@表情:二@@" in text          # 只摘第一张，剩下的原样留着


def test_book_scans_images(tmp_path):
    (tmp_path / "笑.gif").write_bytes(b"GIF89a")
    (tmp_path / "哭.png").write_bytes(b"\x89PNG")
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    book = stickers.StickerBook(tmp_path)
    assert len(book) == 2
    assert "笑" in book.menu()
    assert book.path_for("笑").endswith("笑.gif")


def test_book_index_override(tmp_path):
    (tmp_path / "a.gif").write_bytes(b"GIF89a")
    (tmp_path / "index.json").write_text(
        json.dumps({"无语": {"file": "a.gif", "desc": "摊手"}}, ensure_ascii=False),
        encoding="utf-8")
    book = stickers.StickerBook(tmp_path)
    assert "无语" in book.menu()
    assert book.path_for("无语").endswith("a.gif")
    assert book.path_for("不存在") == ""


def test_book_empty_dir(tmp_path):
    book = stickers.StickerBook(tmp_path / "nope")
    assert len(book) == 0
    assert book.menu() == ""


def test_segment_marks_sub_type(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"GIF89a")
    seg = stickers.segment(str(p))
    assert seg["type"] == "image"
    assert seg["data"]["sub_type"] == 1          # 1 = 表情，不是大图
    assert seg["data"]["file"].startswith("file:///")


# ---------------- 语音 ----------------

def test_voice_disabled_returns_none():
    v = voice.Voice({"enabled": False})
    assert v.transcribe(b"anything") is None


def test_voice_empty_audio_returns_none():
    v = voice.Voice({"enabled": True})
    assert v.transcribe(b"") is None


def test_voice_bad_port_fails_soft():
    """端口上没人监听时要安静地返回 None，不能抛异常打断消息入库。

    坑：不能只 patch `_ping`。`ensure_server()` 发现 ping 不通会**真的去拉起**
    asr_server.py —— 18799 上空出来就被它占了，一个 ~390MB 的 whisper 进程
    跑完测试还赖在那儿，而且这个用例还要白等 25 秒。这里直接短路 ensure_server。
    """
    v = voice.Voice({"enabled": True, "port": 18799})
    v.ensure_server = lambda: False
    assert v.transcribe(b"RIFF....") is None


def test_voice_without_deps_fails_soft(monkeypatch):
    """没装 faster-whisper 时 ensure_server 返回 False，且不留下任何进程。"""
    monkeypatch.setattr(voice, "deps_ok", lambda: False)
    v = voice.Voice({"enabled": True, "port": 18798})
    assert v.ensure_server() is False
    assert v._proc is None


def test_voice_timeout_option():
    v = voice.Voice({"timeout_seconds": 5})
    assert v.timeout == 5.0
    assert voice.Voice({}).port == 18791
