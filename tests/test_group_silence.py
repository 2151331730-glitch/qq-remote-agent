# -*- coding: utf-8 -*-
"""群聊"沉得住气"的四条规则。

1. 模型说 PASS → 群里什么都不发（真沉默，不是发一句"我不想说"）。
2. 被 @ 却 PASS → 至少应一声，否则对方以为机器人掉线。
3. 消息 @ 了群里别人 → 概率插话不接（别抢话）。
4. 刚说完一句 → 接下来 GROUP_AFTER_SPEAK 秒内不再主动插话。

bridge.py 导入时读配置、建队列，所以先用临时配置导进来（同 test_prompt.py 的套路）。
"""
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

SELF = 10000        # 机器人自己的 QQ


def _load(tmp_path, monkeypatch):
    cfg = json.loads((BRIDGE / "config.example.json").read_text(encoding="utf-8"))
    mem = tmp_path / "memory.md"
    mem.write_text("", encoding="utf-8")
    cfg.update({
        "claude_exe": "claude",
        "workdir": str(tmp_path),
        "log_dir": str(tmp_path / "logs"),
        "db_path": str(tmp_path / "t.db"),
        "memory_path": str(mem),
        "instance_lock_port": 0,
        "panel_port": 0,
        "owner_uin": 0,
        # 别把测试消息写进真实的聊天记录 / 表情库
        "transcript_dir": str(tmp_path / "transcripts"),
        "sticker_dir": str(tmp_path / "stickers"),
        "persona_path": str(tmp_path / "persona.md"),
        "public_persona_path": str(tmp_path / "public_persona.md"),
    })
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("QQ_REMOTE_CONFIG", str(cfg_path))
    monkeypatch.setenv("QQ_REMOTE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("QQ_REMOTE_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("QQ_REMOTE_REMINDERS", str(tmp_path / "reminders.db"))
    monkeypatch.setenv("QQ_REMOTE_NO_LOCK", "1")
    sys.modules.pop("bridge", None)
    return importlib.import_module("bridge")


class FakeDirect:
    """直连通道的替身：按顺序吐出预设的回复，不碰网络。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.last_usage = {"in": 7, "out": 2}
        self.last_error = ""
        self.seen_prompt = ""

    def available(self):
        return True

    def history_for(self, scope):
        return [("对方", "前情")]        # 非空 → 不会去 seed

    def seed_history(self, scope, text):
        pass

    def ask(self, scope, text, sys_prompt, on_delta=None, guide="", retry=False):
        self.seen_prompt = sys_prompt
        return self.replies.pop(0) if self.replies else ("", False)


def _wire(b, monkeypatch, replies):
    """把 handle_public 的外部依赖全部换成替身，返回记录发出的消息的列表。"""
    sent = []

    async def fake_send(target_type, target_id, body):
        sent.append(body)
        return True

    async def fake_history(*a, **k):
        return ""

    fake = FakeDirect(replies)
    monkeypatch.setattr(b, "PUBLIC_DIRECT", True)
    monkeypatch.setattr(b, "DIRECT", fake)
    monkeypatch.setattr(b, "_send_bubbles", fake_send)
    monkeypatch.setattr(b, "_file_context", lambda scope: "")
    monkeypatch.setattr(b, "fetch_history", fake_history)
    b.PUBLIC_STATE["last"].clear()
    b.PUBLIC_STATE["stamps"].clear()
    b.PUBLIC_STATE["pending"] = 0
    return sent, fake


def _msg(text, at=None):
    segs = []
    for qq in (at or []):
        segs.append({"type": "at", "data": {"qq": str(qq)}})
    segs.append({"type": "text", "data": {"text": text}})
    return {"group_id": 7, "user_id": 42, "sender": {"nickname": "甲"}, "message": segs}


# ---------------- handle_public 里的 PASS ----------------

def test_group_pass_sends_nothing(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    sent, _ = _wire(b, monkeypatch, [("PASS", True)])
    ok = asyncio.run(b.handle_public("group:7", "group", 7, "今天好热啊",
                                     who="甲(42)", addressed=False))
    assert ok is False
    assert sent == []


def test_group_pass_when_addressed_still_answers(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    sent, _ = _wire(b, monkeypatch, [("（PASS）", True)])
    ok = asyncio.run(b.handle_public("group:7", "group", 7, "你来看看这个",
                                     who="甲(42)", addressed=True))
    assert ok is True
    assert len(sent) == 1
    assert sent[0] in b.policy.HELLO_LINES


def test_group_prompt_carries_the_pass_rule(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    _, fake = _wire(b, monkeypatch, [("嗯好", True)])
    asyncio.run(b.handle_public("group:7", "group", 7, "随便聊聊"))
    assert "PASS" in fake.seen_prompt
    # 陌生人私聊没有这条出口：那种场景必须给答复
    _, fake2 = _wire(b, monkeypatch, [("嗯好", True)])
    asyncio.run(b.handle_public("user:99", "private", 99, "你好"))
    assert "PASS" not in fake2.seen_prompt


# ---------------- on_group_message 里的三条闸 ----------------

def _spy(b, monkeypatch):
    calls = []

    async def fake_hp(scope, target_type, target_id, text, who="", addressed=False):
        calls.append({"scope": scope, "text": text, "addressed": addressed})
        return True

    monkeypatch.setattr(b, "handle_public", fake_hp)
    monkeypatch.setattr(b, "GROUP_POLICY", "open")     # 否则 mention 压根不接别人
    # 配置里有 group_level（默认 1）时，档位优先于 group_policy：
    # 不过档就 return，下面这些「过闸后有没有交给 handle_public」的断言全部落空。
    monkeypatch.setattr(b, "GROUP_LEVEL", None)
    monkeypatch.setattr(b, "GROUP_LEVEL_OVERRIDES", {})
    # 攒批关掉：这几条测的是「过闸之后有没有交给 handle_public」，
    # 攒批开着的话消息会先落进缓冲区（那也是对的，但 spy 就看不到了）。
    # 攒批本身有 tests/test_policy.py 里的独立用例。
    monkeypatch.setattr(b, "GROUP_BATCH_ENABLED", False)
    b.PUBLIC_STATE["last"].clear()
    return calls


def test_at_other_is_not_interrupted(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    asyncio.run(b.on_group_message(_msg("你看看这个怎么办", at=[555]), SELF))
    assert calls == []


def test_at_me_still_reaches_handle_public(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    asyncio.run(b.on_group_message(_msg("你来看看这个", at=[SELF]), SELF))
    assert len(calls) == 1 and calls[0]["addressed"] is True


def test_recent_speech_cools_the_bot_down(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    b.PUBLIC_STATE["last"]["group:7"] = time.time()   # 刚说完一句
    asyncio.run(b.on_group_message(_msg("这个话题挺有意思的"), SELF))
    assert calls == []


def test_cooldown_does_not_block_being_addressed(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    b.PUBLIC_STATE["last"]["group:7"] = time.time()
    asyncio.run(b.on_group_message(_msg("你说说看", at=[SELF]), SELF))
    assert len(calls) == 1


# ---------------- _group_text 的解析 ----------------

def test_group_text_flags(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    text, at_me, at_other = b._group_text(_msg("你好", at=[SELF]), SELF)
    assert (text, at_me, at_other) == ("你好", True, False)

    text, at_me, at_other = b._group_text(_msg("你好", at=[555]), SELF)
    assert at_me is False and at_other is True
    assert "555" in text          # 别人被 @ 的事实在文字里留着，模型看得懂

    _, at_me, at_other = b._group_text(_msg("全体注意", at=["all"]), SELF)
    assert at_me is False and at_other is False


def test_group_text_raw_fallback(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    data = {"raw_message": f"[CQ:at,qq={SELF}] 在吗"}
    assert b._group_text(data, SELF) == ("在吗", True, False)
    data = {"raw_message": "[CQ:at,qq=555] 在吗"}
    assert b._group_text(data, SELF) == ("在吗", False, True)


# ---------------- 群语音：被 @ 时才转 ----------------

def _voice_msg(at=None):
    segs = [{"type": "at", "data": {"qq": str(qq)}} for qq in (at or [])]
    segs.append({"type": "record", "data": {"file": "abc.amr"}})
    return {"group_id": 7, "user_id": 42, "sender": {"nickname": "甲"}, "message": segs}


def test_group_voice_is_transcribed_when_addressed(tmp_path, monkeypatch):
    """被 @ 的语音要真的变成文字交下去 —— 以前这里只有「在呢」。"""
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)

    async def fake_audio(seg):
        return b"fake-audio"

    monkeypatch.setattr(b, "_voice_audio", fake_audio)
    monkeypatch.setattr(b, "VOICE_ENABLED", True)
    monkeypatch.setattr(b.VOICE, "transcribe", lambda audio: "今天下午三点开会")
    asyncio.run(b.on_group_message(_voice_msg(at=[SELF]), SELF))
    assert len(calls) == 1
    assert "今天下午三点开会" in calls[0]["text"]
    assert calls[0]["addressed"] is True


def test_group_voice_transcribed_without_at(tmp_path, monkeypatch):
    """没被 @ 的语音也要转：群聊靠攒批自己决定接不接，不能连内容都看不见。"""
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)

    async def fake_audio(seg):
        return b"fake-audio"

    monkeypatch.setattr(b, "_voice_audio", fake_audio)
    monkeypatch.setattr(b, "VOICE_ENABLED", True)
    monkeypatch.setattr(b, "GROUP_VOICE_ALL", True)   # 不依赖配置文件的默认值
    monkeypatch.setattr(b.VOICE, "transcribe", lambda audio: "你们在聊什么呢")
    asyncio.run(b.on_group_message(_voice_msg(), SELF))      # 不带 @
    assert len(calls) == 1
    assert "你们在聊什么呢" in calls[0]["text"]
    assert calls[0]["addressed"] is False


def test_group_voice_all_off_falls_back_to_addressed_only(tmp_path, monkeypatch):
    """voice.group_all=false 时退回旧行为：只转被 @ 的，省 CPU。"""
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    called = []

    async def fake_lines(data):
        called.append(1)
        return ["不该被转"]

    monkeypatch.setattr(b, "_group_voice_lines", fake_lines)
    monkeypatch.setattr(b, "GROUP_VOICE_ALL", False)
    asyncio.run(b.on_group_message(_voice_msg(), SELF))      # 没被 @
    assert called == []
    assert calls == []


def test_voice_at_someone_else_is_not_transcribed(tmp_path, monkeypatch):
    """@ 别人的语音不转：后面本来也不接，转了是白耗 CPU。"""
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)
    called = []

    async def fake_lines(data):
        called.append(1)
        return ["不该被转"]

    monkeypatch.setattr(b, "_group_voice_lines", fake_lines)
    monkeypatch.setattr(b, "GROUP_SKIP_AT_OTHER", True)
    asyncio.run(b.on_group_message(_voice_msg(at=[555]), SELF))
    assert called == []
    assert calls == []


def test_group_voice_failure_gives_placeholder(tmp_path, monkeypatch):
    """拿不到音频 / 转不出来时给占位，别让模型以为对方发了条空消息。"""
    b = _load(tmp_path, monkeypatch)
    calls = _spy(b, monkeypatch)

    async def no_audio(seg):
        return None

    monkeypatch.setattr(b, "_voice_audio", no_audio)
    monkeypatch.setattr(b, "VOICE_ENABLED", True)
    asyncio.run(b.on_group_message(_voice_msg(at=[SELF]), SELF))
    assert len(calls) == 1
    assert "没能转成文字" in calls[0]["text"]


def test_has_record_recognises_both_shapes(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch)
    assert b._has_record(_voice_msg(at=[SELF])) is True
    assert b._has_record({"raw_message": "[CQ:record,file=x.amr]"}) is True
    assert b._has_record(_msg("纯文字")) is False
