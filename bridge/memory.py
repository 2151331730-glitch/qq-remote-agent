# -*- coding: utf-8 -*-
"""长期记忆：一个纯文本小文件，闲聊时整段注入提示词。

为什么不引数据库/向量库：个人偏好、日程、称呼这类信息就几十条，全文塞进提示词最简单，
模型每次都能看到，不存在"检索不到"；文件本身人也能直接看、直接改。
代价是每条消息都要带上它，所以有长度上限（MAX_CHARS），超了从最旧的一条开始丢。

格式：一行一条，`- YYYY-MM-DD 内容`；第一行是标题，读取时会去掉省 token。
"""
import time
from pathlib import Path

MAX_CHARS = 4000
HEADER = "# 长期记忆（一行一条，越靠后越新；由 /记住 或手工编辑）"


def load(path, max_chars: int = MAX_CHARS) -> str:
    """读记忆正文（不含标题行）；文件不存在或读失败时返回空串。"""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    if len(body) > max_chars:
        body = body[-max_chars:]
    return body


def append(path, text: str, max_chars: int = MAX_CHARS) -> str:
    """追加一条记忆，返回写入后的全文。

    内容会压成一行（换行变空格），避免一条记忆被拆成多行后无法按行裁剪。
    """
    flat = " ".join((text or "").split())
    if not flat:
        raise ValueError("记忆内容不能为空")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = []
    if p.exists():
        body = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    stamp = time.strftime("%Y-%m-%d")
    entry = f"- {stamp} {flat}"
    # 完全一样的最近一条不重复记
    if not body or body[-1] != entry:
        body.append(entry)
    # 超上限：从最旧的开始丢，至少保留一条
    while len(body) > 1 and sum(len(x) + 1 for x in body) > max_chars:
        body.pop(0)
    out = "\n".join([HEADER] + body) + "\n"
    p.write_text(out, encoding="utf-8")
    return out
