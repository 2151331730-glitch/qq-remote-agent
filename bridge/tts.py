# -*- coding: utf-8 -*-
"""发语音条：文字 → wav/mp3 → QQ 的 record 段。

## 两档引擎

| 引擎 | 音色 | 代价 | 什么时候用 |
|---|---|---|---|
| edge-tts | 微软在线音色（晓晓等），自然 | 要联网、国内要过代理（7892） | 装了 edge-tts 且能连上时优先 |
| SAPI | Windows 自带离线（慧慧/康康/瑶瑶） | 机械、但零依赖、断网可用 | 永远兜底 |

SAPI 走 PowerShell 的 `System.Speech`：文本**写到 UTF-8 临时文件**再让 PS 读，
不通过命令行传参——命令行中文在 PS 5.1 下会被按 GBK 解释，实测直接变乱码。
生成的 .ps1 只用 ASCII，所以不踩「PowerShell 读中文注释」那个 BOM 坑。

## 缓存

同一句话 + 同一个音色 → 同一个文件，不重复合成（`bridge/tmp/tts/`）。
名字是 sha1，音频内容不可读；启动时清掉 7 天前的旧文件。

## 长度

QQ 语音条最长 60 秒。中文约 4~5 字/秒，所以 200 字已接近上限；
超过这个长度就不该发语音，调用方应改为发文字。
"""
import asyncio
import hashlib
import re
import subprocess
import time
from pathlib import Path

DEFAULT_SAPI_VOICE = "Microsoft Huihui Desktop"
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_CHARS = 200
MIN_CHARS = 2

