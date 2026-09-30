# -*- coding: utf-8 -*-
"""直接测提示词对"英文行"的取舍：简单钩子该保留，难句子该翻译。

不走文件，直接调 Translator——因为库里没有难英文的样本，只能构造。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import os
import sys

from lyric_tag_translator.translate import TranslateOptions, Translator

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LINES = [
    "夜の風に揺れて",                                       # 日文，必译
    "I love you",                                          # 简单钩子，应保留
    "君の名前を呼ぶ",                                       # 日文，必译
    # 难英文：俚语 + 文化梗
    "He got ghosted after cuffing season, go figure",
    # 难英文：从句嵌套
    "What she never told him was that the version of herself he fell for "
    "had already stopped existing",
    # 简单钩子
    "Please tell me your \"Sweet soul\"",
    # 难英文：习语
    "You can't have your cake and eat it too, babe",
    "さよなら",
]

key = os.environ.get("DEEPSEEK_API_KEY")
if not key:
    print("跳过：没有设置 DEEPSEEK_API_KEY。")
    print("（这个自检要真调一次 API 验证英文行的取舍，需要 key。）")
    raise SystemExit(0)

opts = TranslateOptions(style="poetic", explicit="soften")
tr = Translator(key, opts)
res = tr.translate(LINES, "流派：J-Pop；年份：2024；艺人：某日系艺人；专辑：Single")

print(f"寄存器={res.register}  源语言={res.src_lang}  API×{res.api_calls}\n")
print(f"{'原文':<52} {'译文':<40} 判定")
print("-" * 108)
for src, dst in zip(LINES, res.lines):
    kept = dst.strip() == src.strip()
    verdict = "保留英文" if kept else "已翻译"
    print(f"{src[:50]:<52} {dst[:38]:<40} {verdict}")
