# -*- coding: utf-8 -*-
"""QQ 远程任务桥（v2：持久化队列 + 并发 + 可观测）

链路：
    手机 QQ → NapCat(OneBot11 WS) → 本模块 → claude.exe → 结果回 QQ
    网页看板 (:3099) → 同一套队列与白名单

v2 相比 v1 的三处结构性变化：
1. 任务队列落在 SQLite（`store.py`），靠 `dedup_key` 做幂等：
   QQ 重投同一条消息只会入队一次；进程崩溃后 queued 任务还在，running 的标成 interrupted。
2. worker pool（`workers` 配置，默认 1）+ 按工作目录加锁：
   两个任务改同一个目录才排队，不同目录可以并行。
3. 指标（`metrics.py`）+ 事件表 + 审计日志：
   /metrics 输出 Prometheus 文本，看板据此显示 p50/p95、成功率、成本。

安全模型不变：以 `--dangerously-skip-permissions` 运行，唯一防线是 owner 白名单 + 危险词确认。
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from websockets.asyncio.client import connect

from guard import audit, danger_reason_shell, danger_reason_task, tail_events
from metrics import METRICS
from panel import Panel
from store import TaskStore

BASE = Path(__file__).resolve().parent
# 测试时用环境变量指向临时文件，避免抢占真机正在跑的那一套
CONFIG_PATH = Path(os.environ.get("QQ_REMOTE_CONFIG") or (BASE / "config.json"))
STATE_PATH = Path(os.environ.get("QQ_REMOTE_STATE") or (BASE / "state.json"))

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = json.load(f)

LOG_DIR = Path(CFG["log_dir"])
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("QQ_REMOTE_DB") or CFG.get("db_path") or (BASE / "tasks.db"))

logger = logging.getLogger("bridge")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = TimedRotatingFileHandler(LOG_DIR / "bridge.log", when="midnight", backupCount=14, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

CLAUDE = CFG["claude_exe"]
WORKDIR = CFG["workdir"]
CHUNK = int(CFG.get("chunk_size", 1200))
TASK_TIMEOUT = int(CFG.get("task_timeout_minutes", 45)) * 60
HEARTBEAT_SEC = int(CFG.get("heartbeat_seconds", 60))
MAX_QUEUE = int(CFG.get("max_queue", 5))
WORKERS = max(1, int(CFG.get("workers", 1)))
PANEL_PORT = int(CFG.get("panel_port", 3099))
PANEL_TOKEN = str(CFG.get("panel_token", "") or "")
AUDIT_PATH = LOG_DIR / "audit.jsonl"
CONFIRM_TTL = 300

START_TS = time.time()
STATE = {"owner_uin": int(CFG.get("owner_uin", 0) or 0), "session_id": "", "cwd": WORKDIR,
         "current_task": ""}
COST_TOTAL = 0.0
LAST_RESULT = ""
STORE = TaskStore(DB_PATH)
WAKE = asyncio.Event()
# task_id -> {"task": asyncio.Task, "proc": Process, "text": str, "started": float, "cwd": str}
RUNNING: dict[str, dict] = {}
LOCKS: dict[str, asyncio.Lock] = {}
PENDING_CONFIRM = {"entry": None}
PENDING_NOTICES: list[str] = []


def load_state():
    global COST_TOTAL
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            STATE.update({k: v for k, v in data.items() if k in STATE})
            if data.get("cost_total"):
                COST_TOTAL = float(data["cost_total"])
        except Exception as e:
            logger.warning("state load failed: %s", e)


def save_state():
    try:
        data = dict(STATE)
        data["cost_total"] = COST_TOTAL
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.warning("state save failed: %s", e)


def log(text):
    logger.info(text)


def sync_gauges():
    METRICS.set_gauge("qq_remote_queue_depth", STORE.queue_depth())
    METRICS.set_gauge("qq_remote_running", len(RUNNING))


class Client:
    """最小的 OneBot 11 WS 客户端：发 action、收 event。"""

    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.events = asyncio.Queue()

    async def reader(self):
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                echo = data.get("echo")
                if echo is not None and echo in self.pending:
                    fut = self.pending.pop(echo)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    await self.events.put(data)
        finally:
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("websocket closed"))
            self.pending.clear()

    async def call(self, action, params, timeout=15):
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(echo, None)


CLIENT: Client | None = None


async def send(text: str):
    """把长文本切成多条发出去。"""
    if not STATE["owner_uin"] or CLIENT is None:
        logger.warning("send skipped (no owner/client): %s", text[:80])
        return
    for part in split_text(text):
        try:
            await CLIENT.call("send_private_msg", {"user_id": STATE["owner_uin"], "message": part})
        except Exception as e:
            logger.error("send failed: %r", e)
            return


def split_text(text: str, size=CHUNK):
    text = (text or "").strip()
    if not text:
        return ["（无输出）"]
    if len(text) <= size:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) > size:
            parts.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    parts[0] = f"（共 {len(parts)} 段）\n" + parts[0]
    return parts


# ---------------- claude 执行 ----------------

def _kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    except Exception as e:
        logger.warning("kill tree failed: %s", e)


def _session_invalid(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in (
        "no conversation found", "session not found",
        "could not find session", "invalid session"))


async def run_claude(prompt: str, task_id: str, cwd: str) -> tuple[bool, str, float, int]:
    """跑一次 claude -p。返回 (成功?, 给用户看的文本, 成本, 退出码)。"""
    global COST_TOTAL, LAST_RESULT
    # 多 worker 并发时不许共用同一个 --resume 会话，否则上下文会互相污染
    use_session = WORKERS == 1

    async def _once(use_resume: bool):
        args = [CLAUDE, "-p", prompt, "--output-format", "json",
                "--dangerously-skip-permissions"]
        if use_resume and use_session and STATE.get("session_id"):
            args += ["--resume", STATE["session_id"]]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
        except OSError as e:
            # WinError 216 = claude.exe 正在被自更新替换，等两秒再试一次
            log(f"启动 claude 失败（{e}），2 秒后重试")
            await asyncio.sleep(2)
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
        slot = RUNNING.setdefault(task_id, {})
        slot["proc"] = proc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=TASK_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_tree(proc.pid)
            return None, f"⏰ 超过 {TASK_TIMEOUT // 60} 分钟，已强制结束。", -1
        except asyncio.CancelledError:
            _kill_tree(proc.pid)
            raise
        finally:
            slot["proc"] = None
        return ((out or b"").decode("utf-8", "replace"),
                (err or b"").decode("utf-8", "replace"), proc.returncode or 0)

    def _parse(raw):
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        return None

    resumed = bool(STATE.get("session_id")) and use_session
    out, err, code = await _once(resumed)
    if out is None:
        return False, err, 0.0, code

    data = _parse(out)
    if data is None and resumed and _session_invalid(err or out):
        log("resume 失败（旧会话不存在），改用新会话重试")
        out, err, code = await _once(False)
        if out is None:
            return False, err, 0.0, code
        data = _parse(out)
    if data is None:
        return False, f"❌ claude 输出无法解析：\n{(err or out or '')[-800:]}", 0.0, code

    sid = data.get("session_id")
    if sid and use_session:
        STATE["session_id"] = sid
        save_state()

    cost = float(data.get("total_cost_usd") or 0)
    COST_TOTAL += cost
    text = data.get("result") or ""
    is_error = bool(data.get("is_error"))
    LAST_RESULT = text
    try:
        (LOG_DIR / "last-result.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass
    if text and len(text) > 3000:
        text = f"{text[:3000]}\n\n……（结果较长，共 {len(text)} 字，发 /全文 看剩下部分）"
    return (not is_error), (text or "（任务完成，没有文字输出）"), cost, code


async def _cwd_lock(cwd: str) -> asyncio.Lock:
    lock = LOCKS.get(cwd)
    if lock is None:
        lock = LOCKS[cwd] = asyncio.Lock()
    return lock


async def worker(wid: int):
    """worker pool 里的一个执行单元。"""
    while True:
        row = STORE.claim_next()
        if row is None:
            WAKE.clear()
            try:
                await asyncio.wait_for(WAKE.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            sync_gauges()
            continue

        tid = row["id"]
        cwd = row.get("cwd") or STATE.get("cwd") or WORKDIR
        lock = await _cwd_lock(cwd)
        if lock.locked():
            # 同一目录已有任务在跑：放回队列，等别的 cwd 先跑
            STORE.requeue(tid)
            await asyncio.sleep(1)
            continue

        async with lock:
            await _execute(row, cwd)


async def _execute(row, cwd):
    tid = row["id"]
    text = row["text"]
    started = time.time()
    prompt = CFG.get("prompt_template", "【来自 QQ 的远程任务】{task}").format(task=text)
    STATE["current_task"] = text
    save_state()
    sync_gauges()
    audit(AUDIT_PATH, event="task_start", id=tid, source=row["source"], text=text[:500])
    log(f"任务开始 [{tid}]：{text[:200]}")

    run_task = asyncio.create_task(run_claude(prompt, tid, cwd))
    RUNNING[tid] = {"task": run_task, "text": text, "started": started, "cwd": cwd}
    hb = asyncio.create_task(heartbeat(tid, started))
    cost = 0.0
    try:
        ok, reply, cost, code = await run_task
    except asyncio.CancelledError:
        if not run_task.cancelled():
            raise
        log(f"任务被取消 [{tid}]")
        STORE.finish(tid, "cancelled", error="stopped by user", duration_ms=int((time.time() - started) * 1000))
        METRICS.inc("qq_remote_tasks_total", {"state": "cancelled"})
        await send("🛑 已停止当前任务。")
        _cleanup(tid)
        return
    except Exception as e:
        logger.exception("task exception")
        ok, reply, cost, code = False, f"❌ 执行异常：{e}", 0.0, -1
    finally:
        hb.cancel()

    seconds = time.time() - started
    state = "succeeded" if ok else "failed"
    STORE.finish(tid, state, result=reply, error=None if ok else reply,
                 exit_code=code, cost_usd=cost, duration_ms=int(seconds * 1000))
    METRICS.inc("qq_remote_tasks_total", {"state": state})
    METRICS.observe("qq_remote_task_duration_seconds", seconds)
    METRICS.inc("qq_remote_cost_usd_total", {}, cost)
    audit(AUDIT_PATH, event="task_end", id=tid, source=row["source"], ok=ok,
          seconds=round(seconds, 1), cost_usd=round(cost, 6), result=(reply or "")[:2000])
    log(f"任务结束 [{tid}]：success={ok} 用时 {seconds:.0f}s")
    await send(("✅ " if ok else "⚠️ ") + reply)
    _cleanup(tid)


def _cleanup(tid):
    RUNNING.pop(tid, None)
    STATE["current_task"] = ""
    save_state()
    sync_gauges()


async def heartbeat(tid, started):
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        mins = (time.time() - started) / 60
        await send(f"⏳ 还在执行（已 {mins:.0f} 分钟）…")


# ---------------- 命令 ----------------

HELP = """命令：
/状态 看运行状态（含 p95 时延、成功率）
/任务 [n] 最近任务列表（带 ID）
/停止 中断当前任务
/取消 <ID> 取消排队中的任务
/清空 清空排队中的任务
/确认 执行待确认的危险操作
/取消 放弃待确认的操作（/取消 <ID> = 取消排队中的某条）
/新会话 清空对话上下文
/日志 [n] 看最近日志
/全文 看上次完整结果
/cd <路径> 换工作目录
/心跳 测试是否在线
!<命令> 直接执行 PowerShell（例：!dir C:\\）
其他任何文字 = 交给 claude 干活
（危险任务会先要求 /确认；本机看板 http://127.0.0.1:3099/）"""


