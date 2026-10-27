# -*- coding: utf-8 -*-
"""群里的图 / 表情包：让模型「看得见」。

规矩和公开通道的老规则一致：
① 图里的字 → 本地 OCR，免费、离线，谁都享受；
② 图的**内容** → 视觉模型，花钱、图要出网，所以只有主人的图才允许。

这里只测胶水层（_any_sticker / _group_image_note / _enrich_with_images），
真实的 OCR / 视觉模型在 tests/test_vision.py 和运行时验证。
"""
import asyncio
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

OWNER = 42


def _load(tmp_path, monkeypatch):
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
        "owner_uin": OWNER,
        "transcript_dir": str(tmp_path / "transcripts"),
        "sticker_dir": str(tmp_path / "stickers"),
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
    sys.modules.pop("bridge", None)
    return importlib.import_module("bridge")


def _img_msg(text="", sticker=False):
    d = {"file": "abc.jpg", "sub_type": "1" if sticker else "0"}
    if sticker:
        d["emoji_id"] = "7788"
    segs = [{"type": "image", "data": d}]
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    return {"group_id": 7, "user_id": OWNER, "sender": {"nickname": "甲"},
            "message": segs}


class _Spy:
    """替换 _save_images 与 VISION.look，记下每次调用的参数。"""

    def __init__(self, b, monkeypatch, kind="text", got="空调 26 度"):
        self.calls = []
        self.kind, self.got = kind, got

        async def fake_save(data, limit=3):
            return [str(Path("x.jpg"))]

        def fake_look(path, question="", allow_vision=False):
            self.calls.append({"question": question, "allow": allow_vision})
            return self.kind, self.got

        monkeypatch.setattr(b, "_save_images", fake_save)
        monkeypatch.setattr(b, "VISION_ENABLED", True)
        monkeypatch.setattr(b.VISION, "look", fake_look)


# ---------------- _any_sticker ----------------

def test_any_sticker_recognises_market_face_and_cq(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    assert b._any_sticker(_img_msg(sticker=True)) is True
    assert b._any_sticker(_img_msg()) is False
    assert b._any_sticker({"message": [], "raw_message": ""}) is False
    raw = {"raw_message": "[CQ:image,file=a.jpg,sub_type=1]"}
    assert b._any_sticker(raw) is True
    assert b._any_sticker({"raw_message": "[CQ:image,file=a.jpg]"}) is False


# ---------------- _group_image_note 的两档 ----------------

def test_stranger_image_reads_text_only(tmp_path, monkeypatch):
    """陌生人的图只走 OCR：不花钱，图也不出网。"""
    b = _load(tmp_path, monkeypatch)
    spy = _Spy(b, monkeypatch)
    note = asyncio.run(b._group_image_note(_img_msg("谁懂这个啊"), uid=99))
    assert "[图片里的文字]" in note
    assert spy.calls[0]["allow"] is False


def test_owner_image_may_use_vision(tmp_path, monkeypatch):
    """主人的图才允许请视觉模型看内容。"""
    b = _load(tmp_path, monkeypatch)
    spy = _Spy(b, monkeypatch)
    asyncio.run(b._group_image_note(_img_msg("谁懂这个啊"), uid=OWNER))
    assert spy.calls[0]["allow"] is True


def test_sticker_gets_its_own_question(tmp_path, monkeypatch):
    """表情包的问题不一样：要问「想表达什么」，不是「图里有什么字」。"""
    b = _load(tmp_path, monkeypatch)
    spy = _Spy(b, monkeypatch)
    asyncio.run(b._group_image_note(_img_msg(sticker=True), uid=OWNER))
    assert spy.calls[0]["question"] == b.STICKER_QUESTION


def test_vision_off_returns_nothing(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    _Spy(b, monkeypatch)
    monkeypatch.setattr(b, "VISION_ENABLED", False)
    assert asyncio.run(b._group_image_note(_img_msg(), uid=OWNER)) == ""


# ---------------- _enrich_with_images ----------------

def test_enrich_appends_note_to_text(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    _Spy(b, monkeypatch)
    out = asyncio.run(b._enrich_with_images(_img_msg("看这个"), OWNER, "看这个"))
    assert out.startswith("看这个")
    assert "[图片里的文字]" in out


def test_enrich_without_image_is_noop(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    spy = _Spy(b, monkeypatch)
    data = {"group_id": 7, "message": [{"type": "text", "data": {"text": "纯文字"}}]}
    assert asyncio.run(b._enrich_with_images(data, OWNER, "纯文字")) == "纯文字"
    assert spy.calls == []          # 没图就不该下载、不该调 OCR


def test_enrich_keeps_text_when_nothing_read(tmp_path, monkeypatch):
    """读什么都没读出来：正文原样交下去，别塞一行空的占位。"""
    b = _load(tmp_path, monkeypatch)
    _Spy(b, monkeypatch, kind="", got="")
    assert asyncio.run(b._enrich_with_images(_img_msg("看这个"), OWNER, "看这个")) == "看这个"
