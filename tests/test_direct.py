# -*- coding: utf-8 -*-
"""直连闲聊通道与拟人节奏的测试（不联网：用假 transport）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import direct  # noqa: E402
import humantype  # noqa: E402


# ---------------- humantype：实测数据驱动的规则 ----------------
# 参考基线（275 条真人发言的统计）：句末标点 0%、含句号 1%、无标点 68%、
# 每条中位 11 字、84% 一轮只发 1 条。

def test_short_two_paragraphs_get_joined_with_comma():
    """两小句加起来不长时，真人会合成一条用逗号连，不会专门发两条。"""
    assert humantype.split_like_human("看电脑呢\n\n咋了") == ["看电脑呢，咋了"]
    assert humantype.split_like_human("行\n\n太费钱了") == ["行，太费钱了"]


def test_long_enough_paragraphs_stay_separate():
    """两句都有实质内容、加起来超过阈值时，分两条才是对的。"""
    parts = humantype.split_like_human(
        "之前是会话上限设小了，每句都被清空\n\n现在改大了就没那么频繁")
    assert len(parts) == 2
    assert parts[0].startswith("之前")


def test_never_more_than_two_bubbles():
    """真人只有 2% 的时候连发三条以上，所以最多两条。"""
    text = "\n\n".join([
        "这个功能之前老是掉线，查了好久才找到原因",
        "是会话上限设得太小了，每句话都在换会话",
        "后来把上限改大了就正常了",
    ])
    parts = humantype.split_like_human(text, max_parts=2)
    assert len(parts) == 2
    assert "后来" in parts[-1]          # 第三条并进第二条，不丢内容


def test_split_short_reply_stays_one_bubble():
    assert humantype.split_like_human("嗯") == ["嗯"]
    assert humantype.split_like_human("行，马上") == ["行，马上"]


def test_split_long_text_into_two():
    long = "这个功能之前老是掉线，后来查出来是会话上限设得太小了，" * 4
    parts = humantype.split_like_human(long, max_parts=2)
    assert 1 < len(parts) <= 2
    assert "".join(parts).replace(" ", "") == "".join(" ".join(long.split()).split())


def test_split_empty():
    assert humantype.split_like_human("") == []
    assert humantype.split_like_human("   \n\n  ") == []


# ---------------- humanize：去书面化 ----------------

def test_humanize_removes_periods():
    """真人 99% 不打句号，这是最刺眼的机器味。"""
    out = humantype.humanize("记下了。今天考试，别的先别管，专心考。")
    assert "。" not in out
    assert out == "记下了，今天考试，别的先别管，专心考"


def test_humanize_keeps_line_breaks():
    out = humantype.humanize("看电脑呢。\n\n咋了？")
    assert out == "看电脑呢\n\n咋了？"


def test_humanize_cleans_dangling_punctuation():
    out = humantype.humanize("我去看看，\n等下，")
    assert not out.endswith("，")
    assert "，，" not in out


def test_humanize_preserves_english_spacing():
    out = humantype.humanize("三个多亿 token，才十几块钱。")
    assert "token" in out
    assert "。" not in out


def test_humanize_empty():
    assert humantype.humanize("") == ""
    assert humantype.humanize("   ") == ""


def test_timeline_matches_parts_and_is_bounded():
    parts = ["嗯", "今天有点长的一句话得打一会儿吧"]
    delays = humantype.compose_timeline(parts)
    assert len(delays) == len(parts)
    assert all(0.4 <= d <= 6.0 for d in delays)


def test_first_delay_is_not_instant():
    """秒回是最明显的机器特征，首条必须有思考时间。"""
    for _ in range(50):
        assert humantype.typing_delay(5, first=True) >= 0.9


def test_typing_delay_longer_for_longer_text():
    short = humantype.typing_delay(3, first=True)
    long_ = humantype.typing_delay(200, first=True)
    assert long_ >= short


# ---------------- direct ----------------

def _fake_response(payload, stream=False):
    class R:
        def __init__(self, body):
            self._body = body
            self._lines = iter(body.splitlines(keepends=True))

        def read(self):
            return self._body.encode("utf-8")

        def __iter__(self):
            return iter(self._lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    if stream:
        events = [
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 123, "output_tokens": 0}}},
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": payload}},
            {"type": "message_delta", "usage": {"output_tokens": 45}},
        ]
        return R("".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
                         for e in events))
    return R(json.dumps({"content": [{"type": "text", "text": payload}],
                         "usage": {"input_tokens": 123, "output_tokens": 45}},
                        ensure_ascii=False))


def _mkchat(monkeypatch, reply="看电脑呢", stream=False):
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k", "stream": stream, "history_turns": 4})
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _fake_response(reply, stream=stream)
    monkeypatch.setattr(direct.urllib.request, "urlopen", fake_urlopen)
    return c, sent


def test_ask_adds_turns_and_tracks_usage(monkeypatch):
    c, sent = _mkchat(monkeypatch)
    reply, ok = c.ask("owner", "在干嘛", "SYSTEM")
    assert ok and reply == "看电脑呢"
    assert c.last_usage == {"in": 123, "out": 45}
    assert sent[0]["system"] == "SYSTEM"
    assert sent[0]["messages"][-1]["content"] == "在干嘛"
    # 一轮之后历史里有一问一答
    assert [m["role"] for m in c.history_for("owner")] == ["user", "assistant"]


def test_ask_keeps_context_between_turns(monkeypatch):
    c, sent = _mkchat(monkeypatch)
    c.ask("owner", "第一句", "S")
    c.ask("owner", "第二句", "S")
    msgs = sent[1]["messages"]
    assert [m["content"] for m in msgs][:2] == ["第一句", "看电脑呢"]
    assert msgs[-1]["content"] == "第二句"


def test_history_is_capped(monkeypatch):
    c, _ = _mkchat(monkeypatch)
    for i in range(30):
        c.ask("owner", f"第{i}句", "S")
    # history_turns=4 → 最多 8 条
    assert len(c.history_for("owner")) <= 8


def test_scopes_are_isolated(monkeypatch):
    c, _ = _mkchat(monkeypatch)
    c.ask("owner", "主人的话", "S")
    c.ask("group:1", "群里的话", "S")
    assert all("主人的话" != m["content"] for m in c.history_for("group:1"))


def test_failure_rolls_back_the_user_turn(monkeypatch):
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})

    def boom(req, timeout=None):
        raise OSError("网络断了")
    monkeypatch.setattr(direct.urllib.request, "urlopen", boom)
    reply, ok = c.ask("owner", "在吗", "S")
    assert (reply, ok) == ("", False)
    assert c.last_error
    assert c.history_for("owner") == []      # 失败的话不留在上下文里


def test_seed_history_from_transcript():
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})
    n = c.seed_history("owner", "主人: 在干嘛\n我: 看电脑呢\n主人: 那个能用不")
    h = c.history_for("owner")
    # 3 条对话 + 1 条中性收尾（把"上次没回答的提问"关掉）
    assert n == 4
    assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
    # 末尾不能停在 user，否则模型会把那条旧问题和新消息合并作答
    assert h[-1]["role"] == "assistant"
    c.seed_history("owner2", "我: 在的\n我: 咋了这是")
    # 全是"我"说的话 → 没有对方的对话，只剩收尾，等于没有可用上下文
    assert c.history_for("owner2") in ([], [{"role": "assistant", "content": "嗯"}])


def test_seed_merges_consecutive_same_role():
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})
    c.seed_history("g", "某人: 吃了吗\n某人: 还没呢\n我: 我也没吃\n我: 一起去\n某人: 行啊")
    roles = [m["role"] for m in c.history_for("g")]
    # 三段对话角色 + 收尾的 assistant
    assert roles == ["user", "assistant", "user", "assistant"]


def test_not_available_without_key(monkeypatch):
    # 本机 ~/.claude/settings.json 里有真 key，要禁掉自动兜底才能测这条
    monkeypatch.setattr(direct, "_find_key_from_settings", lambda: ("", ""))
    c = direct.DirectChat({"enabled": True, "base_url": "", "api_key": ""})
    assert not c.available()
    assert c.ask("owner", "hi", "S") == ("", False)


def test_stream_mode_collects_deltas(monkeypatch):
    c, _ = _mkchat(monkeypatch, reply="嗯", stream=True)
    chunks = []
    reply, ok = c.ask("owner", "在吗", "S", on_delta=chunks.append)
    assert ok and reply == "嗯"
    assert "".join(chunks) == "嗯"
    assert c.last_usage["in"] == 123


def test_reset_clears_all_scopes(monkeypatch):
    c, _ = _mkchat(monkeypatch)
    c.ask("owner", "a", "S")
    c.ask("group:1", "b", "S")
    c.reset()
    assert c.history_for("owner") == []
    assert c.history_for("group:1") == []


# ---------------- 历史持久化 ----------------

def test_history_survives_restart(monkeypatch, tmp_path):
    """重启后上下文还在——这是"上下文短"的最后一个漏洞。"""
    hp = tmp_path / "hist.json"
    cfg = {"enabled": True, "base_url": "https://x.invalid", "api_key": "k",
           "history_path": str(hp)}
    a = direct.DirectChat(cfg)
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda req, timeout=None: _fake_response("看电脑呢"))
    a.ask("owner", "在干嘛", "S")

    b = direct.DirectChat(cfg)          # 模拟进程重启
    h = b.history_for("owner")
    assert [m["content"] for m in h] == ["在干嘛", "看电脑呢"]


def test_reset_is_persisted(monkeypatch, tmp_path):
    hp = tmp_path / "hist.json"
    cfg = {"enabled": True, "base_url": "https://x.invalid", "api_key": "k",
           "history_path": str(hp)}
    a = direct.DirectChat(cfg)
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda req, timeout=None: _fake_response("嗯"))
    a.ask("owner", "在吗", "S")
    a.reset()
    a.save(force=True)
    b = direct.DirectChat(cfg)
    assert b.history_for("owner") == []


def test_corrupt_history_file_does_not_crash(tmp_path):
    hp = tmp_path / "bad.json"
    hp.write_text("{not json", encoding="utf-8")
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k", "history_path": str(hp)})
    assert c.history_for("owner") == []


def test_history_file_skips_bad_entries(tmp_path, monkeypatch):
    hp = tmp_path / "h.json"
    hp.write_text(json.dumps({"owner": [
        {"role": "user", "content": "好"},
        {"role": "bogus", "content": "x"},
        {"role": "assistant", "content": ""},
        "nonsense",
    ]}, ensure_ascii=False), encoding="utf-8")
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k", "history_path": str(hp)})
    assert c.history_for("owner") == [{"role": "user", "content": "好"}]


# ---------------- 垫底上下文过滤 ----------------

def test_seed_drops_commands_and_noise():
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})
    t = "\n".join([
        "主人: /心跳",
        "主人: !echo hi",
        "主人: 👍",
        "主人: ok",
        "主人: [CQ:image,file=x]",
        "主人: Traceback (most recent call last):",
        "主人: 今天考试",
        "我: 记下了",
    ])
    c.seed_history("owner", t)
    contents = " ".join(m["content"] for m in c.history_for("owner"))
    assert "今天考试" in contents
    for junk in ("/心跳", "!echo", "👍", "CQ:image", "Traceback"):
        assert junk not in contents


def test_seed_keeps_meaningful_short_replies():
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k"})
    c.seed_history("owner", "某: 行\n某: 我明天考试\n某: 那祝你顺利")
    contents = " ".join(m["content"] for m in c.history_for("owner"))
    assert "明天考试" in contents
    assert "祝你顺利" in contents


# ---------------- 分流与回退的接线（防止签名不匹配这类"只在 CI 才炸"的问题）--------------

def test_execute_falls_back_with_the_right_signature(monkeypatch):
    """直连不可用时必须能真的回退到 claude 路径。

    回归自一个真实事故：`_execute()` 调用 `_execute_claude_chat(row, text, cwd)`
    多传了一个参数，而直连可用时根本走不到那行 —— 本地（有 key）一直绿，
    CI（无 key）必然 TypeError，worker 静默死掉、任务卡在 running。
    """
    import asyncio
    import bridge as br

    called = {}

    async def fake_claude_chat(row, cwd):
        called["args"] = (row, cwd)
        return "claude-lane"

    monkeypatch.setattr(br, "OWNER_DIRECT", False)
    monkeypatch.setattr(br, "_execute_claude_chat", fake_claude_chat)
    monkeypatch.setattr(br, "_execute_direct_chat", None, raising=False)

    row = {"id": "t1", "text": "在吗", "kind": "chat"}
    out = asyncio.run(br._execute(row, r"C:\tmp"))
    assert out == "claude-lane"
    assert called["args"][1] == r"C:\tmp"


def test_direct_failure_falls_back_too(monkeypatch):
    """直连配置在但调用失败时，同样要落回 claude 路径，不能卡住。"""
    import asyncio
    import bridge as br

    async def boom(*a, **k):
        return "", False

    calls = []

    async def fake_claude_chat(row, cwd):
        calls.append(cwd)
        return "fallback"

    monkeypatch.setattr(br, "OWNER_DIRECT", True)
    monkeypatch.setattr(br, "DIRECT", type("D", (), {
        "available": lambda self: True,
        "history_for": lambda self, s: [],
        "seed_history": lambda self, s, t: 0,
        "ask": lambda *a, **k: (_ for _ in ()).throw(OSError("net down")),
        "last_usage": {"in": 0, "out": 0},
        "last_error": "net down",
    })())
    monkeypatch.setattr(br, "_execute_claude_chat", fake_claude_chat)
    monkeypatch.setattr(br, "_file_context", lambda scope: "")

    row = {"id": "t2", "text": "在吗", "kind": "chat", "source": "qq"}
    out = asyncio.run(br._execute(row, r"C:\tmp"))
    assert out == "fallback"
    # 直连内部回退时用 WORKDIR 作为工作目录（历史遗留行为，不是调用方的 cwd）。
    # 这条测试只保证"确实回退了、没有再卡在直连里"。
    assert len(calls) == 1


# ---------------- 内部状态泄漏与敷衍检测 ----------------

@pytest.mark.parametrize("text", [
    "（任务完成，没有文字输出）",
    "（没有回话）",
    "（没说出话来）",
])
def test_internal_state_is_never_sent(text):
    """这些是程序语言，真人不会这么说话。"""
    assert humantype.looks_like_leak(text)


@pytest.mark.parametrize("text", [
    "改好了，能用了", "行，那我闭嘴，等你放片头", "在呢 咋了",
])
def test_normal_reply_is_not_a_leak(text):
    assert not humantype.looks_like_leak(text)


@pytest.mark.parametrize("text", [
    "行 那就不说了", "嗯", "好的", "知道了", "可以",
])
def test_thin_replies_detected(text):
    """只应声、没有信息量。"""
    assert humantype.is_thin(text)


@pytest.mark.parametrize("text", [
    "行，那我闭嘴，等你放片头",
    "改好了，重启就生效",
    "在呢 咋了",
])
def test_replies_with_substance_are_not_thin(text):
    """真人也会用「行」开头，但后面一定接实质内容。"""
    assert not humantype.is_thin(text)


# ---------------- 防复读：看历史纠偏 ----------------

def test_opener_takes_first_clause():
    assert humantype.opener("行，那我闭嘴") == "行"
    assert humantype.opener("看电脑呢，咋了") == "看电脑呢"
    assert humantype.opener("") == ""


def test_question_detection_handles_spoken_chinese():
    assert humantype.is_question("你看看这个？")
    assert humantype.is_question("你咋了")
    assert humantype.is_question("是不是卡住了")
    assert not humantype.is_question("那算啥")       # 陈述句，别当反问
    assert not humantype.is_question("行，我唐，你说了算")


def test_repeat_against_recent_replies():
    prev = ["行，我唐，你说了算", "早说了弄不了"]
    assert humantype.is_repeat("行，我唐，你说了算", prev)
    assert humantype.is_repeat("行 我唐 你说了算。", prev)      # 标点不同也算
    assert not humantype.is_repeat("看电脑呢", prev)           # 太短不判


def test_style_note_flags_repeated_openers_and_questions():
    same = [{"role": "assistant", "content": "行，那我看看"}] * 3
    assert "开头" in humantype.style_note(same)
    asks = [{"role": "assistant", "content": "咋了？"},
            {"role": "assistant", "content": "在哪呢"}]
    assert "反问" in humantype.style_note(asks)
    assert humantype.style_note([]) == ""


def test_retry_replaces_last_assistant_turn(monkeypatch):
    """重写时不往历史里塞「刚才那句太敷衍」这种提醒——那会把对话记录弄脏。"""
    c, sent = _mkchat(monkeypatch, reply="第二版")
    c.ask("owner", "在吗", "S")
    c.history_for("owner")[-1]["content"] = "第一版"
    reply, ok = c.ask("owner", "在吗", "S", guide="换个说法", retry=True)
    assert ok and reply == "第二版"
    hist = c.history_for("owner")
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[-1]["content"] == "第二版"
    assert sent[-1]["system"].endswith("换个说法")
    assert "换个说法" not in json.dumps(hist, ensure_ascii=False)


def test_failed_retry_keeps_the_original_reply(monkeypatch):
    c, _ = _mkchat(monkeypatch, reply="第一版")
    c.ask("owner", "在吗", "S")

    def boom(req, timeout=None):
        raise OSError("断了")
    monkeypatch.setattr(direct.urllib.request, "urlopen", boom)
    assert c.ask("owner", "在吗", "S", retry=True) == ("", False)
    hist = c.history_for("owner")
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[-1]["content"] == "第一版"


def test_replace_last_assistant_stores_delivered_text(monkeypatch):
    """发出去的是去过标点、合过条的版本，历史里也该存那一版。"""
    c, _ = _mkchat(monkeypatch, reply="行。那我看看")
    c.ask("owner", "在吗", "S")
    c.replace_last_assistant("owner", "行，那我看看")
    assert c.history_for("owner")[-1]["content"] == "行，那我看看"
    c.replace_last_assistant("nobody", "x")     # 空会话不炸


# ---------------- 历史总字数预算（2026-10-27）----------------
# 背景：条数封顶挡不住"20 条长消息"。真正决定每次说话贵不贵的是总字数，
# 所以 ask() 在发出去之前还会按预算从最旧的一端再砍一刀。

def test_trim_budget_keeps_short_conversation():
    msgs = [{"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "在"},
            {"role": "user", "content": "干嘛呢"}]
    assert direct.trim_budget(msgs, 100) == msgs


def test_trim_budget_drops_from_the_oldest_end():
    msgs = [{"role": "user", "content": "老" * 50},
            {"role": "assistant", "content": "答" * 50},
            {"role": "user", "content": "新" * 10}]
    out = direct.trim_budget(msgs, 60)
    assert out[-1]["content"] == "新" * 10
    assert sum(len(m["content"]) for m in out) <= 60


def test_trim_budget_never_drops_the_last_message():
    # 这一条就是对方刚说的话，砍掉等于问了个空问题
    msgs = [{"role": "user", "content": "x" * 5000}]
    out = direct.trim_budget(msgs, 10)
    assert len(out) == 1 and out[0]["content"] == "x" * 5000


def test_trim_budget_starts_with_user():
    # 砍完可能正好落在 assistant 上；Anthropic 要求第一条必须是 user
    msgs = [{"role": "user", "content": "u" * 10},
            {"role": "assistant", "content": "a" * 10},
            {"role": "user", "content": "u" * 10},
            {"role": "assistant", "content": "a" * 10},
            {"role": "user", "content": "u" * 10}]
    out = direct.trim_budget(msgs, 20)
    assert out[0]["role"] == "user"
    assert out[-1]["role"] == "user"


def test_trim_budget_does_not_mutate_input():
    msgs = [{"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 10}]
    direct.trim_budget(msgs, 20)
    assert len(msgs) == 2


def test_ask_sends_at_most_the_char_budget(monkeypatch):
    c = direct.DirectChat({"enabled": True, "base_url": "https://x.invalid",
                           "api_key": "k", "stream": False,
                           "history_turns": 20, "history_max_chars": 120})
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _fake_response("好", stream=False)
    monkeypatch.setattr(direct.urllib.request, "urlopen", fake_urlopen)

    for i in range(10):
        c.ask("owner", f"第{i}句" + "啊" * 100, "S")
    total = sum(len(m["content"]) for m in sent[-1]["messages"])
    assert total <= 300          # 120 预算 + 这次新消息本身
    assert sent[-1]["messages"][-1]["content"].startswith("第9句")