def _fmt_ms(ms):
    if not ms:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60000:.1f}min"


async def handle_command(text: str) -> bool:
    t = text.strip()
    low = t.lower()
    if low in ("/帮助", "/help", "/h"):
        await send(HELP)
        return True

    if low in ("/状态", "/status", "/s"):
        st = STORE.stats()
        up = (time.time() - START_TS) / 3600
        cur = "空闲" if not RUNNING else "；".join(
            f"{k[:6]}（{(time.time() - v['started']) / 60:.0f} 分钟）" for k, v in RUNNING.items())
        runs = st["window_runs"] or 1
        rate = (st["done"] / runs * 100) if runs else 0
        await send(
            f"在线 {up:.1f} 小时\n"
            f"当前：{cur}\n"
            f"排队：{st['queue']}／上限 {MAX_QUEUE}\n"
            f"完成 {st['done']} · 失败 {st['failed']}\n"
            f"24h 成功率 {rate:.0f}%（{st['window_runs']} 次）\n"
            f"时延 p50 {_fmt_ms(st['p50_ms'])} · p95 {_fmt_ms(st['p95_ms'])}\n"
            f"24h 花费 ${st['window_cost']:.3f} · 累计 ${st['total_cost']:.3f}\n"
            f"工作目录：{STATE.get('cwd')}\n"
            f"会话：{(STATE.get('session_id') or '无')[:8]}  worker：{WORKERS}"
        )
        return True

    if low.startswith("/任务") or low.startswith("/tasks"):
        n = 10
        parts = t.split()
        if len(parts) > 1 and parts[1].isdigit():
            n = min(int(parts[1]), 30)
        rows = STORE.recent(n)
        if not rows:
            await send("还没有任务记录。")
            return True
        icon = {"succeeded": "✅", "failed": "❌", "cancelled": "🛑",
                "interrupted": "⚠️", "running": "▶️", "queued": "⏳"}
        lines = []
        for r in rows:
            lines.append(f"{icon.get(r['state'], '·')} {r['id']} "
                         f"{_fmt_ms(r['duration_ms'])} {r['source']} {r['text'][:40]}")
        await send("\n".join(lines))
        return True

    if low in ("/确认", "/yes", "/ok"):
        entry = PENDING_CONFIRM.get("entry")
        if not entry:
            await send("没有待确认的操作。")
        elif time.time() - entry["ts"] > CONFIRM_TTL:
            PENDING_CONFIRM["entry"] = None
            await send("⏰ 确认已过期（超过 5 分钟），请重新发送任务。")
        else:
            PENDING_CONFIRM["entry"] = None
            audit(AUDIT_PATH, event="confirm_approved", kind=entry["kind"],
                  source=entry["source"], text=entry["text"][:500])
            await _run_confirmed(entry)
        return True

    if low in ("/取消确认", "/取消", "/cancel", "/no"):
        entry = PENDING_CONFIRM.get("entry")
        if entry:
            PENDING_CONFIRM["entry"] = None
            audit(AUDIT_PATH, event="confirm_cancelled", kind=entry["kind"],
                  source=entry["source"], text=entry["text"][:500])
            await send("🗑 已取消待确认的操作。")
        else:
            await send("没有待确认的操作。")
        return True

    if low in ("/停止", "/stop"):
        if RUNNING:
            for tid, slot in list(RUNNING.items()):
                task = slot.get("task")
                if task and not task.done():
                    task.cancel()
            await send("🛑 已发送停止信号。")
        else:
            await send("当前没有在跑的任务。")
        return True

    if low.startswith("/取消 ") or low.startswith("/cancel "):
        target = t.split(maxsplit=1)[1].strip()
        if STORE.cancel(target):
            await send(f"🗑 已取消排队任务 {target}。")
            sync_gauges()
        else:
            await send(f"没找到排队中的任务 {target}（可能已经在跑或不存在）。")
        return True

    if low in ("/清空", "/clear"):
        n = STORE.cancel_all_queued()
        sync_gauges()
        await send(f"🗑 已清空队列 {n} 个任务。")
        return True

    if low in ("/新会话", "/new"):
        STATE["session_id"] = ""
        save_state()
        await send("🧹 已开新会话。")
        return True

    if low in ("/心跳", "/ping"):
        await send("🏓 我在。")
        return True

    if low.startswith("/日志") or low.startswith("/log"):
        n = 30
        parts = t.split()
        if len(parts) > 1 and parts[1].isdigit():
            n = min(int(parts[1]), 200)
        p = LOG_DIR / "bridge.log"
        if not p.exists():
            await send("还没有日志。")
        else:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            await send("\n".join(lines) or "（空）")
        return True

    if low in ("/全文", "/full"):
        if not LAST_RESULT:
            await send("还没有完整结果。")
        elif len(LAST_RESULT) > 3000:
            await send(LAST_RESULT[3000:])
        else:
            await send("（上次结果不到 3000 字，已经完整发过了）")
        return True

    if low.startswith("/cd "):
        target = t[4:].strip().strip('"')
        p = Path(target)
        if p.is_dir():
            STATE["cwd"] = str(p)
            save_state()
            await send(f"📁 工作目录改为：{p}")
        else:
            await send(f"目录不存在：{target}")
        return True
    return False


