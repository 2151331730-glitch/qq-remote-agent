# -*- coding: utf-8 -*-
"""把最近聊天记录压成一段"上下文摘录"。

为什么需要：群里 @ 机器人时，模型只看得到那一条，接不上前面的话题；
陌生私聊重启后也没有历史。这里把最近 N 条压成紧凑文本，随提示词一起给模型。

"大概"体现在三处限制上（都是为了防止上下文把 token 吃光）：
- 只取最近 `limit` 条
- 每条截到 `per_message` 字
- 总长超过 `max_chars` 时从最旧的开始丢，并从换行处切断，避免留下半句话
"""
import re

_CQ = re.compile(r"\[CQ:[^\]]+\]")


def _segments_text(message) -> str:
    """兼容 array 格式（列表）和 CQ 码字符串。"""
    if isinstance(message, list):
        out = []
        for seg in message:
            kind = seg.get("type")
            data = seg.get("data") or {}
            if kind == "text":
                out.append(data.get("text") or "")
            elif kind == "at":
                qq = str(data.get("qq") or "")
                out.append("@全体 " if qq == "all" else f"@{qq} ")
            elif kind == "image":
                out.append("[图片]")
            elif kind == "face":
                out.append("[表情]")
            elif kind == "reply":
                continue
            elif kind in ("record", "video", "file"):
                out.append("[文件]")
        return "".join(out)
    return _CQ.sub("", str(message or ""))


def format_transcript(messages, self_id=0, limit=12, per_message=80,
                      max_chars=1200, bot_label="我") -> str:
    """返回形如 "昵称: 内容" 的多行摘录；没有可用内容时返回空串。"""
    lines = []
    for msg in list(messages or [])[-max(1, limit):]:
        sender = msg.get("sender") or {}
        uid = str(sender.get("user_id") or msg.get("user_id") or "")
        if self_id and uid == str(self_id):
            name = bot_label
        else:
            name = sender.get("card") or sender.get("nickname") or uid or "某人"
        text = _segments_text(msg.get("message"))
        text = " ".join(text.split())          # 换行压成空格，一行一条
        if not text:
            continue
        lines.append(f"{name}: {text[:per_message]}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:]
        nl = out.find("\n")
        if nl > 0:
            out = out[nl + 1:]                 # 从完整的一行开始
    return out.strip()
