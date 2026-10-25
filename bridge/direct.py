# -*- coding: utf-8 -*-
"""直连 LLM API 的轻量聊天通道（不走 claude.exe）。

## 为什么要它

`claude.exe -p` 是「程序员外壳」：每次调用都要塞进内置工具定义、MCP 工具、
Claude Code 系统提示、以及自动读的 CLAUDE.md/AGENTS.md。实测在空 cwd 里
关掉 MCP 和工具，一句 `hi` 仍然要 14443 输入 token；哪怕全部关死也要 11172。
而同一句 `hi` 直连 `/v1/messages` 只要 **41 token**。

而且它还有第二层伤害：闲聊的 `chat_session_token_limit`（30000）比这个固定开销
（约 30100）还小，于是**每一句话都会触发换会话** —— 上下文永远用不上。

所以闲聊走这里：一个 HTTP POST，带上人格 + 长期记忆 + 最近几轮，拿到文本就返回。
- 成本：约 200~2000 token/句（随对话长度线性增长）
- 延迟：0.8~1.6 秒（不含模型思考）
- 上下文：真的能连续，几十轮都记得

任务通道仍然走 claude.exe —— 那边需要工具和文件系统权限，贵是应该的。

## 配置

从 `config.json` 的 `chat_direct` 段读：

    "chat_direct": {
      "enabled": true,
      "base_url": "https://api.deepseek.com/anthropic",
      "api_key_env_from": "",          // 空 = 用 api_key 字段
      "api_key": "",                   // 留空则尝试从 ~/.claude/settings*.json 找
      "model": "deepseek-v4-flash",
      "max_tokens": 400,
      "temperature": 1.0,
      "history_turns": 20,             // 带最近几轮进上下文（一问一答算一轮）
      "timeout_seconds": 60,
      "stream": true
    }

`api_key` / `base_url` 不打印、不落日志。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# 单轮对话上下文的硬上限：超过就丢最旧的一半。够聊很久，又不至于无限增长。
MAX_CONTEXT_TURNS = 60

# 垫底上下文的过滤：这些不是"对话"，塞进去只会污染语感
_NOISE = re.compile(
    r"^[/!]|"                          # 斜杠命令 / shell 命令
    r"^\[CQ:|"                          # 没解析干净的 CQ 码
    r"^(👍|👌|😂|🤣|🙏|❤️|好的|嗯嗯|收到|ok|OK|okay)$|"   # 纯表情/单字
    r"^traceback|^Traceback|"
    r"^\d{4}-\d{2}-\d{2}\s",          # 日志行
    re.I)
# 短于这个长度、又不含中文/英文实词的，也丢掉
_MIN_KEEP = 2


def _worth_keeping(body: str) -> bool:
    """判断一条记录值不值得进上下文。"""
    t = body.strip()
    if len(t) < _MIN_KEEP:
        return False
    if _NOISE.search(t):
        return False
    # 去掉 emoji、标点、空白后还剩不剩东西
    core = re.sub(r"[\s\W_]+", "", t, flags=re.U)
    return len(core) >= _MIN_KEEP


def _find_key_from_settings():
    """从 ~/.claude/settings*.json 里捞一个可用的 key + base_url。

    本机有两套配置（DeepSeek / bee），各写各的。这里优先 DeepSeek —— 便宜、
    国内直连不用代理，闲聊够用。找不到返回 ("", "")。
    """
    home = Path(os.path.expanduser("~"))
    candidates = [
        (home / ".claude" / "settings.json", "deepseek"),
        (home / ".claude" / "settings-bee.json", "bee"),
        (home / ".claude" / "settings-zhipu.json", "zhipu"),
    ]
    for path, name in candidates:
        try:
            if not path.exists():
                continue
            env = (json.loads(path.read_text(encoding="utf-8")) or {}).get("env") or {}
            key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
            base = env.get("ANTHROPIC_BASE_URL") or ""
            if key and base:
                return key, base
        except Exception:
            continue
    return "", ""


class DirectChat:
    """一次问答 = 一次 HTTP POST。没有子进程、没有会话文件。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "deepseek-v4-flash"
        self.max_tokens = int(cfg.get("max_tokens", 400))
        self.temperature = float(cfg.get("temperature", 1.0))
        self.history_turns = int(cfg.get("history_turns", 20))
        self.timeout = int(cfg.get("timeout_seconds", 60))
        self.stream = bool(cfg.get("stream", True))
        # 历史持久化：重启后上下文还在，不用从聊天记录重新猜
        self.store_path = Path(cfg.get("history_path") or "") if cfg.get("history_path") else None
        if self.enabled and (not self.api_key or not self.base_url):
            k, b = _find_key_from_settings()
            self.api_key = self.api_key or k
            self.base_url = self.base_url or b.rstrip("/")
        # 每个 scope 一段对话历史：主人 / 陌生人 / 每个群 各自独立
        self.histories: dict[str, list[dict]] = {}
        self.last_usage = {"in": 0, "out": 0}
        self.last_error = ""
        self._touched = False
        self._load()

    # ---------------- 持久化 ----------------

    def _load(self):
        """把上次的历史读回来。文件坏了就当没有，不能因此起不来。"""
        if not self.store_path:
            return
        try:
            if not self.store_path.exists():
                return
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            cap = min(self.history_turns * 2, MAX_CONTEXT_TURNS * 2)
            for scope, turns in data.items():
                if not isinstance(turns, list):
                    continue
                clean = []
                for m in turns[-cap:]:
                    if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                            and isinstance(m.get("content"), str) and m["content"].strip()):
                        clean.append({"role": m["role"], "content": m["content"]})
                if clean:
                    self.histories[str(scope)] = clean
        except Exception:
            pass

    def save(self, force: bool = False):
        """落盘。只在有新内容时写，避免每句都 fsync。"""
        if not self.store_path:
            return
        if not force and not self._touched:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            cap = min(self.history_turns * 2, MAX_CONTEXT_TURNS * 2)
            data = {k: v[-cap:] for k, v in self.histories.items() if v}
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.store_path)      # 原子替换，断电不会写坏
            self._touched = False
        except Exception:
            pass

    # ---------------- 可用性 ----------------

    def available(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url)

    def status_line(self) -> str:
        if not self.enabled:
            return "直连关闭"
        if not self.available():
            return "直连缺 key/base_url"
        return f"直连 {self.model}"

    # ---------------- 上下文 ----------------

    def history_for(self, scope: str) -> list[dict]:
        return self.histories.setdefault(scope, [])

    def add_turn(self, scope: str, role: str, text: str):
        if not (text or "").strip():
            return
        h = self.history_for(scope)
        h.append({"role": role, "content": text.strip()})
        self._touched = True
        # 硬上限：一问一答算 2 条，超过就把最旧的丢掉一半
        cap = min(self.history_turns * 2, MAX_CONTEXT_TURNS * 2)
        if len(h) > cap:
            del h[:len(h) - cap]

    def reset(self, scope: str = ""):
        if scope:
            self.histories.pop(scope, None)
        else:
            self.histories.clear()
        self._touched = True

    def seed_history(self, scope: str, transcript: str):
        """用本地聊天记录（`名字: 内容` 格式）给一段会话垫底。

        只在内存里没有历史时调（刚重启 / 刚被清理）。对方说的当 user，
        自己说的当 assistant —— 这样模型一开口就接得上。

        要过滤掉：斜杠命令、`!` 命令、纯 emoji、极短的语气词。这些塞进上下文
        既浪费 token，又会让模型的语感跑偏（曾经把 `/心跳` 当成正经对话）。
        """
        if self.history_for(scope) or not transcript:
            return 0
        h = self.history_for(scope)
        for line in transcript.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, body = line.partition(":")
            body = body.strip()
            if not body or not _worth_keeping(body):
                continue
            role = "assistant" if name.strip() in ("我", "助手") else "user"
            # 连续同角色要合并，Anthropic 格式不允许两条 user 挨着
            if h and h[-1]["role"] == role:
                h[-1]["content"] += "\n" + body
            else:
                h.append({"role": role, "content": body})
        # 末尾必须是 assistant：否则最后一条 user 是"上次没回答的提问"，
        # 模型会把它和这次的新消息合并起来一起答（实测出现过答非所问）。
        # 垫一个中性收尾，把那条问题"关掉"。
        if h and h[-1]["role"] == "user":
            h.append({"role": "assistant", "content": "嗯"})
        # 整段里一个 user 都没有（记录里只有自己说的话）：这种"上下文"没有对方，
        # 留着只会让模型的对话格式不合法，直接丢掉。
        if not any(m["role"] == "user" for m in h):
            h.clear()
        # 开头也不能是 assistant
        while h and h[0]["role"] == "assistant":
            h.pop(0)
        cap = min(self.history_turns * 2, MAX_CONTEXT_TURNS * 2)
        if len(h) > cap:
            del h[:len(h) - cap]
        # 垫底不是"新内容"，但为了让重启后还能用，还是要落盘
        self._touched = True
        return len(h)

    # ---------------- 调用 ----------------

    def ask(self, scope: str, text: str, system: str, on_delta=None) -> tuple[str, bool]:
        """发一句话，返回 (回复文本, 成功?)。失败时返回 ("", False)，错误在 last_error。

        on_delta(chunk) 会在流式返回时被逐段调用，用来边生成边吐到 QQ。
        """
        self.last_error = ""
        if not self.available():
            self.last_error = "直连未配置"
            return "", False

        self.add_turn(scope, "user", text)
        msgs = [dict(m) for m in self.history_for(scope)]
        # 相邻同角色合并（历史里可能有连续的 user）
        merged: list[dict] = []
        for m in msgs:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n" + m["content"]
            else:
                merged.append(m)
        if not merged or merged[-1]["role"] != "user":
            merged.append({"role": "user", "content": text})
        if merged[0]["role"] != "user":
            merged.pop(0)

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": merged,
        }
        if system:
            payload["system"] = system
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.stream and on_delta:
            payload["stream"] = True

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/v1/messages", data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "authorization": "Bearer " + self.api_key,
                "anthropic-version": "2023-06-01",
                "accept": "text/event-stream" if payload.get("stream") else "application/json",
            })

        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if payload.get("stream"):
                    reply = self._read_stream(resp, on_delta)
                else:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                    reply = "".join(b.get("text", "") for b in data.get("content") or []
                                    if isinstance(b, dict))
                    u = data.get("usage") or {}
                    self.last_usage = {"in": int(u.get("input_tokens") or 0),
                                       "out": int(u.get("output_tokens") or 0)}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            self.last_error = f"HTTP {e.code} {detail}"
            if self.history_for(scope):
                self.history_for(scope).pop()      # 失败就把这句话撤回，别污染上下文
                self._touched = True
                self.save()
            return "", False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            if self.history_for(scope):
                self.history_for(scope).pop()
                self._touched = True
                self.save()
            return "", False

        reply = (reply or "").strip()
        if not reply:
            self.last_error = "空回复"
            if self.history_for(scope):
                self.history_for(scope).pop()
                self._touched = True
                self.save()
            return "", False
        self.add_turn(scope, "assistant", reply)
        self.save()                    # 一问一答落盘，重启后接得上
        self._last_seconds = time.time() - started
        return reply, True

    def _read_stream(self, resp, on_delta) -> str:
        """解析 SSE：`data: {...}` 逐行，取出 text_delta 和 usage。"""
        out: list[str] = []
        buf = ""
        for raw in resp:
            line = (raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray))
                    else str(raw)).strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except Exception:
                continue
            t = ev.get("type")
            if t == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    out.append(d["text"])
                    buf += d["text"]
                    # 攒到一句话的断点再吐，避免 QQ 里刷屏
                    if len(buf) >= 18 or buf.rstrip()[-1:] in "。！？!?~…\n":
                        try:
                            on_delta(buf)
                        except Exception:
                            pass
                        buf = ""
            elif t == "message_delta":
                u = ev.get("usage") or {}
                if u.get("output_tokens"):
                    self.last_usage["out"] = int(u["output_tokens"])
            elif t == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                if u.get("input_tokens"):
                    self.last_usage["in"] = int(u["input_tokens"])
            elif t == "error":
                self.last_error = str(ev.get("error"))[:200]
        if buf:
            try:
                on_delta(buf)
            except Exception:
                pass
        return "".join(out)
