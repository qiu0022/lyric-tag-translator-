# -*- coding: utf-8 -*-
"""列出被判为"疑似已翻译"的歌，并打印前几行让人工判断是真双语还是误判。

误判会让一首没翻的歌被静默跳过，所以这个检查必须做。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import sys
from pathlib import Path

from lyric_i18n import clean
from lyric_i18n.identity import read_tags
from lyric_i18n.lyrics import iter_audio, read_lyrics

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

root = Path(sys.argv[1])
show = int(sys.argv[2]) if len(sys.argv) > 2 else 10

hits = []
for f in iter_audio(root):
    text = read_lyrics(f)
    if text is None:
        continue
    tags = read_tags(f)
    doc = clean.parse_tag_text(text)
    if doc.translatable_count == 0:
        continue
    if clean.looks_translated(doc):
        hits.append((tags, text, doc))

print(f"共 {len(hits)} 首被判为【疑似已翻译】\n")
for tags, text, doc in hits:
    print("=" * 74)
    print(f"{tags.display()}    语言={doc.primary_lang}  非空={len([l for l in doc.lines if l.text.strip()])} 行")
    for ln in [l for l in doc.lines if l.text.strip()][:show]:
        print(f"    {ln.kind:<8} {ln.text[:64]}")
    print()
