# -*- coding: utf-8 -*-
"""在日文歌里找"整行英文"，用来验证"难英文要翻译"这条规则。

按英文单词数排序——词越多、句子越完整，越可能是"需要翻译的难句子"。
只读。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import re
import sys
from pathlib import Path

from lyric_tag_translator import clean
from lyric_tag_translator.identity import read_tags
from lyric_tag_translator.lyrics import iter_audio, read_lyrics

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{1,}")


def main() -> None:
    root = Path(sys.argv[1])
    rows = []
    for f in iter_audio(root):
        text = read_lyrics(f)
        if text is None:
            continue
        tags = read_tags(f)
        doc = clean.parse_tag_text(text)
        if doc.primary_lang != "ja":
            continue
        for ln in doc.lines:
            if ln.kind != "lyric":
                continue
            # 只挑没有任何假名/汉字的行 —— 即整行英文
            if clean.RE_KANA.search(ln.text) or clean.RE_HAN.search(ln.text):
                continue
            words = WORD.findall(ln.text)
            if len(words) >= 4:                 # 至少 4 个单词才算"一句"
                rows.append((len(words), tags.display(), ln.text))

    rows.sort(key=lambda r: -r[0])
    print(f"日文歌里的整行英文（≥4 词）：{len(rows)} 行\n")
    for n, song, text in rows[:40]:
        print(f"  [{n:>2}词] {song[:28]:<30} {text}")


if __name__ == "__main__":
    main()
