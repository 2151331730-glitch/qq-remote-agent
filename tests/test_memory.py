# -*- coding: utf-8 -*-
"""长期记忆文件的读写语义。"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
memory = importlib.import_module("memory")


def test_load_missing_file_returns_empty(tmp_path):
    assert memory.load(tmp_path / "nope.md") == ""


def test_append_and_load(tmp_path):
    p = tmp_path / "m.md"
    memory.append(p, "喜欢喝美式")
    memory.append(p, "周三下午有课")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# ")
    body = memory.load(p)
    assert "喜欢喝美式" in body and "周三下午有课" in body
    assert body.splitlines()[0].startswith("- ")
    assert len(body.splitlines()) == 2


def test_append_flattens_newlines(tmp_path):
    p = tmp_path / "m.md"
    memory.append(p, "第一行\n第二行")
    assert "第一行 第二行" in memory.load(p)
    assert len(memory.load(p).splitlines()) == 1


def test_duplicate_last_entry_ignored(tmp_path):
    p = tmp_path / "m.md"
    memory.append(p, "爱吃辣")
    memory.append(p, "爱吃辣")
    assert len(memory.load(p).splitlines()) == 1


def test_empty_rejected(tmp_path):
    try:
        memory.append(tmp_path / "m.md", "   \n ")
    except ValueError:
        return
    raise AssertionError("空内容应抛 ValueError")


def test_limit_drops_oldest(tmp_path):
    p = tmp_path / "m.md"
    for i in range(50):
        memory.append(p, f"第 {i} 条" + "x" * 40, max_chars=300)
    body = memory.load(p)
    assert len(body) <= 320
    assert "第 49 条" in body
    assert "第 0 条" not in body
