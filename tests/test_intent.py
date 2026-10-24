# -*- coding: utf-8 -*-
"""闲聊 / 任务分流规则。"""
import pytest

from intent import classify


@pytest.mark.parametrize("text", [
    "今天好累",
    "在吗",
    "晚安",
    "哈哈哈哈",
    "你觉得我该先学 numpy 还是先学 pytorch？",
    "这个报错是什么意思？",
    "谢谢",
    "生日快乐！",
])
def test_chat(text):
    kind, _ = classify(text)
    assert kind == "chat"


@pytest.mark.parametrize("text", [
    "帮我列一下桌面上的文件",
    "改一下 bridge.py 的提示词",
    "打开 C:\\Users\\win11\\Desktop",
    "跑一下 pytest",
    "把 tasks.db 备份到桌面",
    "查一下 3001 端口是谁占的",
    "写个脚本把日志清空",
    "提醒我 20:30 交作业",
    "```python\nprint(1)\n```",
    "git status 看看有什么改动",
    "这个文件太大了吧，帮我压缩一下",
])
def test_task(text):
    kind, _ = classify(text)
    assert kind == "task"


def test_images_are_task():
    kind, _ = classify("这是啥", has_images=True)
    assert kind == "task"


def test_empty_is_chat():
    assert classify("")[0] == "chat"


def test_reason_is_returned():
    kind, why = classify("帮我看看磁盘")
    assert kind == "task" and why
