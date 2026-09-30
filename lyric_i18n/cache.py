r"""SQLite 缓存：断点续跑、不重复烧钱、可审计、可淘汰。

四个设计决定，每个都有理由：

1. **按整首歌缓存，不按行。**
   我们**故意**把完整歌词发给模型（保留上下文，副歌和主歌的关系会影响用词）。
   逐行缓存会在上下文变化时给出错误结果。整首缓存语义清晰：
   歌词和选项没变，结果就还能用。

2. **缓存键包含 PROMPT_VERSION，且版本号单独存一列。**
   调提示词是这个项目的常态。键里有版本号，旧结果自动失效，不会串味。
   但版本号埋在 sha1 里就**查不出来**——所以额外存一列 `prompt_version`，
   这样才能按版本批量清理。

3. **淘汰机制。**
   每改一次提示词，全库就多一份缓存，旧的全是死重量。
   `prune()` 删掉非当前版本的翻译缓存，以及过期的运行日志，然后 VACUUM。
   不清也不会错（键不同不会串），只是白占地方。

4. **备份不放这里。**
   缓存是可以随手删的东西，备份不是。备份在 `lyrics.py` 管理的
   `.lyric_i18n_backup/` 里，独立于数据库。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB = "lyric_i18n.db"

# 建表和建索引**必须分开**，而且索引要等迁移之后再建。
# 否则在早先版本的库上会崩：表已存在 → CREATE TABLE IF NOT EXISTS 不做事 →
# 索引语句引用了还没补上的 prompt_version 列。
_TABLES = """
CREATE TABLE IF NOT EXISTS song_cache (
    key            TEXT PRIMARY KEY,
    payload        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    prompt_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT,
    path    TEXT,
    status  TEXT NOT NULL,
    detail  TEXT,
    at      TEXT NOT NULL
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_run_log_key ON run_log(key);
CREATE INDEX IF NOT EXISTS idx_cache_version ON song_cache(prompt_version);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Cache:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(_TABLES)
        self._migrate()              # 先补列
        self.conn.executescript(_INDEXES)   # 再建索引（索引可能引用新列）
        self.conn.commit()

    def _migrate(self) -> None:
        """给早先版本的库补上新列。SQLite 的 ADD COLUMN 是幂等的安全操作。"""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(song_cache)")}
        if "prompt_version" not in cols:
            self.conn.execute(
                "ALTER TABLE song_cache ADD COLUMN prompt_version INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------- 翻译结果

    def get_song(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM song_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def put_song(self, key: str, payload: dict, prompt_version: int = 0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO song_cache (key, payload, created_at, prompt_version) "
            "VALUES (?, ?, ?, ?)",
            (key, json.dumps(payload, ensure_ascii=False), _now(), prompt_version),
        )
        self.conn.commit()

    def forget(self, key: str) -> bool:
        cur = self.conn.execute("DELETE FROM song_cache WHERE key = ?", (key,))
        self.conn.commit()
        return cur.rowcount > 0

    # -------------------------------------------------- 运行日志

    def log(self, key: str | None, path: str | None, status: str, detail: str = "") -> None:
        """status: ok / skipped / failed"""
        self.conn.execute(
            "INSERT INTO run_log (key, path, status, detail, at) VALUES (?, ?, ?, ?, ?)",
            (key, path, status, detail[:2000], _now()),
        )
        self.conn.commit()

    def last_status(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM run_log WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row else None

    def recent_failures(self, limit: int = 30) -> list[tuple]:
        return self.conn.execute(
            "SELECT path, detail, at FROM run_log WHERE status = 'failed' "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -------------------------------------------------- 淘汰

    def prune(
        self,
        current_version: int | None = None,
        keep_days: int = 30,
        run_log_keep: int = 5000,
    ) -> dict:
        """清理死重量。返回各表删了多少行。

        - `current_version` 给定时：删掉**非当前提示词版本**的翻译缓存。
          旧版本的产物永远不会再被用到（键里含版本号），留着纯占地方。
        - `keep_days`：删掉超过这个天数的运行日志。
        - `run_log_keep`：日志最多留这么多条，超出的按时间删最旧的。
        - 最后 VACUUM，把文件实际缩小。
        """
        before = self.path.stat().st_size if self.path.exists() else 0
        dead_cache = 0

        if current_version is not None:
            cur = self.conn.execute(
                "DELETE FROM song_cache WHERE prompt_version != ?", (current_version,)
            )
            dead_cache = cur.rowcount

        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        cur = self.conn.execute("DELETE FROM run_log WHERE at < ?", (cutoff,))
        old_logs = cur.rowcount

        cur = self.conn.execute(
            "DELETE FROM run_log WHERE id NOT IN "
            "(SELECT id FROM run_log ORDER BY id DESC LIMIT ?)",
            (run_log_keep,),
        )
        excess_logs = cur.rowcount
        self.conn.commit()

        self.conn.execute("VACUUM")
        after = self.path.stat().st_size if self.path.exists() else 0

        return {
            "dead_cache": dead_cache,
            "old_logs": old_logs,
            "excess_logs": excess_logs,
            "bytes_before": before,
            "bytes_after": after,
        }

    def clear(self, cache: bool = True, logs: bool = True) -> dict:
        """**全部清空**——和 `prune()` 不是一回事，别搞混。

        - `prune()`：只删**非当前提示词版本**的产物，是日常维护。
          当前版本的缓存是有效的，它不会动。
        - `clear()`：不管版本，全删。用途是"让工具忘掉翻过什么"，
          下次会重新调 API。

        这个区别被搞混过一次：界面上一个叫「清理缓存」的按钮实际调的是 `prune`，
        用户点完发现什么都没变（因为缓存全是当前版本），以为工具"记住了"。
        """
        before = self.path.stat().st_size if self.path.exists() else 0
        n_cache = n_logs = 0

        if cache:
            n_cache = self.conn.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
            self.conn.execute("DELETE FROM song_cache")
        if logs:
            n_logs = self.conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
            self.conn.execute("DELETE FROM run_log")
        self.conn.commit()

        self.conn.execute("VACUUM")
        after = self.path.stat().st_size if self.path.exists() else 0
        return {
            "cleared_cache": n_cache,
            "cleared_logs": n_logs,
            "bytes_before": before,
            "bytes_after": after,
        }

    def stats(self) -> dict:
        cached = self.conn.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
        by_version = dict(self.conn.execute(
            "SELECT prompt_version, COUNT(*) FROM song_cache GROUP BY prompt_version"
        ).fetchall())
        rows = dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM run_log GROUP BY status"
        ).fetchall())
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "cached_songs": cached,
            "by_prompt_version": by_version,
            "runs": rows,
            "db_bytes": size,
        }


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import io
    import sys
    import tempfile

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        with Cache(db) as c:
            print("空库：", c.stats())

            for v in (3, 4, 5):
                c.put_song(f"k{v}a", {"lines": ["甲"]}, prompt_version=v)
                c.put_song(f"k{v}b", {"lines": ["乙"]}, prompt_version=v)
            c.log("k5a", r"C:\m\a.m4a", "ok")
            c.log("k5b", r"C:\m\b.m4a", "failed", "行数不一致")

            print("写入后：", c.stats())

            out = c.prune(current_version=5)
            print(f"\n淘汰（保留 v5）：删缓存 {out['dead_cache']} 条，"
                  f"{out['bytes_before']} → {out['bytes_after']} 字节")
            print("淘汰后：", c.stats())

            assert c.get_song("k5a") is not None, "当前版本不该被删"
            assert c.get_song("k3a") is None, "旧版本应被删"
            print("\n✅ 淘汰逻辑正确：当前版本保留，旧版本清除")
