# -*- coding: utf-8 -*-
"""看图层（vision.py）的纯函数单测。

只测不花钱、不碰系统的那部分：OCR 的正文收拾、格式嗅探、给提示词的那几行文字。
真正的 OCR / 视觉模型调用不在单测里跑 —— 一个要 Windows 语言包，一个要代理和钱。
"""
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(BRIDGE))

import vision  # noqa: E402


# ---------------- 后缀嗅探 ----------------

def test_sniff_gif():
    assert vision.sniff_ext(b"GIF89a....") == ".gif"


def test_sniff_png_jpg_webp():
    assert vision.sniff_ext(b"\x89PNG\r\n\x1a\n") == ".png"
    assert vision.sniff_ext(b"\xff\xd8\xff\xe0") == ".jpg"
    assert vision.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"


def test_sniff_unknown_falls_back_to_jpg():
    assert vision.sniff_ext(b"") == ".jpg"
    assert vision.sniff_ext(b"????") == ".jpg"


def test_save_bytes_picks_real_extension(tmp_path):
    # 表情存成 .jpg 就不动了：后缀必须按文件头取，不能按下载链接猜
    p = vision.save_bytes(b"GIF89a....", tmp_path)
    assert p.suffix == ".gif" and p.is_file()
    assert p.read_bytes() == b"GIF89a...."


# ---------------- OCR 正文收拾 ----------------

def test_tidy_cjk_removes_per_char_spaces():
    # Windows 中文 OCR 的输出是「远 程 任 务 失 败」，字间空格必须去掉
    assert vision.tidy_text("远 程 任 务 失 败") == "远程任务失败"


def test_tidy_english_keeps_word_spaces():
    assert vision.tidy_text("hello   world") == "hello world"


def test_tidy_mixed_line_keeps_english_words():
    out = vision.tidy_text("ERROR 连接 失败 retry")
    # 中文占多数的行整行收紧；英文单词之间的空格不能丢
    assert "ERROR" in out and "retry" in out


def test_tidy_drops_blank_lines_and_trims():
    assert vision.tidy_text("\n  你好  \n\n") == "你好"


def test_tidy_truncates():
    out = vision.tidy_text("字" * 50, max_chars=10)
    assert out.startswith("字" * 10) and "太长" in out


# ---------------- 提示词里的那几行 ----------------

def test_note_text_ocr():
    assert vision.note_text("text", "空调 26 度") == "[图片里的文字]\n空调 26 度"


def test_note_text_vision():
    assert vision.note_text("see", "一只橘猫趴在键盘上") == "[图片内容] 一只橘猫趴在键盘上"


def test_note_text_nothing_keeps_path():
    out = vision.note_text("", "", r"C:\tmp\a.jpg")
    assert "没认出内容" in out and r"C:\tmp\a.jpg" in out
