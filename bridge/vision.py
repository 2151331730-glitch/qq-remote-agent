# -*- coding: utf-8 -*-
"""看图：把 QQ 里收到的图片变成一段模型能读的文字。

为什么要这一层：闲聊走 DeepSeek 直连，那条通道**只收文字**；任务通道虽然能用
Read 工具打开文件，但把图片塞进上下文一样看不见内容。所以要先把图变成字。

两条通道，按顺序用：

    ① 本地 OCR（Windows 自带的 Windows.Media.Ocr）
       离线、不花钱、不联网。手机截图、报错窗口、课件、二维码旁边的字——这些都认，
       中文英文混排也认。绝大多数的图其实走这一条就够了。

    ② 视觉模型（claude.exe + bee 中转里的真 Claude）
       照片、表情包这种"一个字都没有"的图，本地 OCR 只能给空白，才请它看一眼。
       这条要花钱、要代理，所以：只给主人用、每小时有上限、OCR 认出了字就不调用。

一个坑：powershell.exe 每次冷启动在这台机器上要 3.5-4.8 秒（实测），一图一进程
根本不能用于聊天。所以 OCR 是**一个常驻的 PowerShell 子进程**，桥启动时预热，
之后每张图只要 100-300ms。见 ocr_server.ps1。
"""

import atexit
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")

# 图片格式靠文件头判断，不靠文件名：QQ 给的下载链接经常是一串没有后缀的东西，
# 而表情要按原格式存（gif 存成 jpg 就不动了）。
_MAGIC = (
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"\x89PNG", ".png"),
    (b"\xff\xd8\xff", ".jpg"), (b"BM", ".bmp"),
)

# CJK 字符：中文、日文、韩文常用区。用来判断"这行要不要去掉字间空格"。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")


def sniff_ext(data: bytes) -> str:
    """按文件头猜后缀，认不出来就给 .jpg（QQ 图片绝大多数是 jpg）。"""
    head = bytes(data[:12]) if data else b""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def save_bytes(data: bytes, folder: Path, prefix: str = "") -> Path:
    """把图片字节落到本地，后缀按真实格式取。返回路径。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    if prefix:
        stem = f"{prefix}-{stem}"
    p = folder / f"{stem}{sniff_ext(data)}"
    p.write_bytes(data)
    return p


def tidy_text(text: str, max_chars: int = 0) -> str:
    """收拾 OCR 的原始输出。

    Windows 的中文 OCR 会把每个汉字当成一个"词"用空格连起来，直接给模型会变成
    「远 程 任 务 失 败」——模型读得懂，但很啰嗦，还浪费 token。所以：
    CJK 占比高的行把空格全删掉；纯英文行只压连续空格（英文单词之间的空格不能删）。
    """
    out = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        dense = re.sub(r"\s+", "", s)
        cjk = len(_CJK.findall(dense))
        if cjk >= 2 and cjk / max(1, len(dense)) >= 0.25:
            s = dense
        else:
            s = re.sub(r"[ \t]{2,}", " ", s)
        out.append(s)
    text = "\n".join(out).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "…（后面还有，太长了）"
    return text


def note_text(kind: str, text: str, path="") -> str:
    """一张图的识别结果 → 提示词里的一行「图里有什么」。

    标记带方括号是故意的：模型能一眼看出这是系统替它看出来的，不是主人说的。
    """
    if kind == "text":
        return "[图片里的文字]\n" + str(text or "")
    if kind == "see":
        return "[图片内容] " + str(text or "")
    return "[图片] 没认出内容（原图在 " + str(path) + "）"


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    """本地端口通不通。用来在花钱之前先问一句"代理活着吗"。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ps_exe() -> str:
    """必须是 Windows PowerShell 5.1：WinRT 投影在 pwsh 7 里用不了。"""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.is_file() else "powershell.exe"


