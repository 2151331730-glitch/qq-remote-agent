# -*- coding: utf-8 -*-
"""从聊天里学到的行为规则：说「下次别这么说」，之后真的不这么说。

## 为什么需要这一层

用户抱怨过一件事：他在 QQ 上说「下次把这句话省了」，机器人回「行 下次直接说事」，
但**下一条回复照旧**。他说了两遍，`memory.md` 里一条都没记下。

根因不是记忆坏了，而是两件事：
1. 引用回复的 `[CQ:reply,id=...]` 没剥掉，模型根本没意识到那是在纠正它；
2. 就算它"理解"了，也只是一句生成出来的话 —— 没有任何东西把它变成约束。

所以这里做的是：**把纠正变成能机械检查的规则**，在每条回复发出前跑一遍。

## 两类学习，别混

- **B 类（规则）**：能机械判定的形式约束。禁用某个词、不要反问、字数上限、条数上限。
  发出前用代码检查，违反了就修。**不依赖模型"记得遵守"**。
- **A 类（偏好）**：说不清、判不了的。写进 `memory.md`，靠提示词注入，模型自己把握。
  例如"我不喜欢被追问" —— 没法机械判"这算不算追问"。

分界线是**能不能写成代码判定**。宁少勿滥：一条管得很准的规则，
比十条看着漂亮但验证不了的规则有用。

## 稳定性

规则是持久化在 JSON 文件里的，`/规则` 能看到，`/规则 删 <号>` 能撤。
自动提炼有额度限制（每天最多几条），并且同一类规则重复出现时会合并 ——
避免"越学越怪"。
"""
import json
import re
import time
from pathlib import Path

# 每天最多新增多少条自动规则，超了就只记不生效（防止短时间被灌爆）
MAX_NEW_PER_DAY = 5
# 规则总数上限，超了丢最久没命中的
MAX_RULES = 40

# ---- 能机械判定的规则类型 ----

RULE_KINDS = {
    "no_word":       "回复里不能出现某个词/短语",
    "no_question":   "结尾不要反问（问「要不要…」「…吗」）",
    "max_chars":     "单条回复的字数上限",
    "max_bubbles":   "一次最多发几条",
    "no_open_ended": "不要给「还要不要…」这类后续提议",
}


