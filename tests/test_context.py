# -*- coding: utf-8 -*-
"""上下文摘录的压缩规则。"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
context = importlib.import_module("context")


def msg(uid, name, *segs):
    return {"user_id": uid, "sender": {"user_id": uid, "nickname": name},
            "message": list(segs)}


def text(t):
    return {"type": "text", "data": {"text": t}}


def at(qq):
    return {"type": "at", "data": {"qq": str(qq)}}


def test_basic_transcript():
    msgs = [msg(1, "甲", text("晚上吃啥")), msg(2, "乙", text("火锅吧"))]
    assert context.format_transcript(msgs) == "甲: 晚上吃啥\n乙: 火锅吧"


def test_at_image_and_self_label():
    msgs = [
        msg(1, "甲", at(999), text(" 看图")),
        msg(999, "机器人", text("收到")),
        msg(1, "甲", {"type": "image", "data": {"url": "x"}}),
    ]
    out = context.format_transcript(msgs, self_id=999)
    assert "@999" in out
    assert "我: 收到" in out
    assert "[图片]" in out


def test_limit_keeps_latest():
    msgs = [msg(i, f"人{i}", text(f"第{i}条")) for i in range(20)]
    out = context.format_transcript(msgs, limit=3)
    assert "第17条" in out and "第18条" in out and "第19条" in out
    assert "第16条" not in out


def test_per_message_truncation():
    out = context.format_transcript([msg(1, "甲", text("长" * 200))], per_message=10)
    assert out.endswith("长" * 10)
    assert len(out) < 30


def test_max_chars_cuts_oldest_whole_lines():
    msgs = [msg(i, f"人{i}", text("内容" * 10)) for i in range(10)]
    out = context.format_transcript(msgs, max_chars=60, per_message=40)
    assert len(out) <= 60
    assert out.splitlines()[0].startswith("人")     # 不会留半行
    assert "人9" in out                              # 保留最新的


def test_cq_string_and_empty():
    assert context.format_transcript([{"message": "[CQ:image,file=a.jpg]"}]) == ""
    out = context.format_transcript([{"message": "[CQ:face,id=1]你好"}])
    assert out == "某人: 你好"


def test_empty_messages():
    assert context.format_transcript([]) == ""
    assert context.format_transcript(None) == ""
