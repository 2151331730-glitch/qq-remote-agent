# -*- coding: utf-8 -*-
r"""从真实聊天记录里学"人味"，生成 style_learned.md 给闲聊通道当参照。

## 为什么要有这个脚本

`humantype.py` 的规则来自一次性的手工统计，人格包里那些"像真人"的写法是手写的。
两者都会过时：聊天在变，人也在变。这个脚本把同一件事做成**可重复跑**的：

    真实记录 → 指标 + 真实原话样本 → 记忆\style_learned.md → 注入系统提示

它是只读的：只读 transcripts，只写那一个 md 文件。

## 判据从哪来

- 只统计**真人**：机器人自己（名字"我"/uid 是 bot）、命令（`/` `!`）、测试残留、超长粘贴都不算。
- 一轮几条按**发言人**拆：同一个人 3 分钟内连发算一轮，不同人插话就断开。
- 样本挑"像人"的：2~26 字、不带 @ 和链接、不是纯数字。

用法：

    python tools\learn_style.py            # 生成/更新
    python tools\learn_style.py --show     # 只看统计，不写文件
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(r"C:\Users\win11\Desktop\记忆\style_learned.md")

# 转录目录：一处是人格包旁边，一处是 bridge 自己的
TRANSCRIPT_DIRS = [
    Path(r"C:\Users\win11\Desktop\记忆\transcripts"),
    REPO / "bridge" / "transcripts",
]

BOT_UIDS = {3089461079, 2813436988}
BOT_NAMES = {"我", "助手", "Sesame", "sesame"}
# 测试残留的 uid，别当成真人
SKIP_UIDS = {12345}
CMD = re.compile(r"^[/!]")
CQ = re.compile(r"\[CQ:[^\]]+\]")
# 别人的口癖：跟着学会露馅
TIC_WORDS = ("喵", "嘞")
# 聊机器人自己 / AI 圈子的句子：样本里留一条，模型就会学着开口谈"我是不是 AI"
META_WORDS = ("机器人", "人格", "提示词", "模型", "重启", "接口", "快照", "封号", "群主",
              "聊天记录", "风格", "翻车", "台词", "演过", "小号", "唐",
              "接话", "回话", "回你", "认全",
              "token", "api", "API", "claude", "Claude", "opus", "Opus", "克劳德",
              "deepseek", "DeepSeek", "sesame", "Sesame", "GitHub", "github")
PARTICLES = "咯勒诶吧呗啊呀嘛"


# ---------------- 读 ----------------

def load_rows():
    seen, rows = set(), []
    for d in TRANSCRIPT_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.jsonl")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                key = (round(float(obj.get("ts") or 0), 1), obj.get("uid"), obj.get("text"))
                if key in seen:
                    continue
                seen.add(key)
                obj["_src"] = p.stem
                rows.append(obj)
    rows.sort(key=lambda d: float(d.get("ts") or 0))
    return rows


def is_bot(d):
    return str(d.get("name") or "") in BOT_NAMES or int(d.get("uid") or 0) in BOT_UIDS


def usable(d, lo=2, hi=26, meta=False):
    """能不能当成"人味样本"。返回清洗后的文本，不合格返回空串。

    meta=True 时额外滤掉"聊机器人自己"的句子——那是给样本用的严格档，
    统计指标仍用宽档，否则样本量会被砍得太少。
    """
    t = CQ.sub("", d.get("text") or "").strip()
    if not t or CMD.match(t):
        return ""
    if int(d.get("uid") or 0) in SKIP_UIDS or is_bot(d):
        return ""
    if not (lo <= len(t) <= hi):
        return ""
    if "@" in t or "http" in t.lower() or re.search(r"\d{6,}", t):
        return ""
    if any(w in t for w in TIC_WORDS):
        return ""
    if meta and any(w in t for w in META_WORDS):
        return ""
    return t


# ---------------- 算 ----------------

def stats(rows):
    human = [(d, usable(d, 2, 200)) for d in rows]
    human = [(d, t) for d, t in human if t]

    # 一轮几条：按发言人
    runs, cur_uid, cur_n, prev = [], None, 0, None
    for d, _ in human:
        uid, ts = d.get("uid"), float(d.get("ts") or 0)
        if uid == cur_uid and prev is not None and ts - prev <= 180:
            cur_n += 1
        else:
            if cur_n:
                runs.append(cur_n)
            cur_uid, cur_n = uid, 1
        prev = ts
    if cur_n:
        runs.append(cur_n)

    texts = [t for _, t in human]
    lens = sorted(len(t) for t in texts)
    n = len(texts) or 1

    def share(pred):
        return sum(1 for t in texts if pred(t)) / n

    q = Counter(runs)
    return {
        "msgs": len(texts),
        "runs": len(runs),
        "run_share": {k: q[k] / len(runs) for k in (1, 2, 3)} if runs else {},
        "median": lens[len(lens) // 2] if lens else 0,
        "p90": lens[min(len(lens) - 1, int(len(lens) * 0.9))] if lens else 0,
        "period": share(lambda t: "。" in t),
        "end_punct": share(lambda t: t[-1] in "。！？!?…~～"),
        "any_punct": share(lambda t: bool(re.search(r"[，,。！？!?；;：:]", t))),
        "question": share(lambda t: t.rstrip("~～。 ")[-1:] in "？?"
                          or t.rstrip().endswith(("吗", "呢"))),
        "repeat3": share(lambda t: bool(re.search(r"(.)\1\1", t))),
    }


def vocab(rows):
    """真人用得多、机器人用得少的词。机器人那边按名字/uid 认。"""
    hi, lo = Counter(), Counter()
    for d in rows:
        t = CQ.sub("", d.get("text") or "").strip()
        if not t or CMD.match(t):
            continue
        bucket = lo if is_bot(d) else hi
        bucket.update({w for w in ("嗯", "行", "这边", "呢", "确实", "不过", "已经", "需要",
                                   "收到", "好的", "可以", "咯", "诶", "呗", "啊", "吧",
                                   "草", "呀", "勒") if w in t})
    return hi, lo


def pick_examples(rows, cap_pairs=10, cap_short=18):
    """挑真实对话样本：①一问一答 ②单独说的短句。

    优先群聊：私聊里大半是主人在调试这个机器人（"你这么回太唐了"），
    学多了会让机器人开口就聊"回复/人格"；群里才是几个人正常闲聊。
    """
    pairs, seen = [], set()
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if a.get("uid") == b.get("uid"):
            continue
        ta, tb = usable(a, 2, 30, meta=True), usable(b, 2, 24, meta=True)
        if not ta or not tb:
            continue
        gap = float(b.get("ts") or 0) - float(a.get("ts") or 0)
        if not (0 < gap <= 45) or ta[:8] in seen:
            continue
        seen.add(ta[:8])
        src = str(a.get("_src") or "")
        rank = (0 if src.startswith("group") else 1,
                -sum(w in tb for w in PARTICLES),
                tb.endswith("？"))
        pairs.append((rank, ta, tb))
    pairs.sort(key=lambda p: p[0])

    shorts, seen_t = [], set()
    for d in rows:
        t = usable(d, 2, 20, meta=True)
        if not t or t[:8] in seen_t:
            continue
        if not any(w in t for w in PARTICLES + "不吗吧"):
            continue
        seen_t.add(t[:8])
        shorts.append((0 if str(d.get("_src") or "").startswith("group") else 1, t))
    shorts.sort(key=lambda p: p[0])
    # 群聊优先，但别把私聊样本全丢掉：各取一部分
    half = cap_short // 2
    group_t = [t for k, t in shorts if k == 0][:half]
    rest = [t for k, t in shorts if k == 0 or True][:cap_short]
    picked = group_t + [t for t in rest if t not in group_t]
    return [(q, a) for _, q, a in pairs[:cap_pairs]], picked[:cap_short]


# ---------------- 写 ----------------

def render(st, hi, lo, pairs, shorts):
    def pct(x):
        return f"{x:.0%}"

    rs = st["run_share"]
    lines = [
        "# 语料实测（自动生成，别手改）",
        "",
        f"> 由 `tools\\learn_style.py` 从 {st['msgs']} 条真实发言（{st['runs']} 轮）算出，"
        f"{time.strftime('%Y-%m-%d %H:%M')} 更新。",
        "> 这是**节奏参照**，不是台词本：学长度、选词、接话方式，不要照抄内容，也不要自称是这些人。",
        "",
        "## 硬指标",
        "",
        f"- 一轮发几条：1 条 {pct(rs.get(1, 0))}、2 条 {pct(rs.get(2, 0))}、3 条 {pct(rs.get(3, 0))}"
        " → **默认就发一条**，想分条是少数情况",
        f"- 字数：中位 {st['median']}，p90 {st['p90']} → 超过 {max(25, st['p90'] + 4)} 字就不像人在聊天",
        f"- 句号：{pct(st['period'])} 的消息有句号 → **别打句号**",
        f"- 句末标点：{pct(st['end_punct'])}；任何标点：{pct(st['any_punct'])}",
        f"- 问句：{pct(st['question'])} → 不是每句都要反问",
        f"- 叠字（哈哈哈）：{pct(st['repeat3'])} → 真人很少这么打",
        "",
        "## 选词",
        "",
    ]
    common = [w for w in ("咯", "诶", "呗", "啊", "吧", "勒", "草", "呀") if hi.get(w)]
    if common:
        lines.append("- 真人爱用：" + " ".join(common))
    avoid = [w for w in ("嗯", "行", "这边", "呢", "确实", "不过") if lo.get(w)]
    if avoid:
        lines.append("- 机器人说得多、真人几乎不说的：" + " ".join(avoid)
                     + "（开口别用「嗯，」「行，」「不过，」起头）")
    lines += ["", "## 真实接话（学节奏：一句话，短，不解释）", "",
              "> 这些多半是主人调试机器人时说的，**只学长度和语气**。"
              "「回复 / 接话 / 机器人 / 学说话」这类话题本身别主动提。", ""]
    for q, a in pairs:
        lines += [f"说：{q}", f"回：{a}", ""]
    if shorts:
        lines += ["## 随口一句的样子", ""]
        lines += [f"- {t}" for t in shorts]
        lines += [""]
    lines += [
        "## 最后三条",
        "",
        "1. 别跟着任何人学口癖（群里有人爱加「喵」，跟着加就露馅）。",
        "2. 别用「不过」起第二段——那是机器人最爱的转折。",
        "3. 一条只放一个意思，剩下的留到下一句。",
        "",
        "样本里的人名、外号、具体事情都不要拿来当自己的经历。",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    rows = load_rows()
    if not rows:
        print("没有找到聊天记录")
        return 1
    st = stats(rows)
    hi, lo = vocab(rows)
    pairs, shorts = pick_examples(rows)
    text = render(st, hi, lo, pairs, shorts)

    print(f"真人发言 {st['msgs']} 条 / {st['runs']} 轮")
    print("一轮几条：" + "  ".join(f"{k}条 {v:.0%}" for k, v in st["run_share"].items()))
    print(f"字数 中位 {st['median']} p90 {st['p90']}；有句号 {st['period']:.0%}；问句 {st['question']:.0%}")
    print(f"样本：接话 {len(pairs)} 对，短句 {len(shorts)} 条")
    if args.show:
        print("\n" + text)
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"已写入 {OUT}（{len(text)} 字）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
