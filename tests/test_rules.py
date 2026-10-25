# -*- coding: utf-8 -*-
"""行为规则与记忆的学习（从 QQ 聊天里学）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import memory  # noqa: E402
import rules  # noqa: E402


# ---------------- 识别：什么算"在纠正我" ----------------

@pytest.mark.parametrize("text", [
    "下次把这句话省了",
    "我说把这句话省了",        # 重复一遍自己的纠正
    "以后别加那句",
    "别每次都问我要不要",
    "你每次都问我要不要",
    "太长了，短点",
    "我说过别加这句",
])
def test_explicit_correction_recognised(text):
    hit, kind = rules.looks_like_correction(text)
    assert hit and kind == "explicit"


@pytest.mark.parametrize("text", [
    "换记忆不用专门告诉我",
    "我不喜欢被追问",
    "说重点",
])
def test_soft_signal_recognised(text):
    hit, kind = rules.looks_like_correction(text)
    assert hit and kind == "soft"


@pytest.mark.parametrize("text", [
    "现在几点", "北京海淀现在天气怎么样", "/心跳", "!echo hi",
    "只回复三个字：桥可用", "你好", "在干嘛", "桥可用",
])
def test_normal_messages_are_not_corrections(text):
    hit, _ = rules.looks_like_correction(text)
    assert not hit


# ---------------- 翻译成规则 ----------------

def test_infer_open_ended():
    r = rules.infer_rule("下次把这句话省了")
    assert r and r["kind"] == "no_open_ended"


def test_infer_no_question():
    r = rules.infer_rule("别每次都问我要不要")
    assert r and r["kind"] == "no_question"


def test_infer_max_chars_explicit_number():
    r = rules.infer_rule("以后回复要 40 字以内")
    assert r and r["kind"] == "max_chars" and r["value"] == 40


def test_infer_returns_none_when_unsure():
    """认不准就不造规则——落回记忆，不冒险改行为。"""
    assert rules.infer_rule("我说的是这个意思吗") is None


# ---------------- 规则生效 ----------------

def _store(tmp_path):
    return rules.RuleStore(tmp_path / "rules.json")


def test_no_open_ended_strips_the_tail(tmp_path):
    s = _store(tmp_path)
    s.add("no_open_ended", True)
    out, fired = s.apply("改好了，重启就生效。要不要我顺手把日志清一下")
    assert "要不要" not in out
    assert out.startswith("改好了")
    assert fired


def test_no_open_ended_keeps_normal_reply(tmp_path):
    s = _store(tmp_path)
    s.add("no_open_ended", True)
    out, fired = s.apply("改好了，能用了")
    assert out == "改好了，能用了"
    assert not fired


def test_no_question_strips_short_question_tail(tmp_path):
    s = _store(tmp_path)
    s.add("no_question", True)
    out, _ = s.apply("弄完了。还有别的吗")
    assert "还有别的吗" not in out


def test_max_chars_truncates_at_break(tmp_path):
    s = _store(tmp_path)
    s.add("max_chars", 20)
    out, fired = s.apply("这句话特别长特别长，中间有个逗号，后面还有很长很长的一段内容")
    assert len(out) <= 20
    assert fired


def test_no_word_removes_and_tidies(tmp_path):
    s = _store(tmp_path)
    s.add("no_word", "好的")
    out, _ = s.apply("好的，我看下")
    assert "好的" not in out
    assert out.startswith("我看下")


def test_rules_persist_across_instances(tmp_path):
    p = tmp_path / "rules.json"
    a = rules.RuleStore(p)
    a.add("no_question", True)
    b = rules.RuleStore(p)
    assert len(b.rules) == 1


def test_duplicate_rule_not_stacked(tmp_path):
    s = _store(tmp_path)
    ok1, _ = s.add("no_question", True)
    ok2, why = s.add("no_question", True)
    assert ok1 and not ok2 and "已经" in why
    assert len(s.rules) == 1


def test_daily_quota(tmp_path):
    """防止短时间内被灌爆：一天最多自动学 N 条。"""
    s = _store(tmp_path)
    added = sum(1 for i in range(rules.MAX_NEW_PER_DAY + 3)
                if s.add("no_word", f"词{i}")[0])
    assert added == rules.MAX_NEW_PER_DAY


def test_remove_and_clear(tmp_path):
    s = _store(tmp_path)
    s.add("no_question", True)
    s.add("no_word", "x")
    assert s.remove(1)
    assert len(s.rules) == 1
    assert s.clear() == 1
    assert s.rules == []


def test_unknown_kind_rejected(tmp_path):
    s = _store(tmp_path)
    ok, why = s.add("make_it_funny", True)
    assert not ok and "未知" in why


def test_apply_on_empty_text(tmp_path):
    s = _store(tmp_path)
    assert s.apply("") == ("", [])


# ---------------- 记忆：注释不能当记忆 ----------------

def test_memory_ignores_comments(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("# 标题\n# 说明：这是私人内容\n\n- 真实记忆\n", encoding="utf-8")
    assert memory.load(p) == "- 真实记忆"


def test_memory_entries_and_remove(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("# 标题\n- 第一条\n- 第二条\n", encoding="utf-8")
    assert memory.entries(p) == ["- 第一条", "- 第二条"]
    assert memory.remove(p, "第一条") == 1
    assert memory.entries(p) == ["- 第二条"]


def test_memory_remove_unknown_is_noop(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("- 只有这条\n", encoding="utf-8")
    assert memory.remove(p, "不存在") == 0
    assert memory.entries(p) == ["- 只有这条"]


def test_memory_append_dedupes_consecutive(tmp_path):
    p = tmp_path / "m.md"
    memory.append(p, "周三有课")
    memory.append(p, "周三有课")
    assert len(memory.entries(p)) == 1
