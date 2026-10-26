# -*- coding: utf-8 -*-
"""语料挖掘脚本的筛选逻辑（tools/learn_style.py）。

不读桌面语料、不写任何文件：这里只喂合成的聊天行，检查它会不会把
机器人自己的话、命令、测试残留、别人的口癖、聊 AI 的句子混进样本。
那几刀是这套"自动学习"安全性的全部——滤掉了才敢每句话都注入提示词。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "learn_style.py"


@pytest.fixture(scope="module")
def ls():
    spec = importlib.util.spec_from_file_location("learn_style_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def row(text, uid=2151331730, name="主人", ts=1.0, src="group_x"):
    return {"uid": uid, "name": name, "text": text, "ts": ts, "_src": src}


def test_rejects_bot_commands_and_test_residue(ls):
    assert ls.usable(row("这个可以做咯", uid=3089461079)) == ""
    assert ls.usable(row("这个可以做咯", name="我")) == ""
    assert ls.usable(row("/状态")) == ""
    assert ls.usable(row("!dir")) == ""
    assert ls.usable(row("这个可以做咯", uid=12345)) == ""


def test_rejects_other_peoples_tics(ls):
    # 群里有人句尾爱加「喵」；跟着学立刻露馅
    assert ls.usable(row("好耶喵")) == ""
    assert ls.usable(row("行嘞")) == ""


def test_meta_filter_only_when_asked(ls):
    t = "你这么回太唐了"
    assert ls.usable(row(t), meta=False) == t        # 统计指标用宽档，样本量别砍太狠
    assert ls.usable(row(t), meta=True) == ""        # 样本用严档，别学"聊机器人自己"
    assert ls.usable(row("克劳德装不记得"), meta=True) == ""
    assert ls.usable(row("我今天弄了三个小时")) == "我今天弄了三个小时"


def test_stats_split_runs_per_speaker(ls):
    rows = [
        row("第一句", uid=1, ts=0),
        row("第二句", uid=1, ts=10),
        row("别人插一句", uid=2, ts=20),
        row("隔太久不算同一轮", uid=1, ts=20 + 600),
    ]
    st = ls.stats(rows)
    assert st["msgs"] == 4
    assert st["runs"] == 3
    assert st["run_share"][1] == pytest.approx(2 / 3)


def test_render_states_the_boundaries(ls):
    rows = [row("发咯", ts=0), row("主要做太久了", uid=2, ts=5)]
    st = ls.stats(rows)
    hi, lo = ls.vocab(rows)
    pairs, shorts = ls.pick_examples(rows)
    text = ls.render(st, hi, lo, pairs, shorts)
    assert "不是台词本" in text          # 学节奏不是照抄
    assert "别跟着任何人学口癖" in text
    assert "只学长度和语气" in text
