# -*- coding: utf-8 -*-
"""发语音条：文字 → wav/mp3 → QQ 的 record 段。

## 三档引擎

| 引擎 | 音色 | 代价 | 什么时候用 |
|---|---|---|---|
| volc | 火山豆包语音合成 2.0，最像真人 | 要 key、按字符计费 | 配了 `secret/volc.json` 时优先 |
| edge-tts | 微软在线音色（晓晓等），自然 | 要联网、国内要过代理（7892） | volc 没配或失败时 |
| SAPI | Windows 自带离线（慧慧/康康/瑶瑶） | 机械、但零依赖、断网可用 | 永远兜底 |

火山这套的坑（2026-10-27 实测）：认证头是 `X-Api-Key`，**不是**文档里老版的
`X-Api-Access-Key`——用后者会 401 `45000010 load grant`，看起来像 key 错了，
其实是头名不对。`X-Api-App-Id` 必须是控制台「开通管理」里那个 APP ID，
跟 api key 同属一个应用，换成别的号也是同样的 401。

**两类音色不能混用**（2026-10-27 实测）：预置音色（`zh_female_xiaohe_uranus_bigtts`
这种）配 `seed-tts-2.0`；音色设计 / 声音复刻出来的音色（id 形如 `S_nvxPQIpj2`）配
`seed-icl-2.0`。配错会返回 `55000000 resource ID is mismatched with speaker related
resource`——HTTP 200、音频 0 字节，看着像"服务端抽风"，其实是两个产品线的 id 不通用。
`_guess_resource()` 按音色前缀自动选，所以换音色只要改 `tts.volc.voice` 一项。

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
import base64
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_SAPI_VOICE = "Microsoft Huihui Desktop"
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_VOLC_VOICE = "zh_female_xiaohe_uranus_bigtts"
DEFAULT_VOLC_RESOURCE = "seed-tts-2.0"
# 复刻/音色设计出来的音色（id 形如 S_xxxxxxxx）属于 ICL 产品线，必须配这个
# resource；拿它去 seed-tts-2.0 会被拒：55000000 resource ID is mismatched
# with speaker related resource（2026-10-27 实测，见 _icl2.py）。
VOLC_ICL_RESOURCE = "seed-icl-2.0"
VOLC_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
MAX_CHARS = 200
MIN_CHARS = 2

# 念出来很狼狈的东西：链接会被逐字拼读，标记会被当口头语
_URL = re.compile(r"https?://\S+")
_MARK = re.compile(r"@@[^@\n]{0,20}@@")

# 模型表达「这句我想用嘴说」的方式，和表情标记同一套写法：
#   在回复里写 @@语音:今天好累啊@@ —— bridge 摘掉标记，单独发一条语音条。
VOICE_RE = re.compile(r"@@\s*语音\s*[:：]\s*([^@\n]{1,200}?)\s*@@")
# 漏网的歪写法（全角冒号、少一个 @）：直接刮掉，绝不能当文字发到手机上
_SCRUB = re.compile(r"@@\s*语音\s*[:：]?[^@\n]{0,200}@@", re.I)
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


def extract(text: str):
    """从回复里摘出 (剩下的话, 要念的内容或空串)。一条回复最多一条语音。"""
    raw = str(text or "")
    m = VOICE_RE.search(raw)
    if not m:
        return raw, ""
    return VOICE_RE.sub("", raw, count=1).strip(), m.group(1).strip()


def scrub(text: str) -> str:
    """刮掉漏网的语音标记（写歪的那种），返回干净文本。"""
    return _SCRUB.sub("", str(text or "")).strip()


def rules_text(public: bool = False) -> str:
    """提示词里的语音说明书。什么时候该用嘴说，而不是打字。"""
    tail = "群里语音更少见，十句里顶多一条。" if public else ""
    return ("（你也能发语音条，跟真人一样。想用嘴说的时候，在回复里写 "
            "@@语音:要说的话@@，系统会把它摘掉、单独发一条语音出去。\n"
            "语音只适合**短、有情绪、打字表达不出来**的那种："
            "撒娇、叹气、笑、一句顺口的吐槽。可以偶尔替换掉文字。"
            "别用来念长句（超过 60 字就念不完），也别每条都发——"
            "真人发语音是有选择的，全发语音会显得懒或者像机器人。" + tail + "）")


def _guess_resource(voice: str) -> str:
    """按音色 id 猜该用哪个 X-Api-Resource-Id（两类音色不通用）。"""
    return VOLC_ICL_RESOURCE if str(voice or "").startswith("S_") \
        else DEFAULT_VOLC_RESOURCE


class TTS:
    def __init__(self, cfg: dict | None = None, cache_dir: str | Path | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        # auto = volc → edge → sapi 依次降级；也可以只写一个引擎（失败就没声）
        self.engine = str(cfg.get("engine") or "auto").lower()
        self.sapi_voice = str(cfg.get("sapi_voice") or DEFAULT_SAPI_VOICE)
        self.edge_voice = str(cfg.get("edge_voice") or DEFAULT_EDGE_VOICE)
        volc = cfg.get("volc") or {}
        self.volc_on = bool(volc.get("enabled", True))
        self.volc_voice = str(volc.get("voice") or DEFAULT_VOLC_VOICE)
        # resource 不写就按音色 id 猜：S_xxx 是复刻/设计音色 → seed-icl-2.0，
        # 其余（zh_female_*_bigtts 这种预置音色）→ seed-tts-2.0。
        # 想让某个音色强制走别的 resource 再显式写 resource_id。
        self.volc_resource = str(volc.get("resource_id") or _guess_resource(self.volc_voice))
        self.volc_speed = float(volc.get("speed_ratio", 1.0))
        self.volc_secret_file = str(volc.get("secret_file") or "").strip()
        self._volc_creds = None                      # (appid, api_key) 或 None=没配
        self._volc_loaded = False
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
        if "volc" in self._engines() and self.volc_ready():
            return f"语音 火山2.0（{self.volc_voice} → 失败降 edge/SAPI）" \
                if self.engine == "auto" else f"语音 火山2.0（{self.volc_voice}）"
        eng = "edge-tts" if self._edge_importable() else "SAPI"
        return f"语音 {eng}（{self.edge_voice if eng == 'edge-tts' else self.sapi_voice}）"

    def _engines(self) -> list:
        if self.engine != "auto":
            return [self.engine]
        order = ["volc", "edge", "sapi"]
        return [e for e in order if e != "volc" or self.volc_on]

    def _volc_credentials(self):
        """读 secret/volc.json。第一次读就缓存，读不到返回 None（不抛异常）。

        文件缺席是正常状态——没配火山就用 edge/SAPI，不该在启动时炸。
        """
        if self._volc_loaded:
            return self._volc_creds
        self._volc_loaded = True
        path = Path(self.volc_secret_file) if self.volc_secret_file \
            else Path(__file__).resolve().parents[1] / "secret" / "volc.json"
        try:
            j = json.loads(path.read_text(encoding="utf-8"))
            appid = str(j.get("appid") or "").strip()
            key = str(j.get("api_key") or j.get("access_token") or "").strip()
            if appid and key:
                self._volc_creds = (appid, key)
        except Exception:
            self._volc_creds = None
        return self._volc_creds

    def volc_ready(self) -> bool:
        return bool(self.volc_on and self._volc_credentials())

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

    def _voice_of(self, engine: str) -> str:
        if engine == "volc":
            return self.volc_voice
        return self.edge_voice if engine == "edge" else self.sapi_voice

    def _cache_path(self, text: str, engine: str) -> Path:
        voice = self._voice_of(engine)
        key = f"{engine}|{voice}|{self.rate}|{text}".encode("utf-8")
        ext = ".wav" if engine == "sapi" else ".mp3"
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

        for eng in self._engines():
            if eng == "volc" and not self.volc_ready():
                self.last_error = "火山语音没配（secret/volc.json）"
                continue
            if eng == "edge" and not self._edge_importable():
                self.last_error = "edge-tts 没装"
                continue
            path = self._cache_path(say, eng)
            if path.exists() and path.stat().st_size > 1000:
                self.last_engine = eng
                return str(path), eng
            try:
                if eng == "volc":
                    await asyncio.to_thread(self._volc, say, path)
                elif eng == "edge":
                    await self._edge(say, path)
                else:
                    await asyncio.to_thread(self._sapi, say, path)
                if path.exists() and path.stat().st_size > 1000:
                    self.last_engine = eng
                    self.last_error = ""      # 前面引擎跳过的原因不算这次的结果
                    return str(path), eng
                self.last_error = f"{eng} 合成结果为空"
            except Exception as e:
                self.last_error = f"{eng}: {type(e).__name__}: {e}"
                path.unlink(missing_ok=True)
        return "", ""

    def _volc(self, text: str, out: Path):
        creds = self._volc_credentials()
        if not creds:
            raise RuntimeError("没配 secret/volc.json")
        audio = volc_request(text, creds, voice=self.volc_voice,
                             resource=self.volc_resource,
                             speed_ratio=self.volc_speed,
                             timeout=self.timeout)
        if len(audio) < 2000:
            raise RuntimeError(f"返回音频太小（{len(audio)} 字节）")
        out.write_bytes(audio)

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


def volc_request(text: str, creds: tuple, voice: str = DEFAULT_VOLC_VOICE,
                 resource: str = DEFAULT_VOLC_RESOURCE, speed_ratio: float = 1.0,
                 timeout: float = 30.0) -> bytes:
    """调火山豆包语音合成 2.0，返回 mp3 字节。失败抛异常，异常里带服务端的码。

    接口是「单向流」：返回一堆 JSON 行，音频 base64 在每行的 data 字段，
    拼起来再解一次。按字符计费，所以调用方要先把文本截短。
    """
    appid, key = creds
    body = {
        "user": {"uid": "qq-remote"},
        "req_params": {
            "text": text,
            "speaker": voice,
            "audio_params": {"format": "mp3", "sample_rate": 24000,
                             "speech_rate": int(round((speed_ratio - 1) * 100))},
        },
    }
    req = urllib.request.Request(
        VOLC_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-App-Id": appid,
            # 注意：X-Api-Key。写成老文档的 X-Api-Access-Key 会 401 45000010。
            "X-Api-Key": key,
            "X-Api-Resource-Id": resource,
            "X-Api-Request-Id": str(uuid.uuid4()),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        snippet = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} {snippet}") from None
    chunks = []
    err = ""
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        data = obj.get("data")
        if isinstance(data, str) and data:
            chunks.append(base64.b64decode(data))
        head = obj.get("header") or {}
        code = head.get("code") or obj.get("code") or 0
        # 成功的码是 20000000（不是 0），别把它当错误
        if code and int(code) not in (0, 20000000):
            err = f'{code} {head.get("message") or obj.get("message") or ""}'
    if err:
        raise RuntimeError(err)
    if not chunks:
        raise RuntimeError("没拿到音频（返回体里没有 data）")
    return b"".join(chunks)


def build(cfg: dict | None = None) -> TTS:
    """给 bridge 用的构造入口：读 config 的 `tts` 段。"""
    cfg = cfg or {}
    return TTS(cfg, cache_dir=cfg.get("cache_dir") or None)
