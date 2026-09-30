r"""歌曲身份层——整个工具的地基。

为什么不能用文件夹路径当身份（这是从真实库里总结出来的，不是臆想）：

    文件夹                          标签
    Abel Tesfaye\              →   艺人 = The Weeknd      （本名 vs 艺名）
    Compilations\...\          →   艺人 = 方大同           （"合辑" vs 歌手）
    ...(feat. Fut              →   ...(feat. Future & ...) （长名被截断）
    放错文件夹 / 繁简差异        →   标签不受影响

路径这条路在真实库里已经踩过四次坑。所以身份一律从**内嵌标签**取，
与文件放在哪、叫什么名字完全无关。

副产品：标签里的 `流派` / `年份` 是现成的风格锚点，直接喂给翻译提示词，
用来约束"译文必须落在与原曲相同的语域"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# MP4 原子名（\xa9 = ©）
ATOM_TITLE = "\xa9nam"
ATOM_ARTIST = "\xa9ART"
ATOM_ALBUM = "\xa9alb"
ATOM_GENRE = "\xa9gen"
ATOM_YEAR = "\xa9day"
ATOM_TRACK = "trkn"
ATOM_LYRICS = "\xa9lyr"

# 专辑艺人（合辑里每首歌的艺人可能不同，专辑艺人更稳定）
ATOM_ALBUM_ARTIST = "aART"

AUDIO_EXT = {".m4a", ".m4b", ".mp4"}


@dataclass
class SongTags:
    """一首歌的身份与风格锚点。字段全部可能为空——文件可能没打标签。"""

    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: str = ""
    track: int = 0
    track_total: int = 0

    @property
    def key(self) -> str:
        """稳定的歌曲身份。不受路径、文件名、繁简、截断影响。

        优先用 (艺人, 专辑, 音轨号, 标题)。标签缺失时逐级降级。
        """
        return make_key(self.artist, self.album, self.track, self.title, self.path)

    def style_hint(self) -> str:
        """给翻译提示词用的风格锚点，只包含确实有值的字段。"""
        bits = []
        if self.genre:
            bits.append(f"流派：{self.genre}")
        if self.year:
            bits.append(f"年份：{self.year[:4]}")
        if self.artist:
            bits.append(f"艺人：{self.artist}")
        if self.album:
            bits.append(f"专辑：{self.album}")
        return "；".join(bits)

    def display(self) -> str:
        trk = f"{self.track:02d} " if self.track else ""
        who = self.artist or self.album_artist or "未知艺人"
        return f"{who} - {trk}{self.title or self.path.stem}"


def make_key(
    artist: str,
    album: str,
    track: int,
    title: str,
    fallback_path: Path | None = None,
) -> str:
    """生成稳定 key。

    标签齐全时，key 只由标签决定 —— 同一个文件放在哪都算出同一个 key。
    标签全缺时才退回路径，并在 key 前缀标注，使降级情况一眼可见。
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    title = (title or "").strip()

    if artist or album or title:
        material = "\x1f".join([artist, album, f"{track:03d}", title])
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
        return f"tag:{digest}"

    # 极端降级：没有任何标签，只能靠路径
    rel = str(fallback_path) if fallback_path else ""
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
    return f"path:{digest}"


# ---------------------------------------------------------------- 读标签


def _first(tags: dict, atom: str) -> str:
    val = tags.get(atom)
    if not val:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def _track_pair(tags: dict) -> tuple[int, int]:
    val = tags.get(ATOM_TRACK)
    if isinstance(val, list) and val:
        pair = val[0]
        if isinstance(pair, tuple) and len(pair) >= 2:
            return int(pair[0]), int(pair[1])
    return 0, 0


def read_tags(path: Path) -> SongTags:
    """读取一首歌的身份标签。读不到标签是正常情况，不抛异常。"""
    from mutagen.mp4 import MP4  # 延迟导入，让本模块能被无依赖地单测

    st = SongTags(path=path)
    try:
        audio = MP4(path)
    except Exception:  # noqa: BLE001 - 文件损坏不应中断整批
        return st

    tags = audio.tags or {}
    st.title = _first(tags, ATOM_TITLE)
    st.artist = _first(tags, ATOM_ARTIST)
    st.album = _first(tags, ATOM_ALBUM)
    st.album_artist = _first(tags, ATOM_ALBUM_ARTIST)
    st.genre = _first(tags, ATOM_GENRE)
    st.year = _first(tags, ATOM_YEAR)
    st.track, st.track_total = _track_pair(tags)
    return st


def scan(root: Path) -> list[SongTags]:
    """递归扫描目录，返回所有音频文件的身份标签。"""
    files = sorted(
        f for f in root.rglob("*") if f.suffix.lower() in AUDIO_EXT and f.is_file()
    )
    return [read_tags(f) for f in files]


def find_duplicate_keys(songs: list[SongTags]) -> dict[str, list[SongTags]]:
    """找出身份重复的歌曲。

    重复意味着标签不足以区分（例如同一专辑里两首同名同轨号），
    或者同一个文件被放了两份。必须在写盘前让人看到，而不是静默覆盖。
    """
    groups: dict[str, list[SongTags]] = {}
    for s in songs:
        groups.setdefault(s.key, []).append(s)
    return {k: v for k, v in groups.items() if len(v) > 1}


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
        songs = scan(root)
        print(f"扫描 {len(songs)} 首\n")
        for s in songs[:15]:
            print(f"{s.key}  {s.display()}")
            print(f"          {s.style_hint()}")
        dupes = find_duplicate_keys(songs)
        print(f"\n身份重复：{len(dupes)} 组")
        for k, v in dupes.items():
            print(f"  {k}")
            for s in v:
                print(f"    {s.path}")
    else:
        # 无参数时用构造数据验证 key 的稳定性
        p1 = Path(r"C:\Music\Abel Tesfaye\Starboy\01 Starboy.m4a")
        p2 = Path(r"D:\备份\The Weeknd - Starboy.m4a")
        k1 = make_key("The Weeknd", "Starboy", 1, "Starboy", p1)
        k2 = make_key("The Weeknd", "Starboy", 1, "Starboy", p2)
        print("同一首歌、不同路径、不同文件夹名：")
        print(f"  {p1}  →  {k1}")
        print(f"  {p2}  →  {k2}")
        print(f"  key 相同？{k1 == k2}")
        k3 = make_key("", "", 0, "", p1)
        print(f"\n标签全缺时降级：{k3}")