# 念出来很狼狈的东西：链接会被逐字拼读，标记会被当口头语
_URL = re.compile(r"https?://\S+")
_MARK = re.compile(r"@@[^@\n]{0,20}@@")
_CODE = re.compile(r"`{1,3}[^`]*`{1,3}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

_PS = r"""
param([string]$TextFile, [string]$OutFile, [string]$Voice, [int]$Rate)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($Voice) { try { $s.SelectVoice($Voice) } catch { } }
try { $s.Rate = $Rate } catch { }
$txt = Get-Content -LiteralPath $TextFile -Raw -Encoding UTF8
$s.SetOutputToWaveFile($OutFile)
$s.Speak($txt)
$s.Dispose()
"""


def clean_text(text: str, limit: int = MAX_CHARS) -> str:
    """把回复改写成「适合念」的样子：去链接、去标记、去代码、压空白。

    **不改内容**，只删除念出来会很怪的东西——TTS 把 URL 逐字拼读出来
    比不说话还糟。
    """
    t = str(text or "")
    t = _URL.sub("链接", t)
    t = _MARK.sub("", t)
    t = _CODE.sub("", t)
    t = _CTRL.sub(" ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    t = t.strip()
    if len(t) > limit:
        # 在标点处收尾，别念到半个词
        cut = t[:limit]
        for sep in ("。", "！", "？", "，", "、", "；", "\n", " "):
            i = cut.rfind(sep)
            if i >= limit // 2:
                cut = cut[:i]
                break
        t = cut.strip()
    return t


def segment(path: str) -> dict:
    """OneBot 的语音段。用 file:// 绝对路径，跟发图那条路一致。"""
    uri = Path(path).resolve().as_uri()
    return {"type": "record", "data": {"file": uri}}


class TTS:
    def __init__(self, cfg: dict | None = None, cache_dir: str | Path | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        # auto = 先 edge 后 sapi；sapi = 只用本地；edge = 只用在线的（失败就没声）
        self.engine = str(cfg.get("engine") or "auto").lower()
        self.sapi_voice = str(cfg.get("sapi_voice") or DEFAULT_SAPI_VOICE)
        self.edge_voice = str(cfg.get("edge_voice") or DEFAULT_EDGE_VOICE)
        self.rate = int(cfg.get("rate", 0))           # SAPI：-10(慢)~10(快)
        self.edge_rate = str(cfg.get("edge_rate") or "+0%")
        self.proxy = str(cfg.get("proxy") or "").strip()
        self.timeout = float(cfg.get("timeout_seconds", 30))
        self.max_chars = int(cfg.get("max_chars", MAX_CHARS))
        self.cache_dir = Path(cache_dir or (Path(__file__).parent / "tmp" / "tts"))
        self.last_engine = ""
        self.last_error = ""
        self._edge_ok = None                          # None=还没试过
        if self.enabled:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._prune()
            except Exception:
                pass

    # ---------------- 可用性 ----------------

    def available(self) -> bool:
        return bool(self.enabled)

    def status_line(self) -> str:
        if not self.enabled:
            return "语音关闭"
        eng = "edge-tts" if self._edge_importable() else "SAPI"
        return f"语音 {eng}（{self.edge_voice if eng == 'edge-tts' else self.sapi_voice}）"

    def _edge_importable(self) -> bool:
        if self._edge_ok is not None:
            return self._edge_ok
        try:
            import edge_tts  # noqa: F401
            self._edge_ok = True
        except Exception:
            self._edge_ok = False
        return self._edge_ok

    # ---------------- 缓存 ----------------

    def _cache_path(self, text: str, engine: str) -> Path:
        voice = self.edge_voice if engine == "edge" else self.sapi_voice
        key = f"{engine}|{voice}|{self.rate}|{text}".encode("utf-8")
        ext = ".mp3" if engine == "edge" else ".wav"
        return self.cache_dir / (hashlib.sha1(key).hexdigest()[:16] + ext)

    def _prune(self, keep_seconds: int = 7 * 86400):
        """清掉过期缓存：语音文件只能重生成，留着只是占盘。"""
        cutoff = time.time() - keep_seconds
        try:
            for p in self.cache_dir.iterdir():
                if p.is_file() and p.suffix.lower() in (".wav", ".mp3") \
                        and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except Exception:
            pass

    # ---------------- 合成 ----------------

    async def synth(self, text: str) -> tuple[str, str]:
        """返回 (音频文件路径, 用的引擎)；失败返回 ("", "")，原因在 last_error。"""
        self.last_error = ""
        self.last_engine = ""
        if not self.enabled:
            self.last_error = "语音关闭"
            return "", ""
        say = clean_text(text, self.max_chars)
        if len(say) < MIN_CHARS:
            self.last_error = "内容太短，发语音不合适"
            return "", ""

        engines = ["edge", "sapi"] if self.engine == "auto" else [self.engine]
        for eng in engines:
            if eng == "edge" and not self._edge_importable():
                self.last_error = "edge-tts 没装"
                continue
            path = self._cache_path(say, eng)
            if path.exists() and path.stat().st_size > 1000:
                self.last_engine = eng
                return str(path), eng
            try:
                if eng == "edge":
                    await self._edge(say, path)
                else:
                    await asyncio.to_thread(self._sapi, say, path)
                if path.exists() and path.stat().st_size > 1000:
                    self.last_engine = eng
                    return str(path), eng
                self.last_error = f"{eng} 合成结果为空"
            except Exception as e:
                self.last_error = f"{eng}: {type(e).__name__}: {e}"
                path.unlink(missing_ok=True)
        return "", ""

    async def _edge(self, text: str, out: Path):
        import edge_tts
        kwargs = {"rate": self.edge_rate}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        comm = edge_tts.Communicate(text, self.edge_voice, **kwargs)
        await asyncio.wait_for(comm.save(str(out)), timeout=self.timeout)

    def _sapi(self, text: str, out: Path):
        """离线合成：文本走文件，脚本纯 ASCII（见模块顶部说明）。"""
        tmp_txt = out.with_suffix(".txt")
        tmp_ps1 = out.with_suffix(".ps1")
        tmp_txt.write_text(text, encoding="utf-8")
        tmp_ps1.write_text(_PS, encoding="ascii")
        try:
            cmd = ["powershell", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-File", str(tmp_ps1),
                   "-TextFile", str(tmp_txt), "-OutFile", str(out),
                   "-Voice", self.sapi_voice, "-Rate", str(self.rate)]
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace")[-300:]
                raise RuntimeError(err or f"exit {r.returncode}")
        finally:
            tmp_txt.unlink(missing_ok=True)
            tmp_ps1.unlink(missing_ok=True)


def build(cfg: dict | None = None) -> TTS:
    """给 bridge 用的构造入口：读 config 的 `tts` 段。"""
    cfg = cfg or {}
    return TTS(cfg, cache_dir=cfg.get("cache_dir") or None)
