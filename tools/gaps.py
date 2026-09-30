# -*- coding: utf-8 -*-
"""精确查漏：找出"外语行后面没有跟译文"的情况。

比行数比值准。原理：
    拿改动前的原始歌词当基准，用指针在现有歌词里走一遍，
    把每一行判定为【原文】或【译文】。
    判为【原文】但下一行不是【译文】的 → 这行没被翻译。

只读，不改任何文件。
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
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def keyof(path: Path, root: Path) -> str:
    return "__".join(path.relative_to(root).with_suffix("").parts)


def load_baseline(txt: Path) -> dict[str, list[str]]:
    songs: dict[str, list[str]] = {}
    cur, buf = None, []
    for line in txt.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip().lstrip("﻿")
        if s.startswith(SEP) and s.endswith(SEP) and len(s) > len(SEP) * 2:
            if cur is not None:
                songs[cur] = buf
            cur, buf = s[len(SEP):-len(SEP)].strip(), []
        elif cur is not None and s:
            buf.append(s)
    if cur is not None:
        songs[cur] = buf
    return songs


def classify(current: list[str], base: list[str]) -> list[tuple[str, int]]:
    """把现有行逐行判为 ('orig', idx) 或 ('trans', -1)。"""
    out, p = [], 0
    for line in current:
        if p < len(base) and line == base[p]:
            out.append(("orig", p))
            p += 1
        else:
            out.append(("trans", -1))
    return out


def main() -> None:
    music = Path(sys.argv[1])
    baseline = load_baseline(Path(sys.argv[2]))

    files = sorted(f for f in music.rglob("*") if f.suffix.lower() in EXT and f.is_file())

    total_gaps = 0
    offenders = []
    for f in files:
        k = keyof(f, music)
        base = baseline.get(k)
        if not base:
            continue
        audio = MP4(f)
        raw = audio.tags.get(KEY) if audio.tags else None
        if not raw:
            continue
        current = [ln.strip() for ln in "\n".join(raw).splitlines() if ln.strip()]

        marks = classify(current, base)
        gaps = []
        for i, (kind, idx) in enumerate(marks):
            if kind != "orig":
                continue
            nxt = marks[i + 1] if i + 1 < len(marks) else None
            if nxt is None or nxt[0] != "trans":
                src = base[idx]
                if not CJK.search(src):          # 中文行不需要译文，跳过
                    gaps.append(src)

        if gaps:
            offenders.append((k, gaps))
            total_gaps += len(gaps)

    if not offenders:
        print("没有查到漏译的外语行。")
    for k, gaps in offenders:
        print(f"=== {k}  （{len(gaps)} 行）")
        for g in gaps[:20]:
            print(f"    {g[:80]}")
        if len(gaps) > 20:
            print(f"    ……还有 {len(gaps) - 20} 行")
        print()

    print(f"合计：{len(offenders)} 首 / {total_gaps} 行疑似漏译")


if __name__ == "__main__":
    main()
