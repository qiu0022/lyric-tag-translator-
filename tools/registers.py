# -*- coding: utf-8 -*-
"""列出缓存里每首歌的语域判定。

用来验证"露骨歌不会影响其他歌"——如果每首歌的语域各不相同，
说明模型是逐首判断的，没有把某一首的调子带到别的歌上。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("lyric_i18n.db")
if not db.exists():
    print(f"找不到 {db}")
    raise SystemExit(1)

conn = sqlite3.connect(str(db))
rows = []
for (payload,) in conn.execute("SELECT payload FROM song_cache"):
    d = json.loads(payload)
    reg = d.get("register") or "（未判定）"
    lang = d.get("src_lang") or "?"
    expl = d.get("explicit")
    rows.append((reg, lang, expl))

rows.sort()
print(f"{'语域判定':<40} {'源语言':<6} {'露骨'}")
print("-" * 58)
for reg, lang, expl in rows:
    mark = "是" if expl is True else ("" if expl is None else "否")
    print(f"{reg:<40} {lang:<6} {mark}")
print(f"\n共 {len(rows)} 首，{len({r[0] for r in rows})} 种不同语域")
