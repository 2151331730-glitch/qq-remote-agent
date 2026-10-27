# -*- coding: utf-8 -*-
"""在群里"学"表情：别人发的商城表情/表情图 → 存进本地库。

真机上商城表情是以 image 段下发的（带 emoji_id + gxh.vip.qq.com 直链），
所以这里也按 image 段造数据，只是把下载换成假的。
"""
import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import stickers  # noqa: E402

GIF = b"GIF89a" + b"\x00" * 400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
JPG = b"\xff\xd8\xff" + b"\x00" * 400
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 400


# ---------------- 纯函数：后缀识别 + 入库 ----------------

def test_ext_of_sniffs_the_file_header():
    assert stickers.ext_of(GIF) == ".gif"
    assert stickers.ext_of(PNG) == ".png"
    assert stickers.ext_of(JPG) == ".jpg"
    assert stickers.ext_of(WEBP) == ".webp"
    assert stickers.ext_of(b"not an image") == ""


def test_store_writes_and_dedupes(tmp_path):
    p1 = stickers.store(tmp_path, "狗头", GIF)
    assert Path(p1).name == "狗头.gif"
    # 同一张图再存一次：不新建文件，直接返回原来的
    assert stickers.store(tmp_path, "狗头", GIF) == p1
    assert len(list(tmp_path.iterdir())) == 1
    # 同名不同图：加序号，不覆盖
    p2 = stickers.store(tmp_path, "狗头", PNG)
    assert Path(p2).name == "狗头-2.png"
    assert Path(p1).read_bytes() == GIF


def test_store_rejects_junk(tmp_path):
    assert stickers.store(tmp_path, "x", b"<html>404</html>") == ""
    assert stickers.store(tmp_path, "y", b"GIF89a") == ""      # 太小，基本是失败页
    assert list(tmp_path.iterdir()) == []


# ---------------- bridge 里挑表情的那套判断 ----------------

@pytest.fixture()
def b(tmp_path, monkeypatch):
    cfg = json.loads((BRIDGE / "config.example.json").read_text(encoding="utf-8"))
    mem = tmp_path / "memory.md"
    mem.write_text("", encoding="utf-8")
    cfg.update({
        "claude_exe": "claude",
        "workdir": str(tmp_path),
        "log_dir": str(tmp_path / "logs"),
        "db_path": str(tmp_path / "t.db"),
        "memory_path": str(mem),
        "instance_lock_port": 0,
        "panel_port": 0,
        "owner_uin": 0,
        "sticker_dir": str(tmp_path / "stickers"),
        "transcript_dir": str(tmp_path / "transcripts"),
        "persona_path": str(tmp_path / "persona.md"),
        "public_persona_path": str(tmp_path / "public_persona.md"),
    })
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QQ_REMOTE_CONFIG", str(cfg_path))
    monkeypatch.setenv("QQ_REMOTE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("QQ_REMOTE_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("QQ_REMOTE_REMINDERS", str(tmp_path / "reminders.db"))
    monkeypatch.setenv("QQ_REMOTE_NO_LOCK", "1")
    monkeypatch.setenv("QQ_REMOTE_NO_ASR", "1")
    sys.modules.pop("bridge", None)
    mod = importlib.import_module("bridge")
    mod.STICKER_DIR = tmp_path / "stickers"
    mod.STICKERS = stickers.StickerBook(mod.STICKER_DIR)
    mod.STICKER_AUTO_LEARN = True
    mod.STICKER_MAX = 200
    mod.STICKER_LEARN_LIMIT = 3
    mod._LEARN.update(at=0.0, n=0)
    return mod


def _market(summary="狗头", eid="abc123"):
    return {"message": [{"type": "image", "data": {
        "summary": summary, "emoji_id": eid,
        "url": "https://gxh.vip.qq.com/x.gif"}}]}


def _plain_image():
    return {"message": [{"type": "image", "data": {"file": "a.jpg", "sub_type": 0}}]}


def test_learn_stores_a_market_face(b, monkeypatch):
    async def fake_bytes(seg):
        return GIF
    monkeypatch.setattr(b, "_image_bytes", fake_bytes)
    n = asyncio.run(b._learn_stickers(_market()))
    assert n == 1
    assert (b.STICKER_DIR / "狗头.gif").is_file()
    assert "狗头" in b.STICKERS.menu()


def test_learn_ignores_plain_pictures(b, monkeypatch):
    async def fake_bytes(seg):
        return JPG
    monkeypatch.setattr(b, "_image_bytes", fake_bytes)
    assert asyncio.run(b._learn_stickers(_plain_image())) == 0
    assert not b.STICKER_DIR.is_dir() or list(b.STICKER_DIR.iterdir()) == []


def test_learn_respects_the_switch(b, monkeypatch):
    b.STICKER_AUTO_LEARN = False

    async def fake_bytes(seg):
        return GIF
    monkeypatch.setattr(b, "_image_bytes", fake_bytes)
    assert asyncio.run(b._learn_stickers(_market())) == 0


def test_learn_is_rate_limited(b, monkeypatch):
    async def fake_bytes(seg):
        return GIF
    monkeypatch.setattr(b, "_image_bytes", fake_bytes)
    b.STICKER_LEARN_LIMIT = 1
    assert asyncio.run(b._learn_stickers(_market(eid="a1"))) == 1
    # 一分钟内的第二张不收了（免得群友刷表情时一直下文件）
    assert asyncio.run(b._learn_stickers(_market(summary="另一个", eid="a2"))) == 0


def test_sticker_name_falls_back_when_summary_is_generic(b):
    assert b._sticker_name({"data": {"summary": "表情", "emoji_id": "ff00ff88"}}) == "表情ff88"
    assert b._sticker_name({"data": {"summary": "[狗头]"}}) == "狗头"
