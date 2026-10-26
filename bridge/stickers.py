# -*- coding: utf-8 -*-
"""本地表情库：让机器人在 QQ 里发「表情」而不是一张撑满气泡的大图。

原理（来自 qq-agent-plugins 的实测，不是猜的）：
    QQ 消息里的图片靠 `sub_type` 区分：
        sub_type = 0 → 普通图片，客户端按原图尺寸渲染
        sub_type = 1 → 表情，客户端按表情渲染（小而紧凑，可长按收藏）
    发图片段时带上 `sub_type: 1`，回收时确实就是 1 —— 所以「发表情」不用商城表情那套 ID。

怎么用：
    bridge/stickers/ 下丢图片（gif/png/jpg/webp），**文件名就是它的名字**，
    比如 `猫猫震惊.gif` → 名字「猫猫震惊」。
    可选 `index.json` 覆盖描述：{"猫猫震惊": {"file": "猫猫震惊.gif", "desc": "无语"}}

模型怎么表达「想发这个表情」：
    在回复末尾单独一行写 `@@表情:猫猫震惊@@`，bridge 把它摘掉、单独发一张表情。
    这和 qq-agent-plugins 的 `@@表情:id@@` 是同一套写法，只是这里的 id 就是文件名。
"""

import json
import re
from pathlib import Path

EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
# 模型偶尔会写成 @@表情：xxx@@（全角冒号）或者前后带空格，都认
STICKER_RE = re.compile(r"@@\s*表情\s*[:：]\s*([^@\n]{1,20}?)\s*@@")


# 模型偶尔把标记写歪：全角冒号、少一个 @、写成「贴纸 / 收藏」。正常写法在
# extract() 里就摘掉了，这里刮的是**漏网的残渣**——它们会原样发到手机上，
# 比不发还难看。宁可这次不发图，也不能让「@@表情:猫猫@@」当文字发出去。
_SCRUB = re.compile(
    r"@@\s*(?:表情|贴纸|收藏|sticker)\s*[:：]?[^@\n]{0,20}@@"
    r"|@_\s*(?:表情|贴纸|收藏)[^_\n]{0,20}@_",
    re.I)


def scrub(text: str) -> str:
    """把漏网的表情标记整段刮掉，返回干净文本。"""
    return _SCRUB.sub("", str(text or "")).strip()


def extract(text: str):
    """从回复里摘出 (剩下的话, 表情名或空串)。一条回复最多一张。"""
    raw = str(text or "")
    m = STICKER_RE.search(raw)
    if not m:
        return raw, ""
    # count=1：只摘第一张，多写的标记原样留着（人能看见，也方便发现模型在滥用）
    return STICKER_RE.sub("", raw, count=1).strip(), m.group(1).strip()


def clean_name(name, max_len: int = 12) -> str:
    """把用户给的名字收拾成能当文件名的样子。

    为什么要管：名字最后会拼进 `STICKER_DIR / f"{名字}{后缀}"`，
    带 `/`、`\\`、`..` 的名字能写到表情库外面去。太长也会让菜单糊成一团。
    """
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(name or "")).strip().strip(".")
    return s[:max_len]


def pick_save_source(paths, at: float, now: float, ttl: float = 120) -> str:
    """挑出"可以存成表情"的那张图；没有就返回空串。

    两道条件缺一不可：图还在磁盘上，而且**刚发的**（default 120 秒内）。
    没有时效限制的话，早上发的截图会在晚上被当成表情存进去。
    """
    if not paths or (now - float(at or 0)) > ttl:
        return ""
    for p in reversed(list(paths)):
        if p and Path(p).is_file():
            return str(p)
    return ""


def rules_text(menu: str, public: bool = False) -> str:
    """提示词里的表情说明书。菜单空着就返回空串——别教它写一个发不出去的标记。"""
    if not menu:
        return ""
    tail = "群里更别刷表情，一条顶天了。" if public else ""
    return ("（你手里有这些 QQ 表情，格式是 名字=它的样子：" + menu + "\n"
            "想发表情就在回复最后单独一行写 @@表情:名字@@，系统会把它摘掉、单独发一张。\n"
            "一条回复最多一张，但用得可以勤——吐槽、无奈、开心、懒得打字的时候都发，"
            "跟人发微信一个样。别同一个连着用。"
            "表情经常比文字更贴，只有一两句话的时候尤其适合。" + tail + "）")


class StickerBook:
    def __init__(self, folder):
        self.dir = Path(folder)
        self._items: dict[str, dict] = {}
        self.scan()

    def scan(self):
        """扫目录。文件名是名字，index.json 可覆盖描述与文件对应关系。"""
        items: dict[str, dict] = {}
        if not self.dir.is_dir():
            self._items = items
            return items
        for p in self.dir.iterdir():
            if p.is_file() and p.suffix.lower() in EXTS:
                items[p.stem] = {"file": p.name, "desc": p.stem}
        try:
            idx = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
            if isinstance(idx, dict):
                for name, meta in idx.items():
                    if not isinstance(meta, dict):
                        continue
                    fname = str(meta.get("file") or "").strip()
                    if fname and (self.dir / fname).is_file():
                        items[str(name)] = {
                            "file": fname,
                            "desc": str(meta.get("desc") or name),
                        }
        except Exception:
            pass
        self._items = items
        return items

    def __len__(self):
        return len(self._items)

    def menu(self) -> str:
        """给模型看的清单：名字=描述。太多时只给前 20 张，免得提示词太长。"""
        if not self._items:
            return ""
        names = list(self._items)[:20]
        return "、".join(f"{n}={self._items[n]['desc']}" for n in names)

    def path_for(self, name: str) -> str:
        """按名字（或描述）找文件，找不到返回空串。"""
        key = str(name or "").strip()
        if key in self._items:
            return str(self.dir / self._items[key]["file"])
        for n, meta in self._items.items():
            if key and (key in n or key == meta["desc"]):
                return str(self.dir / meta["file"])
        return ""


def segment(path: str) -> dict:
    """图片段 + sub_type=1 = 表情。路径用 file:// 让协议端直接读本地文件。"""
    p = Path(path).resolve().as_posix()
    return {"type": "image", "data": {"file": f"file:///{p}", "sub_type": 1}}
