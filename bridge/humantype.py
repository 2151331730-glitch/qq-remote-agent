# -*- coding: utf-8 -*-
"""拟人化：去书面化 + 分段 + 打字节奏。

## 为什么不是一个"风格参数"就够

我把这个 QQ 号里 275 条**真人**发言和机器人自己 57 条发言做了对照统计，
差别不是"语气"，而是几条硬指标：

| 特征 | 真人 | 改造前的机器人 |
|---|---|---|
| 句末打标点 | **0%** | 63% |
| 含句号的消息 | **1%**（4/275） | 大量 |
| 完全不带任何标点 | **68%** | 几乎没有 |
| 每条字数中位 | **11 字** | 25 字 |
| 长度 p90 / 最长 | 23 / 50 | 常常上百字 |
| 一轮发几条 | **84% 只发 1 条** | 几乎总是 2~3 条 |

所以"像真人"不是加几个语气词，而是三件事：
1. **去掉书面标点** —— 句号是最大的破绽，真人聊天几乎不打句号
2. **压短** —— 一句话说完一件事，11~20 字
3. **少分条** —— 大多数时候一条就够了，分条是少数情况

这个模块负责 1 和 3（模型负责 2，靠 `bridge.py` 里 `_direct_system()` 的提示词）。

## 分条的真相

真人连续发言统计（277 段）：发 1 条 233 次（84%），2 条 37 次，3 条 4 次。
所以**默认就该是一条**。只有真的写了很长一段才拆，而且最多拆 2 条 ——
拆成三条以上基本是机器特征。
"""
import random
import re

# 句末断点：优先在这些地方断
_BREAK = re.compile(r"(?<=[。！？!?…~；;])|(?<=\n)")

# 内部状态不该发给用户：这些是程序语言，不是人在说话
_LEAK = re.compile(
    r"^（?任务完成[，,]?\s*没有文字输出）?$|"
    r"^✅|^⚠️\s*执行异常|^❌|"
    r"^（没有回话）$|^（没说出话来）$",
)

# 敷衍式的回应：只有应声、没有内容
# 真人也会说「行」，但后面一定跟一句实质的（对比实测：
# 「行，那我闭嘴，等你放片头」vs 机器人的「行 那就不说了」）
_THIN = re.compile(
    r"^(行|嗯|好|好的|哦|知道|知道了|可以|收到|明白|是|对)"
    r"(\s*(那就不说了|知道了|不说了|了解了|明白了|好吧|了))?[\s，,。.!！~]*$")


def looks_like_leak(text: str) -> bool:
    """判断是不是把内部状态当回复发出去了。"""
    t = (text or "").strip()
    return bool(t) and bool(_LEAK.search(t))


def is_thin(text: str) -> bool:
    """是不是敷衍到没有信息量（只有应声）。"""
    t = (text or "").strip()
    return bool(t) and bool(_THIN.match(t))

# 中文句末标点（要删）与句中停顿（可保留但常被空格替代）
_SENT_END = "。！？!?；;…"
_CLAUSE = "，,、：:"

# 删掉句号后容易连读的地方，补一个空格而不是留着标点
_SPACE_AFTER = re.compile(r"(?<=[^\s])[。！？!?]")


def humanize(text: str, keep_exclaim: bool = True) -> str:
    """把"写作文"改写成"发消息"。

    做的事（都按实测数据定的）：
    - **删句号**：真人 99% 不打句号，这是最刺眼的机器味
    - 感叹号/问号保留但限量（真人 275 条里几乎不用，但偶尔有）
    - 全角逗号换成空格（真人 68% 完全无标点，32% 用逗号；空格更保险）
    - 压掉多余空行、行首行尾空白
    - 太长的话行内不加标点，让它自然变成一条长消息（不硬塞标点）

    注意：**不动内容**，只动标点和空白。缩句是模型的事。
    """
    t = (text or "").strip()
    if not t:
        return ""

    # 1) 句号：句中换成逗号，句末直接删掉
    #    数据依据：真人 275 条里只有 4 条含句号（1%），而含逗号的 87 条（32%）。
    #    所以"知道。之前是会话上限设小了"要变成"知道，之前是会话上限设小了"，
    #    而不是"知道 之前是……"——真人不会用空格断句，空格只出现在英文/数字旁。
    t = re.sub(r"[。]+(?=\s*\n)", "", t)          # 行末句号：删（换行本身是断点）
    t = re.sub(r"[。]+", "，", t)                  # 句中句号：换逗号
    # 2) 英文句号在中文旁边也当句号处理
    t = re.sub(r"(?<=[一-鿿])\.(?=\s*$)", "", t)
    t = re.sub(r"(?<=[一-鿿])\.(?=[一-鿿])", "，", t)
    # 3) 分号 → 逗号；连续省略号 → 删
    t = re.sub(r"[；;]", "，", t)
    t = re.sub(r"…{2,}|\.{3,}", "", t)
    # 4) 连续的逗号压成一个
    t = re.sub(r"[，,]{2,}", "，", t)
    # 5) 感叹号/问号限量：连续多个压成一个
    if keep_exclaim:
        t = re.sub(r"[！!]{2,}", "！", t)
        t = re.sub(r"[？?]{2,}", "？", t)
    else:
        t = re.sub(r"[！!？?]", "，", t)
    # 6) 空白收拾：行内多空格合一，行首尾去空，多余空行合一
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", t)
    # 7) 行首/行末的孤立标点去掉（"我去看看，\n" 这种尾巴逗号）
    #    注意用 [ \t] 而不是 \s —— \s 会把换行也吃掉，把 "\n\n" 压成 "\n"
    t = re.sub(r"[，,、 \t]+(?=\n|$)", "", t)
    t = re.sub(r"(?<=^)[，,、 \t]+", "", t)
    t = re.sub(r"\n[，,、]+", "\n", t)
    return t.strip()


