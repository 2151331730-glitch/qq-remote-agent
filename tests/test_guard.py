# -*- coding: utf-8 -*-
"""危险词/危险命令识别。宁可多问一句，但不能漏掉明显的破坏性操作。"""
import pytest

from guard import danger_reason_shell, danger_reason_task, audit, tail_events


@pytest.mark.parametrize("text,hit", [
    ("把桌面上的文件全删了", "删除"),
    ("清空回收站", "清空"),
    ("帮我格式化 D 盘", "格式化"),
    ("shutdown -s -t 0 执行一下", "shutdown"),
    ("rm -rf /tmp/x", "rm -rf"),
])
def test_task_danger_hits(text, hit):
    # 只要求"识别出来了"，不锁定具体命中哪个词（同义说法会持续补充）
    assert danger_reason_task(text) is not None


@pytest.mark.parametrize("text", [
    "把头像换成可爱的动漫女生",
    "列出桌面上的文件名",
    "写一个 Python 脚本算素数",
])
def test_task_safe(text):
    assert danger_reason_task(text) is None


@pytest.mark.parametrize("cmd", [
    "Remove-Item -Recurse C:\\temp",
    "del /s /q C:\\x",
    "diskpart",
    "reg delete HKLM\\Software\\Foo /f",
    "shutdown /s /t 0",
    "taskkill /f /im notepad.exe",
])
def test_shell_danger_hits(cmd):
    assert danger_reason_shell(cmd) is not None


@pytest.mark.parametrize("cmd", [
    "dir C:\\",
    "echo hello",
    "Get-Process | Select-Object -First 5",
])
def test_shell_safe(cmd):
    assert danger_reason_shell(cmd) is None


def test_audit_and_tail(tmp_path):
    p = tmp_path / "audit.jsonl"
    audit(p, event="task_start", id="abc", text="hi")
    audit(p, event="task_end", id="abc", ok=True)
    events = tail_events(p, 10)
    assert [e["event"] for e in events] == ["task_start", "task_end"]
    assert events[0]["id"] == "abc"
