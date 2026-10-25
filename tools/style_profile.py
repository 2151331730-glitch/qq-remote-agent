# -*- coding: utf-8 -*-
r"""从聊天记录里提炼"某个人怎么说话"，输出给 LLM 用的风格档案草稿。

用法：
    python tools\style_profile.py 语料.txt --speaker 123456789
    python tools\style_profile.py bridge\transcripts\owner.jsonl --jsonl

支持两种输入：
  1) .jsonl —— bridge 自己的记录，每行 {"uid":..,"name":..,"text":..}
  2) .txt   —— QQ 聊天记录导出的纯文本，按"时间 昵称(号码)"行分块

只做统计与抽样，不改动原文件，不联网。
"""
import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

# 中文里高频但不携带风格的词，统计口头禅时先剔除
STOP = set("""
的 了 是 我 你 他 她 它 在 有 和 就 都 也 不 吗 吧 呢 啊 哦 嗯 哈 呀 嘛
这 那 个 们 说 要 会 得 着 过 很 还 被 把 给 让 对 从 到 与 及 或 但
一 二 三 四 五 六 七 八 九 十 什么 怎么 为什么 可以 一个 没有 就是
""".split())


def load_jsonl(path, speaker=None):
    """bridge transcripts：按 uid 或 name 过滤出目标说话人。"""
    out, other = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = (d.get("text") or "").strip()
        if not text:
            continue
        uid = str(d.get("uid", ""))
        name = str(d.get("name", ""))
        if speaker and speaker in (uid, name):
            out.append(text)
        else:
            other.append(text)
    return out, other


def load_txt(path, speaker=None):
    """QQ 导出的纯文本。块首形如「2026-10-25 12:00:00 昵称(123456)」。"""
    head = re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}[^\n]*$")
    header = re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}[ \t]+\d{1,2}:\d{2}(:\d{2})?\s+(.+?)\s*(?:\((\d{5,12})\))?\s*$")
    out, other = [], []
    cur_who, buf = None, []

    def flush():
        if cur_who is None or not buf:
            return
        text = "\n".join(buf).strip()
        if not text:
            return
        if speaker and speaker in cur_who:
            out.append(text)
        else:
            other.append(text)

    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        m = header.match(line)
        if m:
            flush()
            cur_who = m.group(2) + (m.group(3) or "")
            buf = []
        elif head.match(line):
            # 没有昵称的日期分隔行
            flush()
            cur_who, buf = cur_who, []
        else:
            buf.append(line)
    flush()
    return out, other


def basic_stats(msgs):
    lens = [len(m) for m in msgs]
    if not lens:
        return {}
    return {
        "条数": len(msgs),
        "平均字数": round(statistics.mean(lens), 1),
        "中位字数": statistics.median(lens),
        "P25": sorted(lens)[len(lens) // 4],
        "P75": sorted(lens)[(len(lens) * 3) // 4],
        "最长": max(lens),
        "一句话以内(<=15字)占比": f"{sum(1 for x in lens if x <= 15) / len(lens):.0%}",
    }


def punctuation_stats(msgs):
    joined = "\n".join(msgs)
    total = max(len(joined), 1)
    marks = {
        "句号。": joined.count("。"),
        "问号？": joined.count("？") + joined.count("?"),
        "感叹号！": joined.count("！") + joined.count("!"),
        "逗号，": joined.count("，") + joined.count(","),
        "省略号…": joined.count("…") + joined.count("..."),
        "波浪号~": joined.count("~") + joined.count("～"),
        "书名号": joined.count("《"),
        "括号（）": joined.count("（") + joined.count("("),
    }
    per100 = {k: round(v / total * 100, 2) for k, v in marks.items()}
    return per100


def emoji_stats(msgs):
    pattern = re.compile(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
    with_emoji = sum(1 for m in msgs if pattern.search(m))
    cq = sum(1 for m in msgs if "[CQ:" in m or "[表情]" in m)
    return {
        "含 emoji 的条数占比": f"{with_emoji / max(len(msgs), 1):.0%}",
        "含 QQ 表情码的条数": cq,
    }


def catchphrases(msgs, top_n=25):
    """按 2~4 字切分统计高频片段，粗粒度找口头禅。"""
    counter = Counter()
    for m in msgs:
        clean = re.sub(r"[^一-龥a-zA-Z]+", " ", m)
        for seg in clean.split():
            if len(seg) < 2:
                continue
            for n in (2, 3, 4):
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    if g in STOP:
                        continue
                    if all(c in STOP for c in g):
                        continue
                    counter[g] += 1
    # 只保留出现 >=2 次的，去掉被更长片段覆盖的低频短词
    common = Counter({g: c for g, c in counter.items() if c >= 2})
    kept = []
    for g, c in common.most_common(200):
        if any(g != k and g in k and common[k] >= c for k in common):
            continue
        kept.append((g, c))
        if len(kept) >= top_n:
            break
    return kept


def sample_short(msgs, n=15, max_len=40):
    """挑一批短句做 few-shot 样例：短、去重、优先有标点的。"""
    seen, picked = set(), []
    for m in sorted(msgs, key=len):
        s = " ".join(m.split())
        if not s or len(s) > max_len or s in seen:
            continue
        seen.add(s)
        picked.append(s)
        if len(picked) >= n:
            break
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--speaker", default=None, help="uid 或昵称")
    ap.add_argument("--jsonl", action="store_true", help="bridge transcripts 格式")
    ap.add_argument("--out", default=None, help="把草稿写到文件")
    args = ap.parse_args()

    if args.jsonl or str(args.path).lower().endswith(".jsonl"):
        mine, others = load_jsonl(args.path, args.speaker)
    else:
        mine, others = load_txt(args.path, args.speaker)

    if not mine:
        raise SystemExit("没有匹配到任何消息，检查 --speaker 是否正确")

    report = {
        "样本": basic_stats(mine),
        "标点密度（每 100 字出现次数）": punctuation_stats(mine),
        "表情": emoji_stats(mine),
        "高频片段": catchphrases(mine),
        "短句样例": sample_short(mine),
        "对照（他人）": basic_stats(others) if others else {},
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n已写入 {args.out}", flush=True)


if __name__ == "__main__":
    main()
