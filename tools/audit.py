# -*- coding: utf-8 -*-
"""对账：用改动前的 全部歌词.txt 当基准，报告每首 m4a 现在的双语状态。

只读，不修改任何文件。

判据（基于你现有的"原文行 + 紧随译文行"格式）：
    非空行数 ≈ 2 × 原始非空行数   → 全量双语（整首翻过）
    非空行数 ≈ 1 × 原始非空行数   → 未改动
    介于两者之间                   → 部分补译（只给整行英文插了译文）
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import re
import sys
from pathlib import Path

from mutagen.mp4 import MP4

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

KEY = "\xa9lyr"
EXT = {".m4a", ".m4b", ".mp4"}
SEP = "##########"


def keyof(path: Path, root: Path) -> str:
    return "__".join(path.relative_to(root).with_suffix("").parts)


def load_baseline(txt: Path) -> dict[str, int]:
    """从 全部歌词.txt 读每首的原始非空行数。"""
    songs: dict[str, int] = {}
    cur, count = None, 0
    for line in txt.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip().lstrip("﻿")
        if s.startswith(SEP) and s.endswith(SEP) and len(s) > len(SEP) * 2:
            if cur is not None:
                songs[cur] = count
            cur, count = s[len(SEP):-len(SEP)].strip(), 0
        elif cur is not None and s:
            count += 1
    if cur is not None:
        songs[cur] = count
    return songs


def main() -> None:
    music = Path(sys.argv[1])
    baseline_path = Path(sys.argv[2])

    baseline = load_baseline(baseline_path)
    print(f"基准 {baseline_path.name}：{len(baseline)} 首\n")

    files = sorted(
        f for f in music.rglob("*") if f.suffix.lower() in EXT and f.is_file()
    )

    buckets = {"全量双语": [], "部分补译": [], "未改动": [], "无歌词": [], "基准里没有": []}
    for f in files:
        k = keyof(f, music)
        audio = MP4(f)
        raw = audio.tags.get(KEY) if audio.tags else None
        if not raw:
            buckets["无歌词"].append((k, 0, baseline.get(k)))
            continue

        cur = sum(1 for ln in "\n".join(raw).splitlines() if ln.strip())
        base = baseline.get(k)
        if base is None:
            buckets["基准里没有"].append((k, cur, None))
            continue

        ratio = cur / base if base else 0
        if ratio >= 1.8:
            buckets["全量双语"].append((k, cur, base))
        elif ratio <= 1.15:
            buckets["未改动"].append((k, cur, base))
        else:
            buckets["部分补译"].append((k, cur, base))

    for name, items in buckets.items():
        print(f"=== {name}：{len(items)} 首 ===")
        for k, cur, base in items:
            if base is None:
                print(f"  {k}   当前{cur}行  (基准缺失)")
            else:
                print(f"  {k}   当前{cur}行 / 原始{base}行  ≈{cur / base:.2f}×" if base else f"  {k}")
        print()

    print(f"合计扫描 {len(files)} 个文件")


if __name__ == "__main__":
    main()
