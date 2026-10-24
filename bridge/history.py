# -*- coding: utf-8 -*-
"""把聊天记录落到本地文件（JSONL），作为上下文来源。

关键区别：
- 写入文件 = 0 token。磁盘不要钱，全存没关系。
- 花 token 的是"把多少内容放进提示词"。所以文件可以留 500 条，
  但每次只把最近 `limit` 条、每条截断后拼成摘录喂给模型。

为什么不再每次都问 NapCat 要历史：
- 历史接口偶尔会失败/超时，失败了聊天就没上下文；
- 本地文件重启后还在，且比 HTTP 往返快；
- 群里没 @ 机器人的闲聊也会存下来，作为真正的"前情"。

文件布局：`<root>/<scope>.jsonl`，scope 形如 `group_<GROUP_ID>` / `user_54321` / `owner`。
每行一个 JSON：{"ts": 秒, "uid": QQ号, "name": 显示名, "text": 内容}
"""
import json
import re
import time
from pathlib import Path

MAX_LINES = 500
_ROTATE_AT_BYTES = 96 * 1024      # 文件超过约 96KB 就压缩到最近 MAX_LINES 条


def scope_file(root, scope: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", str(scope)).strip("_") or "misc"
    return Path(root) / f"{safe}.jsonl"


def append(root, scope: str, uid, name: str, text: str, ts: float | None = None,
           max_lines: int = MAX_LINES):
    """追加一条记录；内容压成一行。文件过大时自动只留最近 max_lines 条。"""
    text = " ".join((text or "").split())
    if not text:
        return
    p = scope_file(root, scope)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": round(ts if ts is not None else time.time(), 3),
             "uid": uid, "name": (name or "")[:40], "text": text[:1000]}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        if p.stat().st_size > _ROTATE_AT_BYTES:
            _trim(p, max_lines)
    except OSError:
        pass


def _read(p: Path):
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _trim(p: Path, max_lines: int):
    lines = [ln for ln in _read(p) if ln.strip()][-max(1, max_lines):]
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(p)


def tail(root, scope: str, limit: int = 12, per_message: int = 80,
         max_chars: int = 1200) -> str:
    """最近 limit 条，格式化成 `名字: 内容`；没有记录返回空串。"""
    lines = [ln for ln in _read(scope_file(root, scope)) if ln.strip()]
    out = []
    for ln in lines[-max(1, limit):]:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        text = " ".join(str(e.get("text") or "").split())
        if not text:
            continue
        out.append(f"{e.get('name') or e.get('uid') or '某人'}: {text[:per_message]}")
    joined = "\n".join(out)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
        nl = joined.find("\n")
        if nl > 0:
            joined = joined[nl + 1:]
    return joined.strip()


def count(root, scope: str) -> int:
    return len([ln for ln in _read(scope_file(root, scope)) if ln.strip()])
