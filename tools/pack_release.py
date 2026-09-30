# -*- coding: utf-8 -*-
"""把仓库打包成一个 zip，放到桌面。

只收录 git 已暂存的文件——也就是 `.gitignore` 放行的那批。
这样能保证：**音频、数据库、缓存、配置都不会进包**。
（这个工具处理的是版权音乐，包里绝不该有音频。）

按 `git ls-files --cached` 取清单，而不是自己遍历目录再写排除规则——
排除规则只有一份（.gitignore），不会两边跑偏。
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Desktop" / "lyric-tag-translator-0.1.0.zip"
PREFIX = "lyric-tag-translator-0.1.0"

files = subprocess.run(
    ["git", "ls-files", "--cached"],
    cwd=ROOT, capture_output=True, text=True, check=True,
).stdout.split()
files = [f for f in files if f]

if not files:
    print("git 里没有已暂存的文件。先跑 git add -A。")
    raise SystemExit(1)

OUT.parent.mkdir(parents=True, exist_ok=True)
bad = []
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for rel in sorted(files):
        src = ROOT / rel
        if not src.is_file():
            continue
        if src.suffix.lower() in {".m4a", ".m4b", ".mp4", ".mp3", ".flac", ".lrc", ".db"}:
            bad.append(rel)
            continue
        z.write(src, f"{PREFIX}/{rel}")

if bad:
    print("！包里本不该出现这些，已跳过：", bad)

size = OUT.stat().st_size
print(f"已打包：{OUT}")
print(f"  {len(files)} 个文件，{size:,} 字节（{size / 1024:.1f} KB）")
print()
print("包内清单：")
with zipfile.ZipFile(OUT) as z:
    for n in z.namelist():
        print("  " + n)
