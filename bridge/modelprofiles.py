# -*- coding: utf-8 -*-
"""闲聊模型档案：只描述「闲聊走哪个端点、用哪个模型」。

## 边界（2026-10-25 定）

**任务永远走配置里的 DeepSeek**，不读档案、不加 `--settings`、不过代理。
`/model` 只切闲聊直连通道：

| 档案 | 端点 | 闲聊模型 | 延迟参考（同一句话实测） |
|---|---|---|---|
| flash（别名 deepseek） | DeepSeek 直连 | 配置里的 `chat_direct.model` | ~1.0 s / 77 in |
| grok | bee（beeapi.ai，需 Pigeon 7892） | grok-4.7 | ~3.3 s / 344 in |

## 为什么单独一个模块

`bridge.py` 一 import 就会读真配置、建队列，测试很难隔离。这里只有纯函数
（读配置、探端口、存状态），`tests/test_model_switch.py` 可以直接测。

## 安全

token 只在 `bee_credentials()` 的返回值里活一次，不打印、不落日志；
`model_state.json` 只存档案名和模型 id。
"""
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

# 档案只影响闲聊。provider=bee 才需要代理；default = 配置里的 DeepSeek 端点。
MODEL_PROFILES = {
    "flash": {"label": "DeepSeek Flash", "provider": "default", "model": ""},
    "grok":  {"label": "Grok 4.7（bee）", "provider": "bee",    "model": "grok-4.7"},
}

# 手机上顺手打的别名
ALIASES = {"deepseek": "flash", "ds": "flash", "默认": "flash", "default": "flash"}

DEFAULT_PROFILE = "flash"
BEE_PROXY = "http://127.0.0.1:7892"
PIGEON_EXE = Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "Pigeon" / "Pigeon.exe"


def canonical(name: str) -> str:
    """别名 / 大小写 / 空格 → 规范档案名。未知的原样返回（交给 spec_for 判空）。"""
    n = (name or "").strip().lower()
    return ALIASES.get(n, n)


def spec_for(name: str) -> dict | None:
    """档案名 → 配置。大小写不敏感、认别名，未知返回 None。"""
    return MODEL_PROFILES.get(canonical(name))


# ---------------- 自然语言切模型 ----------------
# 主人常直接说「你接的模型换成 deepseek」而不是发 /model。以前这种话会被当成闲聊，
# 由模型自己回答——它没有切换工具，只能编一句「换不了，模型是平台定的」。
# 这里只认「切换动词 + 已知模型名」同时出现的**短句**，把动作提到 bridge 层执行。
SWITCH_VERBS = ("换成", "换到", "换成个", "切换成", "切换到", "切到", "切成",
                "改成", "改为", "调成", "换回", "换个", "换一个", "切个")
MODEL_WORDS = {"deepseek": "flash", "flash": "flash", "grok": "grok"}

_DS_WORD = re.compile(r"(?<![a-z])ds(?![a-z])")


def detect_switch_request(text: str) -> str | None:
    """一句话 → 要切的档案名；只说「换个模型」却没给目标 → "hint"；不像在切模型 → None。

    两条护栏，避免把「换成 deepseek 写的代码再跑一遍」这种活当成切换：
    · 整句不超过 30 字；
    · 模型名必须落在句尾（允许后面跟「模型」和语气词）。
    """
    t = (text or "").strip()
    if not t or len(t) > 30:
        return None
    low = t.lower()
    has_verb = any(v in t for v in SWITCH_VERBS)

    # 去掉句尾语气词和「模型」二字，再看模型名是不是就在结尾附近
    tail = low.rstrip("。！!？?，,吧啊呗呀了哦呢 ")
    tail = re.sub(r"(的)?模型$", "", tail).rstrip("。！!？?，,吧啊呗呀了哦呢 ")
    tail_window = tail[-12:]

    target = None
    for word, profile in MODEL_WORDS.items():
        if word in tail_window:
            target = profile
            break
    if target is None and _DS_WORD.search(tail_window):
        target = "flash"
    if target and has_verb:
        return target
    if target is None and "模型" in t and (has_verb or "换" in t or "切" in t):
        return "hint"
    return None


def bee_credentials(settings_path) -> dict:
    """从 bee 的 Claude Code 配置里取 base_url + token。

    token 只留在内存里：不打印、不写日志、不进 model_state.json。
    读不出来返回 {}（调用方据此给主人一句人话，而不是抛异常）。
    """
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8")) or {}
        env = data.get("env") or {}
    except Exception:
        return {}
    base = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    if not base or not key:
        return {}
    return {"base_url": base, "api_key": key}


def proxy_port(proxy_url: str = BEE_PROXY) -> int:
    try:
        return int(urlparse(proxy_url).port or 7892)
    except Exception:
        return 7892


def proxy_alive(proxy_url: str = BEE_PROXY, timeout: float = 0.6) -> bool:
    """本地代理端口通不通。bee 不走它 = 每次请求都等到超时，先探一下能省 60 秒。"""
    try:
        with socket.create_connection(("127.0.0.1", proxy_port(proxy_url)), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_proxy(proxy_url: str = BEE_PROXY, autostart: bool = True,
                 exe: Path | None = None, wait_seconds: float = 10.0) -> bool:
    """代理没开就试着拉起 Pigeon（和桌面启动器 claude-bee.ps1 的做法一致）。"""
    if proxy_alive(proxy_url):
        return True
    if not autostart:
        return False
    exe = Path(exe) if exe else PIGEON_EXE
    if not exe.exists():
        return False
    try:
        subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.5)
        if proxy_alive(proxy_url):
            return True
    return False


def load_state(path) -> dict:
    """读上次的档案。文件坏了/不存在都当"没有"，不能让桥起不来。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, profile: str, model: str, provider: str) -> bool:
    """原子写。只存名字，不存 key。"""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"profile": profile, "model": model,
                                   "provider": provider}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False