async def _notify(text, timeout=5):
    try:
        await asyncio.wait_for(send(text), timeout)
    except Exception as e:
        logger.warning("notify failed: %r", e)


async def _ask_confirm(kind, text, source, reason):
    entry = PENDING_CONFIRM.get("entry")
    if entry and time.time() - entry["ts"] < CONFIRM_TTL:
        await _notify(f"⚠️ 还有一条待确认的操作：{entry['text'][:80]}\n先 /确认 或 /取消确认。")
        return {"status": "confirm_busy", "reason": entry["reason"]}
    PENDING_CONFIRM["entry"] = {"kind": kind, "text": text, "source": source,
                                "reason": reason, "ts": time.time()}
    audit(AUDIT_PATH, event="confirm_requested", kind=kind, source=source,
          reason=reason, text=text[:500])
    await _notify(f"⚠️ 这条操作可能造成破坏（命中：{reason}）：\n{text[:300]}\n"
                  f"回复 /确认 执行，/取消确认 放弃（5 分钟内有效）。")
    return {"status": "confirm", "reason": reason}


async def submit_task(text, source="qq", confirmed=False, dedup_key=None, sender=None):
    """入队一个 agent 任务；危险任务先要求确认。"""
    text = (text or "").strip()
    if not text:
        return {"status": "rejected", "error": "empty"}
    if STORE.queue_depth() >= MAX_QUEUE:
        return {"status": "full", "limit": MAX_QUEUE}
    reason = danger_reason_task(text)
    if reason and not confirmed:
        return await _ask_confirm("task", text, source, reason)
    tid, dup = STORE.enqueue(text, source=source, sender=sender, dedup_key=dedup_key,
                             cwd=STATE.get("cwd") or WORKDIR)
    if dup:
        log(f"重复投递，忽略：{dedup_key} -> {tid}")
        return {"status": "duplicate", "id": tid}
    WAKE.set()
    sync_gauges()
    return {"status": "queued", "id": tid, "pos": STORE.queue_depth()}