class Ocr:
    """常驻的 Windows OCR 助手进程。"""

    def __init__(self, cfg: dict, folder: Path):
        self.enabled = bool(cfg.get("ocr", True))
        self.timeout = float(cfg.get("ocr_timeout_seconds", 25))
        self.max_chars = int(cfg.get("ocr_max_chars", 1200))
        self.min_chars = int(cfg.get("ocr_min_chars", 4))
        self.folder = Path(folder)
        self.script = Path(__file__).with_name("ocr_server.ps1")
        self._proc = None
        self._lock = threading.Lock()
        self._dead_reason = ""
        self.last_error = ""

    # ---- 进程管理 ----

    def available(self) -> bool:
        return (self.enabled and sys.platform == "win32"
                and self.script.is_file() and not self._dead_reason)

    def _spawn(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [_ps_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.script), "-Tmp", str(self.folder)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags)
        hello = proc.stdout.readline().strip() if proc.stdout else ""
        if hello != "READY":
            # 引擎建不起来（系统没装 OCR 语言包、企业策略禁 WinRT…）。记一次，
            # 之后不再反复尝试开机，免得每张图都白等 5 秒。
            self._dead_reason = hello or "no-ready"
            self.last_error = f"OCR 引擎不可用（{self._dead_reason}）"
            try:
                proc.kill()
            except Exception:
                pass
            return None
        return proc

    def _ensure(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._proc = self._spawn()
        return self._proc

    def warm(self) -> bool:
        """启动时预热：把 4 秒的冷启动挪到没人等的时候。"""
        if not self.available():
            return False
        with self._lock:
            return self._ensure() is not None

    def close(self):
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---- 识别 ----

    def read(self, path) -> str:
        """一张图 → 里面的文字。认不出来返回空串，绝不抛异常打断聊天。"""
        if not self.available():
            return ""
        with self._lock:
            proc = self._ensure()
            if proc is None:
                return ""
            out = self.folder / f"ocr-{uuid.uuid4().hex[:8]}.txt"
            try:
                # 必须给绝对路径：WinRT 的 GetFileFromPathAsync 不认相对路径，
                # 传相对的会抛一个很不直观的 Wait 异常。
                proc.stdin.write(f"{Path(path).resolve()}\t{out.resolve()}\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except Exception as e:
                self.last_error = f"OCR 进程通信失败：{e}"
                try:
                    proc.kill()
                except Exception:
                    pass
                self._proc = None
                return ""
            if not line:
                # 子进程自己退了（被杀、崩溃）：下次重新拉起
                self.last_error = "OCR 进程已退出"
                self._proc = None
                return ""
            verdict = line.strip()
            if verdict != "OK":
                self.last_error = f"OCR 失败：{verdict[:120]}"
                try:
                    out.unlink()
                except Exception:
                    pass
                return ""
            try:
                text = out.read_text(encoding="utf-8-sig", errors="replace")
            except Exception as e:
                self.last_error = f"OCR 结果读不到：{e}"
                text = ""
            try:
                out.unlink()
            except Exception:
                pass
        return tidy_text(text, self.max_chars)


class Describe:
    """视觉模型：让真 Claude（bee 中转）看一眼图，说人话描述内容。"""

    def __init__(self, cfg: dict, claude_exe: str, workdir: str):
        self.cfg = dict(cfg or {})
        self.enabled = bool(self.cfg.get("enabled", True))
        self.settings = str(self.cfg.get("settings") or "")
        self.model = str(self.cfg.get("model") or "claude-opus-5-5")
        self.proxy = str(self.cfg.get("proxy") or "http://127.0.0.1:7892")
        self.timeout = float(self.cfg.get("timeout_seconds", 150))
        self.max_per_hour = int(self.cfg.get("max_per_hour", 20))
        self.max_chars = int(self.cfg.get("max_chars", 400))
        self.claude = claude_exe or "claude"
        self.workdir = workdir
        self._stamps = []
        self.last_error = ""

    def settings_path(self) -> str:
        """没写路径就默认用桌面上那套 bee 配置（真正的 Claude 才看得懂图）。"""
        if self.settings:
            return self.settings
        return str(Path.home() / ".claude" / "settings-bee.json")

    def available(self) -> bool:
        return self.enabled and Path(self.settings_path()).is_file()

    def quota_ok(self) -> bool:
        now = time.time()
        self._stamps = [t for t in self._stamps if now - t < 3600]
        return len(self._stamps) < self.max_per_hour

    def _proxy_ready(self) -> bool:
        if not self.proxy:
            return True
        m = re.search(r"//([^:/]+):(\d+)", self.proxy)
        if not m:
            return True
        return port_open(m.group(1), int(m.group(2)))

    def describe(self, path, question: str = "") -> str:
        """返回一两句中文描述；不可用时返回空串并写 last_error。"""
        if not self.available():
            self.last_error = f"视觉通道没开（找不到 {self.settings_path()}）"
            return ""
        if not self.quota_ok():
            self.last_error = f"这一小时看图超过 {self.max_per_hour} 张了"
            return ""
        if not self._proxy_ready():
            self.last_error = "代理没开（bee 要连 127.0.0.1:7892）"
            return ""
        ask = "对方问的是：" + question.strip()[:200] + "\n" if question.strip() else ""
        prompt = ("用 Read 工具打开这个图片文件，然后回答：\n"
                  f"{Path(path)}\n\n"
                  f"{ask}"
                  "用一两句中文说清楚图里是什么、有什么值得注意的信息（人物、物体、场景、"
                  "文字、氛围）。不要客套，不要复述这段要求，直接说图。")
        args = [self.claude, "-p", prompt, "--output-format", "json",
                "--settings", self.settings_path(),
                "--model", self.model,
                "--tools", "Read", "--allowedTools", "Read",
                "--strict-mcp-config", "--max-turns", "5",
                "--dangerously-skip-permissions"]
        env = dict(os.environ)
        if self.proxy:
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = self.proxy
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout, env=env,
                               cwd=self.workdir or None,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self.last_error = f"视觉模型调用失败：{e}"
            return ""
        text = _result_text(r.stdout or "")
        if not text:
            self.last_error = (f"视觉模型没给结论（exit={r.returncode}）"
                               f"{(r.stderr or '')[:120]}")
            return ""
        self._stamps.append(time.time())
        return text[:self.max_chars]


def _result_text(stdout: str) -> str:
    """从 claude.exe --output-format json 的输出里抠出正文。

    正常是一行 JSON；万一夹了别的行，就从后往前找第一行能解析的。
    """
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if isinstance(obj, dict):
            res = obj.get("result") or obj.get("text") or ""
            if isinstance(res, str) and res.strip():
                return res.strip()
    return ""


class Vision:
    """bridge 只跟这个门面打交道：给它图片路径，它还一段能塞进提示词的文字。"""

    def __init__(self, cfg: dict, claude_exe: str = "", workdir: str = ""):
        self.ocr = None
        self.desc = None
        self.reload(cfg or {}, claude_exe, workdir)

    def reload(self, cfg: dict, claude_exe: str = "", workdir: str = ""):
        folder = Path(cfg.get("ocr_tmp_dir") or (Path(__file__).parent / "tmp" / "ocr"))
        self.enabled = bool(cfg.get("enabled", True))
        self.owner_vision = bool(cfg.get("owner_vision", True))
        old = self.ocr
        self.ocr = Ocr(cfg, folder)
        self.desc = Describe(cfg.get("describe") or {}, claude_exe, workdir)
        if old is not None and old is not self.ocr:
            try:
                old.close()
            except Exception:
                pass

    # ---- 生命周期 ----

    def warm(self) -> bool:
        return self.ocr.warm() if self.enabled else False

    def close(self):
        try:
            self.ocr.close()
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ocr": bool(self.ocr and self.ocr.available()),
            "vision": bool(self.desc and self.desc.available()),
            "ocr_error": getattr(self.ocr, "last_error", ""),
            "vision_error": getattr(self.desc, "last_error", ""),
            "hour_used": len(getattr(self.desc, "_stamps", []) or []),
            "hour_cap": getattr(self.desc, "max_per_hour", 0),
        }

    # ---- 主入口 ----

    def look(self, path, question: str = "", allow_vision: bool = False):
        """看一张图，返回 (来源, 文字)。来源是 "text" / "see" / ""。

        text = 图里的字（本地 OCR，免费）
        see  = 图的内容（视觉模型，花钱；allow_vision 才走）
        """
        if not self.enabled or not path:
            return "", ""
        text = ""
        try:
            text = self.ocr.read(path)
        except Exception as e:
            self.ocr.last_error = f"OCR 异常：{e}"
        if text and len(text) >= self.ocr.min_chars:
            return "text", text
        if allow_vision and self.desc is not None:
            seen = ""
            try:
                seen = self.desc.describe(path, question)
            except Exception as e:
                self.desc.last_error = f"视觉模型异常：{e}"
            if seen:
                return "see", seen
        if text:
            return "text", text
        return "", ""


_VISION = None


def get(cfg=None, claude_exe: str = "", workdir: str = ""):
    """取全局单例。bridge 传配置进来；测试可以反复调。"""
    global _VISION
    if _VISION is None:
        _VISION = Vision(cfg or {}, claude_exe, workdir)
        atexit.register(_VISION.close)
    elif cfg is not None:
        _VISION.reload(cfg, claude_exe, workdir)
    return _VISION