# 链接里自带 ? & # = ，句末断点会把它们当成句子结束，一条链接被劈成两条就点不开了。
# 切之前先换成占位符（私有区字符，正常文本里不会出现），切完再还原。
_URL = re.compile(r"https?://[^\s一-鿿，。！？；：、）】》\"']+")
_URL_PH = re.compile("(\\d+)")


def _protect_urls(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def rep(m):
        urls.append(m.group(0))
        return f"{len(urls) - 1}"

    return _URL.sub(rep, text), urls


def _restore_urls(text: str, urls: list[str]) -> str:
    if not urls:
        return text
    return _URL_PH.sub(lambda m: urls[int(m.group(1))], text)


def split_like_human(text: str, max_parts: int = 2, soft_limit: int = 60,
                     join_below: int = 26):
    """把回复切成真人会发的那种 1~2 条。

    链接先保护起来再切（见 `_protect_urls`）：含 ? 的链接被切开等于把人坑了。
    """
    raw, urls = _protect_urls((text or "").strip())
    parts = _split_parts(raw, max_parts, soft_limit, join_below)
    return [_restore_urls(p, urls) for p in parts]


def _split_parts(raw: str, max_parts: int = 2, soft_limit: int = 60,
                 join_below: int = 26):
    """把回复切成真人会发的那种 1~2 条。

    实测真人 84% 只发一条、13% 发两条、只有 2% 发三条以上。所以：

    1. **短内容一律合成一条**：总长不超过 `join_below`（约 36 字，两倍中位长度）
       时，即使模型自己分了段也用逗号连起来。真人不会为了"看电脑呢"和"咋了"
       这两句话专门发两条。
    2. **最多 2 条**。三条以上是明显的机器特征，多出来的并进第二条。
    3. 只有单条真的超过 `soft_limit`（约 60 字，已到真人 p100 之上）才切。
    """
    if not raw:
        return []

    if "\n\n" in raw:
        segs = [s.strip() for s in re.split(r"\n\s*\n", raw) if s.strip()]
        total = sum(len(s) for s in segs)
        if len(segs) == 1:
            return segs
        # 内容本来就不长：合成一条，用逗号连（真人的做法）
        if total <= join_below:
            return ["，".join(s.rstrip("，,、 ") for s in segs)]
        if len(segs) <= max_parts:
            return segs
        return segs[:max_parts - 1] + ["，".join(segs[max_parts - 1:])]

    flat = " ".join(raw.split())
    if len(flat) <= soft_limit:
        return [flat]

    # 太长：在句末断点处切成两半，尽量各占一半
    chunks = [c for c in (p.strip() for p in _BREAK.split(flat)) if c]
    if len(chunks) <= 1:
        mid = len(flat) // 2
        return [flat[:mid].strip(), flat[mid:].strip()]

    half = len(flat) / 2
    best, acc, idx = None, 0, 0
    for i, c in enumerate(chunks[:-1]):
        acc += len(c)
        d = abs(acc - half)
        if best is None or d < best:
            best, idx = d, i + 1
    a, b = "".join(chunks[:idx]).strip(), "".join(chunks[idx:]).strip()
    return [x for x in (a, b) if x]


# ---------------- 防复读：看它最近说了什么 ----------------
# 模型的坏习惯是「模仿自己的历史」：连着三条都用「行」开头，或者每条都以反问收尾。
# 这些提示词管不住，得把最近几条拿出来算一算，动态塞回系统提示。

_CLAUSE_SEP = re.compile(r"[，,。！？!?~…\s、：:；;]")
_PUNCT = re.compile(r"[，,。！？!?~…\s、：:；;　]+")


def _said(history, n: int = 5) -> list[str]:
    out = [str(m.get("content") or "") for m in (history or [])
           if str(m.get("role") or "") == "assistant"]
    return [x for x in out if x.strip()][-n:]


def opener(text: str) -> str:
    """第一条小句的头 4 个字，用来判断开头是不是在复读。"""
    t = _PUNCT.sub(" ", (text or "").strip()).strip()
    if not t:
        return ""
    return _CLAUSE_SEP.split(t, 1)[0][:4]


def is_question(text: str) -> bool:
    """中文口语里问句常常不带问号（「咋了」「那算啥」），两种都要认。"""
    t = (text or "").strip().rstrip("～~。.，, ")
    if not t:
        return False
    if t.endswith("？") or t.endswith("?"):
        return True
    if t.endswith(("吗", "呢")):
        return True
    if any(w in t for w in ("咋了", "咋回事", "是不是")):
        return True
    return t[:3].startswith(("怎么", "为什么", "啥", "哪", "谁", "咋"))


def _norm(text: str) -> str:
    return _PUNCT.sub("", text or "")


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def is_repeat(reply: str, prev: list[str], threshold: float = 0.65) -> bool:
    """这句是不是跟刚才说过的某句几乎一样（复读机）。"""
    r = _norm(reply)
    if not r:
        return False
    for p in prev[-3:]:
        pn = _norm(p)
        if pn and r == pn:          # 一字不差地复读，短句也算
            return True
    if len(r) < 8:
        return False
    rb = _bigrams(reply)
    if not rb:
        return False
    for p in prev[-3:]:
        pn = _norm(p)
        if not pn:
            continue
        if r == pn:
            return True
        pb = _bigrams(p)
        if not pb:
            continue
        sim = len(rb & pb) / len(rb | pb)
        if sim >= threshold:
            return True
    return False


def style_note(history, window: int = 3) -> str:
    """根据最近几条，给这次生成加一句"别这样"的提醒。没有毛病就返回空串。"""
    said = _said(history, window + 1)
    if not said:
        return ""
    recent = said[-window:]
    hints = []

    ops = [opener(x) for x in recent]
    if len(ops) >= 2 and ops and all(o and o == ops[-1] for o in ops):
        hints.append(f"开头别再是「{ops[-1]}」，前面连着用了")
    if len(recent) >= 2 and all(is_question(x) for x in recent):
        hints.append("你连着几条都在反问，这条别提问，直接给反应或看法")
    lens = [len(_norm(x)) for x in recent]
    if lens and sum(lens) / len(lens) > 24:
        hints.append("最近几条偏长，这条压到 15 字以内")
    if any(w in "".join(recent[-2:]) for w in ("模型", "AI", "助手", "提示词", "人设")):
        hints.append("别再聊你自己是什么模型或程序，也别为上一句道歉")
    if not hints:
        return ""
    return "（这次注意：" + "；".join(hints) + "。自然接下句，不用解释为什么）"


def typing_delay(before_chars: int, first: bool = False) -> float:
    """模拟"读消息 + 打字"的时间。

    实测真人换人对话的响应间隔：群聊中位 7.4 秒，私聊更慢（中位 31 秒，
    但那是他在忙别的）。作为"员工"不能真的等半分钟，也不能秒回 ——
    秒回是最明显的机器特征。

    目标：第一条 1.5~4 秒（体感"在打字"而不是"卡住"），
    后面的条 0.6~2 秒（接着打下一个意思）。
    """
    base = 1.1 if first else 0.45
    per_char = random.uniform(0.030, 0.065)      # 每字 30~65ms
    thinking = random.uniform(0.3, 1.6)          # 想一下说什么
    if first:
        # 偶尔像"正好在看手机"秒回，多数时候正常打字
        thinking *= random.choice([0.5, 0.8, 1.0, 1.0, 1.3])
    d = base + thinking + before_chars * per_char
    return max(0.9, min(d, 6.0))


def compose_timeline(parts: list[str]) -> list[float]:
    """给每一条算出"该等多久再发"。返回与 parts 等长的延迟列表（秒）。"""
    delays = []
    for i, p in enumerate(parts):
        if i == 0:
            delays.append(typing_delay(len(p), first=True))
        else:
            d = typing_delay(len(p) * 0.7)
            if random.random() < 0.2:
                d += random.uniform(0.8, 2.0)   # 偶尔"打完又补一句"
            delays.append(max(0.4, d))
    return delays
