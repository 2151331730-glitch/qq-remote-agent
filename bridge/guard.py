# -*- coding: utf-8 -*-
"""安全护栏 + 审计日志。

- danger_reason_task / danger_reason_shell：判断一条任务或命令是否可能造成破坏。
  命中不等于禁止，只是要求主人在 QQ 里再回一句 /确认。
- audit：所有任务、命令、确认动作都追加到 JSONL，供看板读取和事后回放。
"""
import json
import re
from datetime import datetime
from pathlib import Path

# 任务文本里的危险意图（中文关键词为主，宁可多问一句）
DANGER_TASK_WORDS = (
    "删除", "删掉", "删了", "全删", "删光", "清空", "清掉", "格式化", "抹掉", "卸载", "覆盖",
    "关机", "重启电脑", "重置系统", "恢复出厂",
    "rm -rf", "rm -r", "del /", "rmdir", "format ", "diskpart",
    "shutdown", "restart-computer", "stop-computer", "remove-item",
    "reg delete", "bcdedit", "cipher /w", "net user", "takeown",
)

# PowerShell 命令里的危险模式（匹配到就是这个命令本身要动手）
DANGER_SHELL_PATTERNS = re.compile(
    r"(?ix)"
    r"\brm\s+-[rf]{1,2}\b"
    r"|\bdel\s+/[sqf]"
    r"|\b(rd|rmdir)\s+/s"
    r"|\bformat\s+[a-z]:"
    r"|\bdiskpart\b"
    r"|\b(shutdown|restart-computer|stop-computer)\b"
    r"|\bremove-item\b[^\n|;]*-recurse"
    r"|\breg\s+delete\b"
    r"|\bbcdedit\b"
    r"|\bcipher\s+/w\b"
    r"|\bnet\s+user\s+\S+\s+/(add|delete)\b"
    r"|\btakeown\b"
    r"|\bicacls\b[^\n;]*/(grant|deny|reset)\b"
    r"|\bschtasks\b[^\n;]*/delete\b"
    r"|\btaskkill\b[^\n;]*/f\b"
)


def danger_reason_task(text: str):
    low = (text or "").lower()
    for w in DANGER_TASK_WORDS:
        if w in low:
            return w
    return None


def danger_reason_shell(cmd: str):
    m = DANGER_SHELL_PATTERNS.search(cmd or "")
    return m.group(0) if m else None


def audit(path: Path, **fields):
    """追加一条审计记录；失败不影响主流程。超过 5MB 自动轮转一份。"""
    try:
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix(path.suffix + ".1"))
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds")}
        rec.update(fields)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def tail_events(path: Path, limit: int = 80):
    """给看板用：读审计文件最后 limit 条。"""
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []
