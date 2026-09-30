# -*- coding: utf-8 -*-
"""看缓存库里到底存了什么，以及 test 目录的音频文件是否真的被改过。"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

db = Path(sys.argv[1])
if not db.exists():
    print(f"没有 {db}")
    raise SystemExit(0)

conn = sqlite3.connect(str(db))
print(f"=== {db} ===")
print(f"文件大小 {db.stat().st_size:,} 字节\n")

print("表：", [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")])

try:
    n = conn.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
    print(f"\nsong_cache：{n} 条")
    for ver, c in conn.execute(
            "SELECT prompt_version, COUNT(*) FROM song_cache GROUP BY prompt_version"):
        print(f"    提示词 v{ver}: {c} 条")
    row = conn.execute("SELECT payload FROM song_cache LIMIT 1").fetchone()
    if row:
        d = json.loads(row[0])
        print(f"    样例：语域={d.get('register')}  行数={len(d.get('lines') or [])}")
except sqlite3.OperationalError as e:
    print("song_cache 读取失败：", e)

try:
    print("\nrun_log：")
    for st, c in conn.execute("SELECT status, COUNT(*) FROM run_log GROUP BY status"):
        print(f"    {st}: {c} 条")
    print("  最近 5 条：")
    for path, status, at in conn.execute(
            "SELECT path, status, at FROM run_log ORDER BY id DESC LIMIT 5"):
        print(f"    {at}  {status:<10} {Path(path).name if path else ''}")
except sqlite3.OperationalError as e:
    print("run_log 读取失败：", e)
