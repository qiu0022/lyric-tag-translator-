r"""©lyr 原子读写、备份与还原。

只管一个字段：`©lyr` —— iPod 的静态纯文本歌词。时间轴不在里面（iPod 不认）。

三条铁律，都来自真实踩过的坑：

1. **写盘前必先备份，且备份独立存放。**
   备份不放进缓存目录——缓存是可以随手删的东西，备份不是。
   备份**首次为准**：同一首歌重复翻译不会把"真正的原始版本"覆盖掉。

2. **原子写。**
   mutagen 的 `save()` 是原地重写整个文件。中途崩溃可能留下半个坏文件。
   所以：复制到临时文件 → 改临时文件 → `os.replace` 换回去。
   代价是多一倍磁盘 IO，换来的是"要么成功要么没动过"。

3. **幂等。**
   新内容与旧内容一致就不写、不备份。反复跑同一批不会污染备份历史。

注意：iTunes / Apple Music 打开着的时候可能锁住文件，写盘会失败。
失败是明确报错，不会静默跳过。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .identity import ATOM_LYRICS, AUDIO_EXT

BACKUP_DIRNAME = ".lyric_tag_translator_backup"
# 旧名。这工具一度叫 lyric-i18n，改名之前建立的备份目录还挂在这个名字上，
# **必须继续认它**。备份里是原始歌词，既是"翻坏了退回去"的唯一凭据，
# 也是判断"这首歌是不是已经动过"的依据。不认它有两个后果：
# 一是还原不了，二是翻好的歌会被当成新歌再翻一遍，译文叠加、把歌词写坏。
LEGACY_BACKUP_DIRNAME = ".lyric_i18n_backup"
TMP_SUFFIX = ".lyrici18n.tmp"


@dataclass
class WriteResult:
    path: Path
    changed: bool
    backup: Path | None = None
    before_lines: int = 0
    after_lines: int = 0
    reason: str = ""


# ---------------------------------------------------------------- 读


def read_lyrics(path: Path) -> str | None:
    """读出 ©lyr 原文。没有歌词返回 None。"""
    from mutagen.mp4 import MP4

    tags = MP4(path).tags
    val = tags.get(ATOM_LYRICS) if tags else None
    if not val:
        return None
    return "\n".join(str(v) for v in val)


def line_count(text: str | None) -> int:
    if not text:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip())


# ---------------------------------------------------------------- 备份


def backup_dir_for(root: Path) -> Path:
    """这首歌库的备份目录。

    库里已经有旧名目录就**继续用旧的**，不迁移。刻意不自动重命名：
    这目录在用户的音乐库里，"改名改出岔子"（备份失联 → 歌词被写坏）
    的代价，远大于名字不统一这点别扭。新库走新名，老库维持原样。
    """
    legacy = root / LEGACY_BACKUP_DIRNAME
    if legacy.is_dir():
        return legacy
    return root / BACKUP_DIRNAME


def _backup_file(backup_dir: Path, key: str) -> Path:
    safe = key.replace(":", "_").replace("/", "_").replace("\\", "_")
    return backup_dir / f"{safe}.json"


def save_backup(backup_dir: Path, key: str, path: Path, original: str) -> Path | None:
    """存下原始歌词。**首次为准**——已存在就不覆盖。

    这样无论后面翻译重跑多少次，`restore` 拿回的都是最初那份。
    """
    if original is None:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = _backup_file(backup_dir, key)
    if dst.exists():
        return dst

    payload = {
        "key": key,
        "path": str(path),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "original": original,
    }
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, dst)
    return dst


def load_backup(backup_dir: Path, key: str) -> dict | None:
    f = _backup_file(backup_dir, key)
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("*.json"))


def clear_backups(backup_dir: Path) -> int:
    """删除全部备份，返回删了多少份。

    **两个后果，都要清楚：**
    1. 不能再还原原始歌词了。
    2. "已处理"的标记也没了。但注意：**没有备份时的"重做"是危险的**——
       工具拿不到原始版本，会把现在这份双语文本当成原文再翻一遍，
       在已有译文后面再插一条。所以 `cli.collect()` 会在这种情况下拒绝重做，
       而不是放任它写坏文件。
    """
    if not backup_dir.exists():
        return 0
    n = len(list(backup_dir.glob("*.json")))
    shutil.rmtree(backup_dir)
    return n


# ---------------------------------------------------------------- 写


def write_lyrics(
    path: Path,
    text: str,
    *,
    key: str,
    backup_dir: Path | None = None,
    atomic: bool = True,
) -> WriteResult:
    """写入 ©lyr。先备份、再原子替换、内容相同则跳过。"""
    from mutagen.mp4 import MP4

    before = read_lyrics(path)
    before_n = line_count(before)
    after_n = line_count(text)

    if before is not None and before.strip() == text.strip():
        return WriteResult(path, False, None, before_n, after_n, reason="内容一致，跳过")

    backup_path = None
    if backup_dir is not None and before is not None:
        backup_path = save_backup(backup_dir, key, path, before)

    target = path
    if atomic:
        target = path.with_name(path.name + TMP_SUFFIX)
        shutil.copy2(path, target)

    try:
        audio = MP4(target)
        if audio.tags is None:
            audio.add_tags()
        audio.tags[ATOM_LYRICS] = [text]
        audio.save()
        if atomic:
            os.replace(target, path)
    except Exception as exc:  # noqa: BLE001
        if atomic and target.exists():
            target.unlink(missing_ok=True)
        raise RuntimeError(
            f"写入失败：{path}\n"
            f"  原因：{exc}\n"
            f"  提示：iTunes/Apple Music 是否正开着并锁住了这个文件？"
        ) from exc

    return WriteResult(path, True, backup_path, before_n, after_n)


def restore(path: Path, key: str, backup_dir: Path) -> WriteResult:
    """从备份还原原始歌词。"""
    data = load_backup(backup_dir, key)
    if data is None:
        raise FileNotFoundError(f"没有 {key} 的备份（{_backup_file(backup_dir, key)}）")
    return write_lyrics(
        path,
        data["original"],
        key=key,
        backup_dir=None,      # 还原动作本身不产生新备份，否则会覆盖原始版本
    )


# ---------------------------------------------------------------- 扫描辅助


def iter_audio(root: Path) -> list[Path]:
    return sorted(
        f for f in root.rglob("*") if f.suffix.lower() in AUDIO_EXT and f.is_file()
    )


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("用法：python -m lyric_tag_translator.lyrics <目录> [--show N] [--only 关键词]")
        print("  --show N   打印每首歌前 N 行歌词（看排版用）")
        print("  --only S   只处理名字或 key 含 S 的歌")
        print("（只读，不写任何文件）")
        raise SystemExit(0)

    from .identity import read_tags

    argv = sys.argv[1:]
    show = 0
    only = ""
    if "--show" in argv:
        i = argv.index("--show")
        show = int(argv[i + 1]) if i + 1 < len(argv) else 20
        del argv[i:i + 2]
    if "--only" in argv:
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else ""
        del argv[i:i + 2]

    root = Path(argv[0])
    files = iter_audio(root)
    print(f"扫描 {len(files)} 个音频文件（只读）\n")

    no_lyrics = []
    for f in files:
        tags = read_tags(f)
        if only and only not in tags.display() and only != tags.key:
            continue
        text = read_lyrics(f)
        if text is None:
            no_lyrics.append(tags.display())
            continue
        raw_lines = len(text.splitlines())
        filled = line_count(text)
        blank = raw_lines - filled
        print(f"{tags.display()}")
        print(f"    key={tags.key}  非空 {filled} 行 / 空行 {blank} 行")
        if show:
            for i, ln in enumerate(text.splitlines()[:show], 1):
                print(f"      {i:>3} | {ln}")
            if raw_lines > show:
                print(f"      …  还有 {raw_lines - show} 行")
            print()

    if no_lyrics:
        print(f"\n没有内嵌歌词：{len(no_lyrics)} 首")
        for name in no_lyrics:
            print(f"    {name}")
