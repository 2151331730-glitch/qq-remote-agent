# -*- coding: utf-8 -*-
"""分条发送的拆分规则。"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
chatstyle = importlib.import_module("chatstyle")


def test_short_reply_stays_one():
    assert chatstyle.split_bubbles("在呢", min_chars=80) == ["在呢"]
    assert chatstyle.split_bubbles("嗯？咋了", min_chars=80) == ["嗯？咋了"]


def test_empty():
    assert chatstyle.split_bubbles("") == []
    assert chatstyle.split_bubbles("   \n ") == []


def test_two_sentences_split_when_long():
    text = "这个我看了一下，主要是配置文件写错了。" + "你把 group_policy 改回 0.1 就行。" * 3
    parts = chatstyle.split_bubbles(text, min_chars=30, limit=60, max_parts=3)
    assert 2 <= len(parts) <= 3
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_never_more_than_max_parts():
    text = "。".join(["这句话有一点长" * 4] * 8) + "。"
    parts = chatstyle.split_bubbles(text, min_chars=20, limit=50, max_parts=3)
    assert len(parts) == 3
    assert parts[-1].endswith("。")


def test_long_single_sentence_hard_cut():
    text = "啊" * 300
    parts = chatstyle.split_bubbles(text, min_chars=20, limit=100, max_parts=3)
    assert len(parts) == 3
    assert all(len(p) <= 200 for p in parts)     # 合并后最后一段允许超 limit


def test_newlines_become_spaces():
    parts = chatstyle.split_bubbles("第一行\n第二行\n第三行", min_chars=4, limit=20)
    assert all("\n" not in p for p in parts)
    assert "".join(parts).count("第一行") == 1


def test_punctuation_kept():
    parts = chatstyle.split_bubbles("行吧。" + "那就先这样。" * 5, min_chars=10, limit=20)
    assert all(p.endswith("。") for p in parts)
