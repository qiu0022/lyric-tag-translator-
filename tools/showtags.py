# -*- coding: utf-8 -*-
"""看 m4a 的内嵌标签。用途：判断能不能用标签（而非文件夹路径）当歌曲身份。

只读。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import sys
from pathlib import Path

from mutagen.mp4 import MP4

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXT = {".m4a", ".m4b", ".mp4"}
WANT = [
    ("\xa9nam", "标题"),
    ("\xa9ART", "艺人"),
    ("\xa9alb", "专辑"),
    ("\xa9gen", "流派"),
    ("trkn", "音轨"),
    ("\xa9day", "年份"),
    ("\xa9lyr", "歌词"),
]


def show(path: Path, root: Path) -> None:
    try:
        tags = MP4(path).tags or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[读取失败] {path.name}: {exc}")
        return

    print(f"--- {path.relative_to(root)}")
    for key, label in WANT:
        val = tags.get(key)
        if val is None:
            print(f"    {label:<4} (无)")
            continue
        if key == "\xa9lyr":
            lines = "\n".join(val).splitlines()
            print(f"    {label:<4} {len(lines)} 行")
        else:
            text = val[0] if isinstance(val, list) else val
            print(f"    {label:<4} {text}")


def main() -> None:
    root = Path(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    files = sorted(f for f in root.rglob("*") if f.suffix.lower() in EXT and f.is_file())
    step = max(1, len(files) // limit)
    for f in files[::step][:limit]:
        show(f, root)


if __name__ == "__main__":
    main()
