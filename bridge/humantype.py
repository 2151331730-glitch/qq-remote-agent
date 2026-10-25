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


def split_like_human(text: str, max_parts: int = 2, soft_limit: int = 60,
                     join_below: int = 26):
    """把回复切成真人会发的那种 1~2 条。

    实测真人 84% 只发一条、13% 发两条、只有 2% 发三条以上。所以：

    1. **短内容一律合成一条**：总长不超过 `join_below`（约 36 字，两倍中位长度）
       时，即使模型自己分了段也用逗号连起来。真人不会为了"看电脑呢"和"咋了"
       这两句话专门发两条。
    2. **最多 2 条**。三条以上是明显的机器特征，多出来的并进第二条。
    3. 只有单条真的超过 `soft_limit`（约 60 字，已到真人 p100 之上）才切。
    """
    raw = (text or "").strip()
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
