# -*- coding: utf-8 -*-
"""把一条长回复拆成几条短消息，像真人那样分条发。

真人聊天很少一次甩 200 字；常见是两三条短句。这里只在回复够长时拆，
并且限制条数（默认最多 3 条），避免把一句话拆碎成刷屏。
"""
import re

# 句末标点（中英文）与换行都算断点，标点留在前一段
_BREAK = re.compile(r"(?<=[。！？!?…~；;])|(?<=\n)")


def split_bubbles(text: str, min_chars: int = 80, limit: int = 140,
                  max_parts: int = 3):
    """返回 1~max_parts 段文本。短回复原样返回。"""
    raw = (text or "").strip()
    if not raw:
        return []
    # 换行压成空格：QQ 里一条消息内的换行不如分条自然
    flat = " ".join(raw.split())
    if len(flat) <= max(1, min_chars) or max_parts <= 1:
        return [flat]

    chunks = [c for c in (p.strip() for p in _BREAK.split(flat)) if c]
    if len(chunks) <= 1:
        chunks = [flat[i:i + limit] for i in range(0, len(flat), limit)]

    parts = []
    buf = ""
    for c in chunks:
        if len(buf) + len(c) <= limit or not buf:
            buf += c
        else:
            parts.append(buf)
            buf = c
    if buf:
        parts.append(buf)

    # 不超过 max_parts：多出来的并进最后一段
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + ["".join(parts[max_parts - 1:])]
    return [p.strip() for p in parts if p.strip()]
