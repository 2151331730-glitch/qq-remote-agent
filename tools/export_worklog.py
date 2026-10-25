# -*- coding: utf-8 -*-
r"""把 bridge 的任务记录导出成可读的月度归档，存到 桌面\记忆\work\。

用法：
    python tools\export_worklog.py            # 导出全部月份
    python tools\export_worklog.py --since 2026-10

为什么要导出：tasks.db 是 SQLite，跑久了可能损坏或被误删；导出的 markdown
是纯文本，随便备份、随便看，和 Claude Code 的会话存档一个思路。
"""
import argparse
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB = Path(r"C:\Users\win11\qq-remote\bridge\tasks.db")
OUT = Path(r"C:\Users\win11\Desktop\记忆\work")


def fmt_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def clip(s, n):
    s = (s or "").replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="只导出该月及以后，如 2026-10")
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"找不到 {DB}")

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select id, kind, state, text, result, error, created_at, duration_ms,"
        " tokens_in, tokens_out, cost_usd from tasks order by created_at"
    ).fetchall()

    months = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(r["created_at"] or 0)
        month = d.strftime("%Y-%m")
        if args.since and month < args.since:
            continue
        months[month].append(r)

    OUT.mkdir(parents=True, exist_ok=True)
    for month, items in sorted(months.items()):
        lines = [f"# QQ 远程任务归档 · {month}", "",
                 f"> 自动导出，共 {len(items)} 条。原始数据在 `bridge\\tasks.db`。", ""]
        for r in items:
            state = {"succeeded": "完成", "failed": "失败",
                     "cancelled": "取消", "interrupted": "中断"}.get(r["state"], r["state"])
            kind = "闲聊" if r["kind"] == "chat" else "任务"
            lines.append(f"## {fmt_ts(r['created_at'])} · {kind} · {state}")
            lines.append(f"- 说的：{clip(r['text'], 200)}")
            if r["result"]:
                lines.append(f"- 回的：{clip(r['result'], 300)}")
            if r["error"]:
                lines.append(f"- 错误：{clip(r['error'], 200)}")
            secs = (r["duration_ms"] or 0) / 1000
            lines.append(f"- 耗时 {secs:.0f}s · token {r['tokens_in'] or 0}→{r['tokens_out'] or 0}"
                         f" · 成本 {r['cost_usd'] or 0:.3f}")
            lines.append("")
        path = OUT / f"{month}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"已写 {path}（{len(items)} 条）")


if __name__ == "__main__":
    main()