async def submit_shell(text, source="qq", confirmed=False):
    cmd = text[1:].strip() if text.startswith("!") else text.strip()
    if not cmd:
        await send("用法：!<PowerShell 命令>")
        return {"status": "rejected", "error": "empty"}
    reason = danger_reason_shell(cmd)
    if reason and not confirmed:
        return await _ask_confirm("shell", text, source, reason)
    asyncio.create_task(handle_shell(text if text.startswith("!") else "!" + text))
    return {"status": "running"}


async def _run_confirmed(entry):
    if entry["kind"] == "task":
        res = await submit_task(entry["text"], entry["source"], confirmed=True)
        if res.get("status") == "queued":
            await send(f"✅ 已确认执行，排在第 {res['pos']} 位。")
        else:
            await send("⚠️ 队列满了，没提交成功。")
    else:
        await submit_shell(entry["text"], entry["source"], confirmed=True)
        await send("✅ 已确认执行命令。")


async def handle_shell(text: str):
    cmd = text[1:].strip()
    if not cmd:
        await send("用法：!<PowerShell 命令>")
        return
    tid = uuid.uuid4().hex[:8]
    t0 = time.time()
    audit(AUDIT_PATH, event="shell_start", id=tid, cmd=cmd[:500])
    log(f"直接命令：{cmd}")
    try:
        # 显式把 PowerShell 的输出编码设成 UTF-8，否则中文输出会按 GBK 出来变乱码
        wrapped = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                   "$OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd)
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", wrapped,
            cwd=STATE.get("cwd") or WORKDIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            _kill_tree(proc.pid)
            audit(AUDIT_PATH, event="shell_end", id=tid, ok=False,
                  ms=int((time.time() - t0) * 1000), note="timeout")
            await send("⏰ 超过 3 分钟，已结束。")
            return
        text_out = out.decode("utf-8", "replace").strip() or "（无输出）"
        audit(AUDIT_PATH, event="shell_end", id=tid, ok=True,
              ms=int((time.time() - t0) * 1000), out=text_out[:1500])
        await send(text_out)
    except Exception as e:
        audit(AUDIT_PATH, event="shell_end", id=tid, ok=False,
              ms=int((time.time() - t0) * 1000), error=str(e))
        await send(f"❌ 执行失败：{e}")


