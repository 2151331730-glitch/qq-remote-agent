# -*- coding: utf-8 -*-
"""/读：直接把本机文件内容发回 QQ。

这一套的核心是**不经过模型**，所以测试全在本地跑，不需要 MockLLM 或 claude.exe。
重点覆盖三件容易出错的事：
1. 路径解析（相对路径要按 /cd 的工作目录算，不是按进程 cwd）；
2. 编码（Windows 上中文文件有一半是 GBK）；
3. 长文件不许一次全塞进聊天（默认只发前 120 行，并给出接着看的命令）。
"""
import asyncio

import pytest

import bridge


@pytest.fixture
def sent(monkeypatch):
    """把 send 换掉：收集发出去的消息，不连 NapCat。"""
    box = []

    async def fake_send(text):
        box.append(text)
        return True

    monkeypatch.setattr(bridge, "send", fake_send)
    return box


def _read(arg):
    asyncio.run(bridge._handle_read_file(arg))


def test_resolve_relative_uses_state_cwd(tmp_path, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    p = bridge._resolve_local("笔记.md")
    assert p == tmp_path / "笔记.md"


def test_resolve_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert bridge._resolve_local("~") == tmp_path


def test_resolve_absolute_untouched(tmp_path):
    f = tmp_path / "a.md"
    assert bridge._resolve_local(str(f)) == f


@pytest.mark.parametrize("s,expect", [
    (r"E:\x.md 10-50", (r"E:\x.md", 10, 50)),
    (r"E:\x.md:10-50", (r"E:\x.md", 10, 50)),
    (r"E:\x.md 10", (r"E:\x.md", 10, 10)),
    ("笔记.md 3~8", ("笔记.md", 3, 8)),
    ("没有行号.md", None),
])
def test_split_line_range(s, expect):
    assert bridge._split_line_range(s) == expect


@pytest.mark.parametrize("raw,enc", [
    ("中文内容\n第二行".encode("utf-8"), "utf-8-sig"),
    ("中文内容\n第二行".encode("gbk"), "gbk"),
    ("中文内容\n第二行".encode("utf-16"), "utf-16"),
])
def test_decode_text(raw, enc):
    text, got = bridge._decode_text(raw)
    assert text.splitlines()[0] == "中文内容"
    assert got == enc


def test_sends_whole_small_file(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    (tmp_path / "小.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    _read("小.md")
    assert len(sent) == 1
    assert "# 标题" in sent[0] and "正文" in sent[0]
    assert "接着看" not in sent[0]


def test_long_file_previews_and_hints(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    lines = [f"第 {i} 行" for i in range(1, 301)]
    (tmp_path / "长.md").write_text("\n".join(lines), encoding="utf-8")
    _read("长.md")
    msg = sent[0]
    assert "共 300 行" in msg
    assert "第 1 行" in msg and "第 120 行" in msg
    assert "第 121 行" not in msg
    assert "/读 长.md 121-240" in msg


def test_line_range(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    lines = [f"第 {i} 行" for i in range(1, 301)]
    (tmp_path / "长.md").write_text("\n".join(lines), encoding="utf-8")
    _read("长.md 250-255")
    msg = sent[0]
    assert "第 250 行" in msg and "第 255 行" in msg
    assert "第 249 行" not in msg and "第 256 行" not in msg


def test_range_clamped_and_order_fixed(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    (tmp_path / "三行.md").write_text("a\nb\nc\n", encoding="utf-8")
    _read("三行.md 2-999")
    assert "b" in sent[0] and "c" in sent[0]
    _read("三行.md 5-2")
    assert "找不到" not in sent[-1]


def test_directory_lists_entries(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    (tmp_path / "子目录").mkdir()
    (tmp_path / "文件.txt").write_text("x", encoding="utf-8")
    _read(str(tmp_path))
    msg = sent[0]
    assert "📁 子目录" in msg and "📄 文件.txt" in msg


def test_missing_path_says_so(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    _read("不存在.md")
    assert "找不到" in sent[0]


def test_binary_rejected(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    (tmp_path / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    _read("图.png")
    assert "二进制" in sent[0]


def test_empty_arg_prints_usage(sent):
    _read("")
    assert "用法" in sent[0]


def test_quoted_path_with_spaces_and_range(tmp_path, sent, monkeypatch):
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    d = tmp_path / "有 空格"
    d.mkdir()
    (d / "a.md").write_text("aa\nbb\ncc\ndd\n", encoding="utf-8")
    _read(f'"{d / "a.md"}" 2-3')
    msg = sent[0]
    assert "bb" in msg and "cc" in msg and "dd" not in msg
    # 续读提示里的路径要被引号包住且只有一个引号对，否则复制回来说不通
    assert '""' not in msg
    assert f'/读 "{d / "a.md"}" 4-4' in msg


def test_command_dispatch_reaches_reader(tmp_path, sent, monkeypatch):
    """`/读` 要真的被 handle_command 接住，而不是当普通消息丢给模型。"""
    monkeypatch.setitem(bridge.STATE, "cwd", str(tmp_path))
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    assert asyncio.run(bridge.handle_command("/读 a.md")) is True
    assert "hello" in sent[0]
    assert asyncio.run(bridge.handle_command("/read a.md")) is True
