# -*- coding: utf-8 -*-
"""clean 层在全库上的分类报告。

三件事：
  1. 每首歌的分类统计（待译 / 唯一 / 省下 / 拟声）
  2. 不可译清单 —— 人工扫一遍有没有误判（误判 = 真歌词被静默跳过）
  3. 重复率最高的行 —— 验证去重到底省了多少

只读。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import sys
from collections import Counter
from pathlib import Path

from lyric_tag_translator import clean
from lyric_tag_translator.identity import read_tags
from lyric_tag_translator.lyrics import iter_audio, read_lyrics

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> None:
    root = Path(sys.argv[1])
    verbose = "--all" in sys.argv

    vocal_lines: dict[str, int] = Counter()
    repeat_pool: Counter[str] = Counter()
    rows = []
    totals = {"lyric": 0, "vocal": 0, "blank": 0, "credit": 0, "placeholder": 0}
    sum_need = sum_uniq = 0

    for f in iter_audio(root):
        text = read_lyrics(f)
        if text is None:
            continue
        tags = read_tags(f)
        doc = clean.parse_tag_text(text)

        counts = {k: 0 for k in totals}
        for ln in doc.lines:
            counts[ln.kind] = counts.get(ln.kind, 0) + 1
            if ln.kind == "vocal":
                vocal_lines[ln.text.strip()] += 1
        for k in totals:
            totals[k] += counts.get(k, 0)

        need = doc.translatable_count
        uniq = len(doc.unique_translatable())
        sum_need += need
        sum_uniq += uniq
        for ln in doc.lines:
            if ln.needs_translation:
                repeat_pool[ln.text.strip()] += 1
        rows.append((tags, doc, counts, need, uniq))

    rows.sort(key=lambda r: -(r[3] - r[4]))

    if verbose:
        print(f"{'歌曲':<38} {'语言':<6} {'待译':>4} {'唯一':>4} {'省':>4} {'拟声':>4} {'空行':>4}")
        print("-" * 76)
        for tags, doc, counts, need, uniq in rows:
            name = tags.display()[:36]
            print(
                f"{name:<38} {doc.primary_lang:<6} {need:>4} {uniq:>4} "
                f"{need - uniq:>4} {counts['vocal']:>4} {counts['blank']:>4}"
            )
        print()

    print("=== 全库汇总 ===")
    print(f"  待译行（逐行计）  {sum_need}")
    print(f"  去重后需翻        {sum_uniq}")
    print(f"  重复省下          {sum_need - sum_uniq}   "
          f"（{100 * (sum_need - sum_uniq) / sum_need:.1f}%）" if sum_need else "")
    print(f"  拟声行            {totals['vocal']}")
    print(f"  空行              {totals['blank']}")
    print(f"  人员行            {totals['credit']}")
    print(f"  占位行            {totals['placeholder']}")
    print()

    print(f"=== 重复最多的行（跨库 Top 15）===")
    for text, n in repeat_pool.most_common(15):
        if n < 2:
            break
        print(f"  ×{n:<3} {text[:66]!r}")
    print()

    print(f"=== 被判为【不可译】的唯一内容（{len(vocal_lines)} 种）===")
    print("人工扫一遍：这里面混进真歌词了吗？")
    for text, n in vocal_lines.most_common():
        print(f"  {text!r:<58} 共 {n} 行")


if __name__ == "__main__":
    main()