async def on_message(data):
    if data.get("post_type") != "message" or data.get("message_type") != "private":
        return
    uid = int(data.get("user_id", 0))
    text = (data.get("raw_message") or "").strip()
    if not text:
        return

    if not STATE["owner_uin"]:
        STATE["owner_uin"] = uid
        save_state()
        log(f"绑定主人：{uid}")
        await send(f"🔗 已绑定主人：{uid}\n发 /帮助 看用法。")
    elif uid != STATE["owner_uin"]:
        log(f"忽略非主人消息：{uid}: {text[:50]}")
        return

    log(f"收到：{text[:300]}")
    if text.startswith("!"):
        await submit_shell(text, "qq")
        return
    if await handle_command(text):
        return

    # message_id 做幂等键：NapCat 重连后可能重投同一条消息
    mid = data.get("message_id")
    dedup = f"qq:{mid}" if mid is not None else None
    res = await submit_task(text, "qq", dedup_key=dedup, sender=str(uid))
    status = res.get("status")
    if status == "queued":
        if res["pos"] > 1:
            await send(f"📥 已收到，排在第 {res['pos']} 位。（任务 {res['id']}）")
        else:
            await send(f"📥 收到，开始执行。（任务 {res['id']}）")
    elif status == "duplicate":
        log(f"忽略重复投递：{dedup}")
    elif status == "full":
        await send(f"📮 队列满了（{MAX_QUEUE} 个待执行），先等前面的跑完。")


