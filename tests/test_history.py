# -*- coding: utf-8 -*-
"""本地聊天记录的写入、裁剪与摘录。"""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
history = importlib.import_module("history")


def test_append_and_tail_order(tmp_path):
    history.append(tmp_path, "group:1", 11, "甲", "第一句")
    history.append(tmp_path, "group:1", 22, "乙", "第二句")
    out = history.tail(tmp_path, "group:1")
    assert out == "甲: 第一句\n乙: 第二句"


def test_scopes_are_isolated(tmp_path):
    history.append(tmp_path, "group:1", 11, "甲", "群里的")
    history.append(tmp_path, "user:9", 99, "乙", "私聊的")
    assert "群里的" in history.tail(tmp_path, "group:1")
    assert "私聊的" not in history.tail(tmp_path, "group:1")
    assert history.tail(tmp_path, "user:9") == "乙: 私聊的"


def test_limit_and_truncation(tmp_path):
    for i in range(10):
        history.append(tmp_path, "s", i, f"人{i}", "内" * 100)
    out = history.tail(tmp_path, "s", limit=3, per_message=10)
    assert len(out.splitlines()) == 3
    assert "人9:" in out and "人6:" not in out
    assert all(len(ln.split(": ", 1)[1]) <= 10 for ln in out.splitlines())


def test_max_chars_keeps_whole_lines(tmp_path):
    for i in range(20):
        history.append(tmp_path, "s", i, f"人{i}", "内容" * 8)
    out = history.tail(tmp_path, "s", limit=20, max_chars=50)
    assert len(out) <= 50
    assert out.splitlines()[0].startswith("人")


def test_flatten_and_empty(tmp_path):
    history.append(tmp_path, "s", 1, "甲", "第一行\n第二行")
    history.append(tmp_path, "s", 1, "甲", "   ")
    assert history.tail(tmp_path, "s") == "甲: 第一行 第二行"


def test_trim_when_file_grows(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_ROTATE_AT_BYTES", 200)
    for i in range(40):
        history.append(tmp_path, "s", i, f"人{i}", "x" * 30, max_lines=5)
    assert history.count(tmp_path, "s") <= 6
    assert "人39" in history.tail(tmp_path, "s", limit=10)


def test_missing_file(tmp_path):
    assert history.tail(tmp_path, "nobody") == ""
    assert history.count(tmp_path, "nobody") == 0


def test_scope_sanitised(tmp_path):
    p = history.scope_file(tmp_path, "group:110/abc")
    assert p.name == "group_110_abc.jsonl"
    assert "/" not in p.name and ":" not in p.name


def test_entries_are_json_lines(tmp_path):
    history.append(tmp_path, "s", 7, "甲", "你好")
    p = history.scope_file(tmp_path, "s")
    e = json.loads(p.read_text(encoding="utf-8").strip())
    assert e["uid"] == 7 and e["name"] == "甲" and e["text"] == "你好"