class RuleStore:
    """规则的读写与执行。文件是 JSON，人也能直接改。"""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.rules: list[dict] = []
        self._load()

    # ---------- 持久化 ----------

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.rules = [r for r in data
                              if isinstance(r, dict) and r.get("kind") in RULE_KINDS]
        except Exception:
            self.rules = []

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.rules, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------- 增删 ----------

    def add(self, kind: str, value, note: str = "") -> tuple[bool, str]:
        """加一条规则。返回 (是否新增, 说明)。重复的会合并而不是叠加。"""
        if kind not in RULE_KINDS:
            return False, f"未知规则类型 {kind}"
        if kind in ("max_chars", "max_bubbles"):
            try:
                value = int(value)
            except Exception:
                return False, "数值不合法"
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return False, "内容为空"

        # 同类同值视为重复
        for r in self.rules:
            if r.get("kind") == kind and str(r.get("value")) == str(value):
                r["hits"] = r.get("hits", 0)
                return False, "这条已经有了"

        # 配额：24 小时内自动新增太多就拒绝（防止被灌爆）
        if self._added_today() >= MAX_NEW_PER_DAY:
            return False, f"今天自动学的规则已达上限 {MAX_NEW_PER_DAY} 条，明天再学"

        self.rules.append({
            "kind": kind, "value": value, "note": (note or "")[:120],
            "at": int(time.time()), "hits": 0,
        })
        # 超总数：丢最久没命中的
        if len(self.rules) > MAX_RULES:
            self.rules.sort(key=lambda r: (r.get("hits", 0), r.get("at", 0)))
            self.rules = self.rules[-MAX_RULES:]
        self.save()
        return True, "已生效"

    def remove(self, index: int) -> bool:
        """按 /规则 里显示的序号（从 1 开始）删。"""
        if 1 <= index <= len(self.rules):
            self.rules.pop(index - 1)
            self.save()
            return True
        return False

    def clear(self) -> int:
        n = len(self.rules)
        self.rules = []
        self.save()
        return n

    def _added_today(self) -> int:
        day = int(time.time()) // 86400
        return sum(1 for r in self.rules if int(r.get("at", 0)) // 86400 == day)

    # ---------- 执行 ----------

    def apply(self, text: str) -> tuple[str, list[str]]:
        """按规则修一条回复。返回 (修好的文本, 触发了哪些规则的说明)。"""
        if not text:
            return text, []
        fired = []
        out = text

        for r in list(self.rules):
            kind, val = r.get("kind"), r.get("value")
            before = out
            if kind == "no_word" and isinstance(val, str):
                # 删掉这个词连同它后面的逗号/顿号
                out = re.sub(rf"{re.escape(val)}[，,、]?", "", out)
            elif kind == "no_question":
                out = self._strip_trailing_question(out)
            elif kind == "no_open_ended":
                out = self._strip_open_ended(out)
            elif kind == "max_chars":
                try:
                    limit = int(val)
                    if len(out) > limit:
                        # 在 limit 附近找最后一个断点，别切碎词
                        cut = out[:limit]
                        for sep in ("，", "、", " ", "—"):
                            pos = cut.rfind(sep)
                            if pos > limit * 0.6:
                                cut = cut[:pos]
                                break
                        out = cut.rstrip("，,、 ")
                except Exception:
                    pass
            elif kind == "max_bubbles":
                pass   # 条数由发送层控制，这里只记录（见 humantype.split_like_human）

            if out != before:
                r["hits"] = r.get("hits", 0) + 1
                fired.append(f"{RULE_KINDS.get(kind, kind)}：{val}")
        if fired:
            self.save()
        # 收拾规则删完留下的痕迹
        out = re.sub(r"[ \t]{2,}", " ", out).strip("，,、 \n")
        return out, fired

    @staticmethod
    def _strip_trailing_question(text: str) -> str:
        """去掉结尾那句反问。

        真人不会每条回复都反问一句；这是模型最顽固的坏习惯之一。
        只处理**最后一句**，而且是短句（真反问通常很短），避免误删正事。
        """
        parts = re.split(r"(?<=[。！？!?\n])", text)
        parts = [p for p in parts if p.strip()]
        if not parts:
            return text
        last = parts[-1]
        # 反问的标志：疑问句式 + 短句。除了"要不要/是不是"这类，
        # 「还有别的吗」「还要吗」这种也算——它们同样是没话找话的收尾。
        askish = re.search(
            r"(要不要|需不需要|用不用|是不是|对不对|好吗|行吗|怎样|"
            r"还有(别的|其他|什么)|还要|需要(我|再)|可以吗|行不行)", last)
        endswith_q = last.rstrip().endswith(("?", "？", "吗", "呢"))
        if askish and (len(last) <= 24 or endswith_q):
            parts = parts[:-1]
        return "".join(parts).strip()

    @staticmethod
    def _strip_open_ended(text: str) -> str:
        """去掉「要不要我顺手…」「需要我…吗」这类后续提议。

        只删最后一句、且是短句，避免把正文里的"需要"误伤。
        """
        parts = re.split(r"(?<=[。！？!?\n])", text)
        parts = [p for p in parts if p.strip()]
        if not parts:
            return text
        last = parts[-1]
        if re.search(r"(要不要我|需要我|用不用我|要我帮|我可以帮你|随时找我)", last) \
                and len(last) <= 30:
            parts = parts[:-1]
        return "".join(parts).strip()


# ---- 从一句话里认出「这是在纠正我」 ----

# 显式纠正：这类句式基本可以确定是在改行为，允许自动变成规则
EXPLICIT = re.compile(
    # 「下次/以后 + 别/不要」，中间允许插几个字（"下次**把这句话**省了"）
    r"(下次|以后|之后)[^，。！？\n]{0,8}(别|不要|不用|省|删|去掉)|"
    r"(别|不要|不用)再?(加|说|问|写|发|带|提|结尾|最后)|"
    # 「我说的是/我指的是」——纠正在前面没被理解
    r"我说的是|我指的是|不是这个意思|"
    # 「我说把…省了」——重复一遍自己的纠正（他已经说过一次了）
    r"(我说|我讲的|我刚说)[^，。！？\n]{0,6}(省|删|去掉|别|不要)|"
    # 直接的形态批评
    r"(太|有点)(啰嗦|长|多|烦)|"
    # 「你每次都…」「老是…」——抱怨式纠正
    r"(每次|老是|总是)(都)?[^，。！？\n]{0,6}(问|说|加|发)")

# 弱信号：可能是纠正，也可能是随口一句。只记偏好，不生成规则
SOFT = re.compile(
    r"(不喜欢|不爱|讨厌|受不了|烦死|"
    r"少(说|问|发)|简洁|简短|直接说|说重点|"
    r"不用(专门)?(告诉|说))")

# 明显不是纠正的（命令、测试、闲聊里的正常话题）
NOT_CORRECTION = re.compile(
    r"^[/!]|桥可用|^测试|^你好$|^在[吗呢]?$")


def looks_like_correction(text: str) -> tuple[bool, str]:
    """返回 (像不像在纠正, 'explicit' / 'soft' / '')。"""
    t = (text or "").strip()
    if len(t) < 3 or NOT_CORRECTION.search(t):
        return False, ""
    if EXPLICIT.search(t):
        return True, "explicit"
    if SOFT.search(t):
        return True, "soft"
    return False, ""


def infer_rule(text: str) -> dict | None:
    """把一句显式纠正翻译成一条规则。认不出来返回 None。

    这里只做**能机械判定**的映射，宁缺勿滥：认不准就返回 None，
    让这句话落回 A 类偏好（写进记忆），而不是造一条错规则。
    """
    t = (text or "").strip()

    # 「省了/别加/去掉」——真人最常说的形态。他说的是我刚才多说的那一句，
    # 按经验那多半是「要不要我顺手…」这类后续提议，所以映射到 no_open_ended。
    if re.search(r"(省了|省掉|删了|去掉|别加|别带|别提|不说)", t):
        return {"kind": "no_open_ended", "value": True,
                "note": "他说过别加那句收尾"}

    # 「别问」「每次都要问」→ 不要反问
    if re.search(r"(别|不要|不用).{0,6}(问|反问)", t) \
            or re.search(r"(每次|老是|总是).{0,6}(问|说)", t):
        return {"kind": "no_question", "value": True,
                "note": "他说过别再反问"}

    # 「太长了」「短一点」→ 字数上限
    m = re.search(r"(\d{1,3})\s*字(以内|之内|以下)", t)
    if m:
        return {"kind": "max_chars", "value": int(m.group(1)),
                "note": "他要求过字数上限"}
    if re.search(r"(太|有点)(长|啰嗦|多)", t):
        return {"kind": "max_chars", "value": 60,
                "note": "他说过太长，默认收到 60 字"}

    # 「别一次发好几条」→ 只发一条
    if re.search(r"(别|不要).{0,8}(几条|多條|多条|好几条|分开)", t):
        return {"kind": "max_bubbles", "value": 1,
                "note": "他要求过只发一条"}

    return None