SEEN_FLAGS = {}


async def on_request(data):
    if data.get("post_type") != "request" or data.get("request_type") != "friend":
        return
    uid = int(data.get("user_id", 0))
    flag = data.get("flag")
    now = time.time()
    if flag and now - SEEN_FLAGS.get(flag, 0) < 60:
        return
    SEEN_FLAGS[flag] = now
    if not STATE["owner_uin"] or uid == STATE["owner_uin"]:
        await CLIENT.call("set_friend_add_request", {"flag": flag, "approve": True})
        log(f"同意好友申请：{uid}")
        if not STATE["owner_uin"]:
            STATE["owner_uin"] = uid
            save_state()
            await send(f"🔗 已绑定主人：{uid}")
    else:
        await CLIENT.call("set_friend_add_request", {"flag": flag, "approve": False})
        log(f"拒绝好友申请：{uid}")


async def serve(ws):
    global CLIENT, PENDING_NOTICES
    client = Client(ws)
    CLIENT = client
    log("已连上 NapCat WebSocket")
    reader = asyncio.create_task(client.reader())
    try:
        if PENDING_NOTICES:
            names = "；".join(x[:60] for x in PENDING_NOTICES[:3])
            await send(f"⚠️ 上次重启时有 {len(PENDING_NOTICES)} 个任务被打断：\n{names}\n"
                       f"发 /任务 查看，需要的话重发。")
            PENDING_NOTICES = []
        while True:
            get_task = asyncio.create_task(client.events.get())
            done, _ = await asyncio.wait({get_task, reader}, return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                data = get_task.result()
                try:
                    if data.get("post_type") == "message":
                        asyncio.create_task(on_message(data))
                    elif data.get("post_type") == "request":
                        asyncio.create_task(on_request(data))
                except Exception:
                    logger.exception("event handler error")
            if reader in done:
                get_task.cancel()
                if reader.cancelled() or reader.exception():
                    log("WebSocket 连接异常结束，准备重连")
                else:
                    log("WebSocket 已断开，准备重连")
                break
    finally:
        reader.cancel()
        CLIENT = None


# ---------------- 看板回调 ----------------

def panel_state():
    st = STORE.stats()
    return {
        "panel": f"127.0.0.1:{PANEL_PORT}",
        "current": "；".join(f"{k[:6]} {v['text'][:30]}" for k, v in RUNNING.items()) or "",
        "queue": st["queue"],
        "done": st["done"],
        "failed": st["failed"],
        "cost": st["total_cost"],
        "uptime_hours": (time.time() - START_TS) / 3600,
        "cwd": STATE.get("cwd") or "",
        "session": (STATE.get("session_id") or "")[:8],
        "pending_confirm": bool(PENDING_CONFIRM.get("entry")),
        "p50_ms": st["p50_ms"],
        "p95_ms": st["p95_ms"],
        "success_rate": (st["done"] / (st["done"] + st["failed"]) * 100)
                        if (st["done"] + st["failed"]) else 0,
        "workers": WORKERS,
        "daily": STORE.daily(7),
        "metrics": METRICS.snapshot(),
    }


def panel_tasks():
    return STORE.recent(40)


def panel_events():
    return tail_events(AUDIT_PATH, 80)


def panel_metrics():
    sync_gauges()
    return METRICS.render()


async def main():
    global PENDING_NOTICES
    load_state()
    # 上次崩溃/重启时还在跑的任务：标 interrupted 并准备通知
    rows = STORE.recover_interrupted()
    PENDING_NOTICES = [f"{r['id']} {r['text'][:60]}" for r in rows]
    STATE["current_task"] = ""
    save_state()
    for i in range(WORKERS):
        asyncio.create_task(worker(i))
    panel = Panel(PANEL_PORT, PANEL_TOKEN, asyncio.get_running_loop(),
                  state_fn=panel_state, tasks_fn=panel_tasks, submit_coro=submit_task,
                  events_fn=panel_events, metrics_fn=panel_metrics)
    log(f"看板：{panel.start()}（worker={WORKERS}，队列库 {DB_PATH.name}）")
    url = CFG["napcat_ws"]
    token = CFG.get("napcat_token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    while True:
        try:
            async with connect(url, additional_headers=headers, ping_interval=20,
                               ping_timeout=20, max_size=2 ** 24) as ws:
                await serve(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("连接失败：%s，5 秒后重试", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
