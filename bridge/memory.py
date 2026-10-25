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
    """读记忆正文（不含注释行）；文件不存在或读失败时返回空串。

    **去掉所有 `#` 开头的行为**。原来只跳过第一行，于是模板里的说明注释
    （"这个文件是私人内容…"）会被当成记忆注入提示词 —— 它"记得"的是注释。
    """
    p = Path(path)
    if not p.exists():
        return ""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    lines = [ln for ln in raw.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    body = "\n".join(lines).strip()
    if len(body) > max_chars:
        body = body[-max_chars:]
    return body


def entries(path) -> list[str]:
    """当前所有记忆条目（已去掉注释与空行）。给 /记忆 和去重用。"""
    body = load(path, max_chars=10**9)
    return [ln for ln in body.splitlines() if ln.strip()]


def remove(path, needle: str) -> int:
    """删掉包含 `needle` 的条目，返回删除条数。空串不匹配任何东西。"""
    needle = (needle or "").strip()
    if not needle:
        return 0
    keep = [ln for ln in entries(path) if needle not in ln]
    removed = len(entries(path)) - len(keep)
    if removed:
        out = "\n".join([HEADER] + keep) + "\n"
        Path(path).write_text(out, encoding="utf-8")
    return removed


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
