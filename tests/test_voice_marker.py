# -*- coding: utf-8 -*-
"""@@语音:…@@ 标记：模型说「这句我想用嘴说」的那套写法。

和 @@表情:名字@@ 同一套机制——bridge 摘掉标记，单独发一条语音条。
这里只测纯函数；真发语音要看 send_voice（需要 NapCat，不在单测里跑）。
"""
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import tts  # noqa: E402


def test_extract_plain_marker():
    rest, say = tts.extract("今天真的累死了\n@@语音:唉 累死了@@")
    assert say == "唉 累死了"
    assert "@@" not in rest
    assert "累死了" in rest


def test_extract_fullwidth_colon_and_spaces():
    _, say = tts.extract("@@ 语音 ： 你吃饭了吗 @@")
    assert say == "你吃饭了吗"


def test_extract_only_first_marker():
    rest, say = tts.extract("@@语音:第一句@@\n@@语音:第二句@@")
    assert say == "第一句"
    assert "第二句" in rest        # 第二条原样留着，方便发现模型在滥用


def test_extract_without_marker_is_noop():
    text = "就普通一句话"
    assert tts.extract(text) == (text, "")


def test_scrub_removes_malformed_marker():
    # 少了闭合的 @、写成全角：这些绝不能当文字发到手机上
    assert tts.scrub("好@@语音:嗯@@哦") == "好哦"
    assert tts.scrub("正常文本") == "正常文本"


def test_rules_text_mentions_the_marker():
    rule = tts.rules_text()
    assert "@@语音:" in rule
    assert "@@语音:" in tts.rules_text(public=True)


def test_clean_text_drops_marker_fragments():
    # 念出来的文本里不该夹着标记本身
    assert "@@" not in tts.clean_text("你好@@语音:嗯@@")
