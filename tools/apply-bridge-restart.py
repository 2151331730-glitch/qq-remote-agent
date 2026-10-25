# -*- coding: utf-8 -*-
"""等本任务结果发完后，自动重启 bridge 让新代码生效。

只认「任务开始：那就做」这一条任务的结束行；之后若又出现新任务，就再等。
"""
import datetime as dt
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\win11\qq-remote")
LOG = ROOT / "logs" / "bridge.log"
OUT = ROOT / "tools" / "restart-watcher.log"
RESTART = ROOT / "restart-bridge.ps1"
# 用法：python tools\apply-bridge-restart.py <任务号或文字片段>
# 不传就退回旧默认，兼容以前的调用方式。
TASK_MARK = sys.argv[1] if len(sys.argv) > 1 else "那就做"
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO ")

DEADLINE = time.time() + 15 * 60


def note(msg):
    try:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def read_lines():
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except Exception:
        return []


def task_finished():
    lines = read_lines()
    start = None
    for i, line in enumerate(lines):
        if TASK_MARK in line and "任务开始" in line:
            # 只看这次跑的那条；任务号/文字片段由调用方给出
            start = i
    if start is None:
        return False
    end = None
    for j in range(start + 1, len(lines)):
        if "任务结束" in lines[j]:
            end = j
            break
    if end is None:
        return False
    # 结束之后又开始了新任务 → 先不动，等下一条结束
    if any("任务开始" in line for line in lines[end + 1:]):
        return False
    return True


def main():
    note("watcher 启动，等待本任务结束")
    while time.time() < DEADLINE:
        time.sleep(5)
        if not task_finished():
            continue
        time.sleep(10)  # 给 send 收尾、state 落盘留时间
        if not task_finished():
            continue
        note("检测到本任务已结束，执行 restart-bridge.ps1 -Force")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(RESTART), "-Force"],
                       timeout=180, capture_output=True)
        time.sleep(3)
        try:
            with urllib.request.urlopen("http://127.0.0.1:3099/api/state", timeout=8) as r:
                note(f"看板返回 {r.status}：{r.read(120).decode('utf-8', 'replace')}")
        except Exception as e:
            note(f"看板检查失败：{e}")
        return
    note("15 分钟内没等到任务结束，放弃重启")


if __name__ == "__main__":
    main()
