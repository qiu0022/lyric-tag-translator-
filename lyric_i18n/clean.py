r"""歌词清洗与规范化。

输入有两种形态，处理方式不同：

  1. **`©lyr`（iPod 用的那个）** —— 纯文本，**没有时间轴**，空行表示段落边界。
  2. **`.lrc` 侧车** —— 带 `[mm:ss.xx]` 时间轴，供将来导出定时歌词用。

两者统一解析成"保留原始行位"的结构：**每一行都在列表里占位**，
只有需要翻译的行才送出去。翻译回来后按原索引回填，所以重排风险为零。

行分类（决定要不要翻译）：

    blank        空行 / 纯标点          保留，不译
    meta         [ti:] [ar:] [offset:]  保留，不译
    credit       作词/作曲/编曲/监制…    保留，不译
    placeholder  「纯音乐，请欣赏」       保留，不译
    vocal        拟声/和声/注音符号       **保留，不译** ← 见下
    lyric        真正要翻的歌词

`vocal` 这一类是拿真实库对账之后才加的，不是想当然。库里实际出现过：

    Oh-oh                                  La-la-la-la-la-la
    Say whoa-oh-oh-oh, whoa-oh-oh-oh       Baby-baby-baby-baby, oh, baby
    ㄅㄆㄇㄈㄉㄊㄋㄌ                          (Oh oh, oh oh oh oh)

这些如果当成歌词送翻译，会变成「哦-哦」「啦-啦-啦」「宝贝-宝贝-宝贝」——
**把整首歌毁掉**。所以必须在清洗层就拦掉，不能指望模型自觉。

人名（如 `Angeline`）不走机械判断——中文里没法定量识别专有名词，
交给提示词约束"人名/品牌保留原文"，那是语义问题，代码管不了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 字符类

RE_KANA = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ]")
RE_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
RE_HANGUL = re.compile(r"[가-힯]")
# 注音符号（Bopomofo）：陶喆《讨厌红楼梦》里念的 ㄅㄆㄇㄈ
RE_BOPOMOFO = re.compile(r"[ㄅ-ㄯㆠ-ㆿ]")
RE_LATIN = re.compile(r"[A-Za-z]")

# 行类型
KIND_BLANK = "blank"
KIND_META = "meta"
KIND_CREDIT = "credit"
KIND_PLACEHOLDER = "placeholder"
KIND_VOCAL = "vocal"
KIND_LYRIC = "lyric"


# ---------------------------------------------------------------- 词表

# 纯感叹/拟声：整行只有它们才算「不可译」。
# 只收"没有命题意义"的音节词。say / come / on / go / please 这类能组词的真词
# **不能**放进来——否则 `Come on` 会被误判成拟声而漏翻（实测踩过）。
INTERJECTIONS: frozenset[str] = frozenset({
    "oh", "ooh", "ohh", "uh", "ah", "aah", "ahh", "eh", "er", "hmm", "mm",
    "mmm", "hm", "ha", "haha", "hey", "heyy", "whoa", "woah", "woo",
    "yeah", "yea", "yah", "ya", "yo", "yuh", "ay", "aye",
    "la", "lala", "na", "nana", "da", "dada", "doo", "du", "dum", "di", "de",
    "ba", "bab", "bam", "boom", "chu", "shh", "sh", "tsk", "wow", "ouch", "ow",
    "no", "say",
    # 亲吻声一类
    "mwah", "muah", "mwha",
})
# 在副歌里当音节用、但本身也有实义的词。只有**整行全是这类词**且种类极少时，
# 才判定为拟声——这样 "Man, I love my baby, ah" 不会被误伤。
VOCABLE_EXTRAS: frozenset[str] = frozenset({
    "baby", "babe", "girl", "boy", "yeah", "yay", "woo", "hey", "la", "na", "da",
    "doo", "du", "dum", "oh", "ooh", "ah", "uh", "whoa", "hmm", "mm",
})


# ---------------------------------------------------------------- 行分类判定

_PUNCT_ONLY_RE = re.compile(r"^[\s\-—–~～.。…·、,，!！?？*＊♪♫♡♥#@&/\\|+]+$")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-zÀ-ɏ]+")


def _normalize_syllables(text: str) -> str:
    """去掉标点、连字符、空白，转小写。用于音节级判断。"""
    return re.sub(r"[\s\-—–~～.。…·、,，!！?？*＊♪♫♡♥'\"“”‘’()（）\[\]【】/\\|+]", "", text).lower()


def _tokens(text: str) -> list[str]:
    """切成"词"。CJK 每个字算一个 token，拉丁按词切。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if RE_HAN.match(ch) or RE_KANA.match(ch) or RE_HANGUL.match(ch) or RE_BOPOMOFO.match(ch):
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        elif ch.isalnum() or ch in "'’":
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
    if buf:
        out.append(buf)
    return [t.lower() for t in out if t]


def _repeated_unit(s: str) -> str | None:
    """如果整串是某个短单元的重复，返回那个单元；否则 None。

    lalala → "la"；ohoh → "oh"；doodoodoo → "doo"。
    返回单元而不是布尔值，是为了让调用方能进一步判断"这个单元是不是音节词"——
    `Tap Tap Tap` 也是重复，但单元 "tap" 是实义词，不该当拟声。
    """
    n = len(s)
    if n < 2:
        return None
    for size in range(1, min(6, n // 2) + 1):
        if n % size == 0:
            unit = s[:size]
            if unit * (n // size) == s:
                return unit
    return None


# 行尾的**纯声音词**：中文读者看英文也懂，译成「啊/耶」反而失真。
# 注意这里刻意**不含** no / say / come / go 这类有实义的词——
# 它们出现在句尾是正常内容，不是语气词，还原会出错。
TRAILING_SOUNDS: frozenset[str] = frozenset({
    "ah", "oh", "ooh", "uh", "eh", "yeah", "yea", "yah", "ya", "yo",
    "hey", "ha", "haha", "hmm", "mm", "mmm", "hm",
    "woo", "whoa", "woah", "wow", "la", "na", "da", "doo", "du",
    "ay", "aye", "yuh", "shh",
})

_TRAILING_SRC_RE = re.compile(r"[,，、\s]+([A-Za-z']+)\s*[!！~～.。]*\s*$")
_TRAILING_CN_RE = re.compile(r"[,，、\s]*[啊呀呢吧哦噢耶哟哈唉哎嘿唔嗯][!！~～.。]*\s*$")


def restore_trailing_sound(src: str, dst: str) -> str:
    """把译文行尾的中文语气词还原成原文那个英文 token。

    实测问题：`..., ah` 被译成「，啊」。中文读者看 `ah` 完全懂，
    而「啊」既丢了原文质感，又和歌词本身的语言混在一起。
    用户既有的成品里 `yeah` 保留英文、`ah` 却译成「啊」——不一致。

    这件事能机械判断，所以不指望模型自觉，在代码里定死。
    """
    m = _TRAILING_SRC_RE.search(src)
    if not m:
        return dst
    token = m.group(1)
    if token.lower() not in TRAILING_SOUNDS:
        return dst
    if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])\s*[!！~～.。]*\s*$", dst, re.I):
        return dst                                   # 模型已经保留了英文
    stripped = _TRAILING_CN_RE.sub("", dst).rstrip()
    if not stripped:
        return dst                                   # 整句都是语气词，不动
    return f"{stripped}，{token}"


def is_vocal_loose(text: str) -> bool:
    """宽松版拟声判断，**只用于报告**（`check`），不用于决定"要不要翻译"。

    为什么需要两个版本：
    - `is_vocal` 严格，因为误判会让**真歌词被静默跳过**——那个错误没人看得见。
    - `check` 是报告工具，误判的代价只是"少报一行"，用户看歌词本身就能发现；
      而漏报的代价是一堆噪音，人就不看了。

    所以这里放宽到：整行 token 全是感叹词、音节词、或单个字母。
    实测筛掉的例子：`♡ yeah, i, i, i`、`yeah, i, i, i (ooh, ooh, yeah)`。
    """
    if is_vocal(text):
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if RE_HAN.search(stripped) or RE_KANA.search(stripped) or RE_HANGUL.search(stripped):
        return False
    toks = _tokens(stripped)
    if not toks:
        return True
    return all(
        t in INTERJECTIONS or t in VOCABLE_EXTRAS or len(t) <= 1
        for t in toks
    )


def is_vocal(text: str) -> bool:
    """判断是否"不可译行"：拟声、和声、注音符号、纯语气。

    判定顺序很重要——只要有一条命中就算，宁可漏翻一行，
    也不能把 `Oh-oh` 翻成「哦-哦」写进歌词。
    """
    stripped = text.strip()
    if not stripped:
        return True

    # 纯标点 / 装饰符号
    if _PUNCT_ONLY_RE.match(stripped):
        return True

    # 含汉字/假名/谚文 → 是正常歌词语言，不是拟声。
    # 这一步**必须**排在注音符号判断之前：
    # 「ㄜ 小姐 請問一下 有沒有賣半島鐵盒」是周杰伦的真歌词（半岛铁盒），
    # 只因行首带一个注音符号，早先版本整行被误杀。
    if RE_HAN.search(stripped) or RE_KANA.search(stripped) or RE_HANGUL.search(stripped):
        return False

    # 整行只有注音符号（ㄅㄆㄇㄈㄉㄊㄋㄌ）才算不可译
    if not _strip_bopomofo(stripped):
        return True

    # 到这里只剩拉丁字母和符号
    if not RE_LATIN.search(stripped):
        return True

    norm = _normalize_syllables(stripped)
    if not norm:
        return True

    toks = _tokens(stripped)
    if not toks:
        return True

    # 全是感叹词（整行就是 oh / yeah / no）
    if all(t in INTERJECTIONS for t in toks):
        return True

    # 全是"音节词"、种类极少、且足够长 → Baby-baby-baby-baby, oh, baby
    # 要求 len(toks) >= 3 是故意的：`Girl, yeah` 这种两个词的短句
    # 也满足"种类极少"，但它更可能是真歌词。宁可放过。
    kinds = set(toks)
    if kinds <= VOCABLE_EXTRAS and len(kinds) <= 2 and len(toks) >= 3:
        return True

    # 整串是某个**音节单元**的重复 → lalalala / doodooodoo
    # 单元必须本身就在音节词表里，否则 `Tap Tap Tap`（真歌词）会被吃掉。
    unit = _repeated_unit(norm)
    if unit is not None and (unit in INTERJECTIONS or unit in VOCABLE_EXTRAS):
        return True

    # 字符种类极少且极短 → No / i,i
    if len(set(norm)) <= 2 and len(norm) <= 4:
        return True

    return False


_BOPOMOFO_STRIP_RE = re.compile(
    r"[\s\-—–~～.。…·、,，!！?？*＊♪♫♡♥'\"“”‘’()（）\[\]【】/\\|+]"
)


def _strip_bopomofo(text: str) -> str:
    """去掉注音符号与标点后剩下的内容。空 = 整行只有注音符号。"""
    return _BOPOMOFO_STRIP_RE.sub("", RE_BOPOMOFO.sub("", text))


# ---------------------------------------------------------------- 制作人员 / 占位

_CREDIT_KEYS: frozenset[str] = frozenset({
    "作词", "作曲", "编曲", "混音", "母带", "演唱", "作词作曲", "词曲",
    "监制", "出品", "制作人", "和声", "录音", "调音", "调教", "视频", "插画",
    "封面", "翻译", "发行", "贝斯", "鼓手", "弦乐", "吉他", "钢琴", "策划",
    "特别感谢", "感谢", "歌名", "素材", "字幕", "压制", "原唱", "翻唱",
    "统筹", "企划", "美术", "设计", "摄影", "后期", "校对", "润色", "协助",
    "词", "曲", "唱", "编", "混", "录", "调", "演", "奏",
    "lyrics", "lyric", "composed", "composer", "arranged", "arranger",
    "produced", "producer", "mixed", "mastered", "vocals", "guitar",
})
_CREDIT_PREFIX = "©＠@※＊*·-—– \t"
_CREDIT_SPLIT_RE = re.compile(r"[／/\\、,，:：\s]+")

_PLACEHOLDER_NORM: frozenset[str] = frozenset({
    "暂无歌词", "纯音乐", "无歌词", "请欣赏", "没有填词",
    "此歌曲为没有填词的纯音乐", "纯音乐请欣赏", "暂无歌词请欣赏",
    "instrumental", "nolyrics",
})
_PLACEHOLDER_PREFIX: tuple[str, ...] = ("纯音乐", "暂无歌词", "无歌词", "请欣赏", "没有填词")


def is_credit(text: str) -> bool:
    """制作人员行。只在**整段匹配**时命中：「作词：张三」算，「谢谢你」不算。"""
    s = text.strip().lstrip(_CREDIT_PREFIX).strip()
    if not s:
        return False
    head = re.split(r"[:：]", s, maxsplit=1)[0]
    parts = [p for p in _CREDIT_SPLIT_RE.split(head) if p]
    if not parts:
        return False
    return all(p.lower() in _CREDIT_KEYS for p in parts)


def is_placeholder(text: str) -> bool:
    norm = re.sub(r"[\s\-—–~～.。…·、,，!！?？:：;；'\"“”‘’()（）\[\]【】]", "", text).lower()
    if not norm:
        return False
    return norm in _PLACEHOLDER_NORM or norm.startswith(_PLACEHOLDER_PREFIX)


# ---------------------------------------------------------------- 编码

def sniff_decode(raw: bytes) -> tuple[str, str]:
    """字节级解码嗅探。中文歌词圈的编码现实：UTF-8 BOM / 裸 UTF-8 / GBK / 偶尔 Big5。

    utf-8 严格解码失败再退 gb18030（GBK 超集），最后才用替换字符兜底——
    宁可留问号，也不要静默产生乱码送进翻译。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


# ---------------------------------------------------------------- 数据结构

_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(r"^\[([a-zA-Z#]+):([^\]]*)\]\s*$")


@dataclass
class Line:
    index: int
    raw: str = ""
    text: str = ""
    timestamps: list[str] = field(default_factory=list)
    kind: str = KIND_LYRIC
    needs_translation: bool = False
    # 与之前某一行文本完全相同 → 指向首次出现的 index。
    # 副歌会重复很多遍，靠它做到"同一行只翻一次"。
    repeat_of: int | None = None

    @property
    def is_blank(self) -> bool:
        return self.kind == KIND_BLANK

    @property
    def is_repeat(self) -> bool:
        return self.repeat_of is not None


@dataclass
class LyricDoc:
    lines: list[Line] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    offset_ms: int = 0
    encoding: str = "unknown"
    has_timestamps: bool = False
    primary_lang: str = "unknown"

    def to_translate(self) -> list[Line]:
        return [ln for ln in self.lines if ln.needs_translation]

    def source_texts(self) -> list[str]:
        return [ln.text for ln in self.lines if ln.needs_translation]

    def apply(self, translations: list[str]) -> list[str]:
        """把译文按原索引回填，产出最终的双语行序列。

        格式：原文行 + 译文行；**空行只出现一次，不配译文**。
        这是从既有成品里反推出来的规则。
        """
        targets = self.to_translate()
        if len(translations) != len(targets):
            raise ValueError(
                f"译文数量与待译行数不符：{len(translations)} vs {len(targets)}"
            )
        by_index = {ln.index: t for ln, t in zip(targets, translations)}

        out: list[str] = []
        for ln in self.lines:
            if ln.kind == KIND_META:
                continue                      # 元数据不进 iPod 歌词
            if ln.kind == KIND_BLANK:
                out.append("")                # 空行保留一次
                continue
            out.append(ln.text)
            translated = by_index.get(ln.index)
            if translated is None:
                continue
            t = translated.strip()
            # 译文与原文相同 → 不输出译文行。
            #
            # 这是第二道保险。提示词要求模型把拟声词、人名、品牌名**原样返回**，
            # 所以这里不必再写一遍。它同时兜住了启发式的漏判：就算某个 `Oh-oh`
            # 没被 is_vocal 拦住、送去了翻译，只要模型返回原文，
            # 也不会产生 "Oh-oh / Oh-oh" 这种重复行。
            #
            # 有了这道保险，"哪个拟声词该算拟声"就不再是单点故障。
            if t and t != ln.text.strip():
                out.append(t)
        return out

    # ------------------------------------------------------------ 重复行

    def unique_translatable(self) -> list[str]:
        """去重后的待译文本，按首次出现顺序。

        为什么必须去重：副歌会重复很多遍。逐行送翻时，同一句原文在第二次、
        第三次出现的位置，模型很可能给出**不一样的译文**——它每次都在重新
        斟酌，而没有任何东西告诉它"这句刚才翻过"。

        去重后只翻一次再复用，既省 token，更重要的是把一致性锁死。
        """
        seen: set[str] = set()
        out: list[str] = []
        for ln in self.lines:
            if ln.needs_translation and ln.text not in seen:
                seen.add(ln.text)
                out.append(ln.text)
        return out

    def expand_unique(self, unique_translations: list[str]) -> list[str]:
        """把"按唯一行翻译"的结果展开回逐行译文，顺序与 to_translate() 一致。"""
        uniq = self.unique_translatable()
        if len(unique_translations) != len(uniq):
            raise ValueError(
                f"唯一译文数量不符：{len(unique_translations)} vs {len(uniq)}"
            )
        table = dict(zip(uniq, unique_translations))
        return [table[ln.text] for ln in self.lines if ln.needs_translation]

    def enforce_consistency(self, translations: list[str]) -> tuple[list[str], int]:
        """把重复行的译文统一到首次出现的那一份，返回 (新译文, 被改动的处数)。

        为什么光靠"送翻前先去重"不够：
        去重只省了钱，但代价是**模型看不到完整歌词**——它不知道哪句是副歌、
        和上下文什么关系，翻译质量会掉。

        所以改成：照常送完整歌词（保住上下文），拿到结果后在这里强制统一。
        同一句原文只认第一次的译文，后面一律复用。这是确定性的，
        一定能消掉漂移；被改动的处数顺便成了一个质量指标——
        它等于"模型本来会漂移多少处"。
        """
        targets = self.to_translate()
        if len(translations) != len(targets):
            raise ValueError(
                f"译文数量与待译行数不符：{len(translations)} vs {len(targets)}"
            )
        first: dict[str, str] = {}
        out: list[str] = []
        fixed = 0
        for ln, tr in zip(targets, translations):
            key = ln.text
            if key in first:
                if first[key] != tr:
                    fixed += 1
                out.append(first[key])
            else:
                first[key] = tr
                out.append(tr)
        return out, fixed

    @property
    def translatable_count(self) -> int:
        return sum(1 for ln in self.lines if ln.needs_translation)

    @property
    def repeat_saving(self) -> int:
        """去重省下的行数。"""
        return self.translatable_count - len(self.unique_translatable())


# ---------------------------------------------------------------- 主解析

def parse(
    text: str,
    encoding: str = "unknown",
    force_translate: frozenset[str] | None = None,
) -> LyricDoc:
    """解析歌词文本（©lyr 纯文本 或 .lrc 时间轴），产出结构化文档。

    force_translate：人工豁免集合，命中的行强制当歌词送去翻译。
    """
    force = force_translate or frozenset()
    doc = LyricDoc(encoding=encoding)

    for raw_line in text.splitlines():
        raw = raw_line.rstrip("\r\n")

        meta_match = _META_RE.match(raw.strip())
        if meta_match:
            key, value = meta_match.group(1).lower(), meta_match.group(2).strip()
            if key == "offset":
                doc.offset_ms = _parse_offset(value)
            else:
                doc.meta[key] = value
            doc.lines.append(Line(index=len(doc.lines), raw=raw, kind=KIND_META))
            continue

        timestamps = [m.group(0) for m in _TIMESTAMP_RE.finditer(raw)]
        content = _TIMESTAMP_RE.sub("", raw).strip()
        if timestamps:
            doc.has_timestamps = True

        doc.lines.append(
            Line(
                index=len(doc.lines),
                raw=raw,
                text=content,
                timestamps=timestamps,
                kind=KIND_LYRIC,      # 先假设是歌词，下面再降级
            )
        )

    _classify(doc, force)
    doc.primary_lang = _detect_primary_language(doc)
    _mark_translatable(doc)
    _mark_repeats(doc)
    return doc


def _classify(doc: LyricDoc, force_translate: frozenset[str]) -> None:
    for ln in doc.lines:
        if ln.kind == KIND_META:
            continue
        t = ln.text
        if not t.strip() or _PUNCT_ONLY_RE.match(t.strip()):
            ln.kind = KIND_BLANK
        elif is_placeholder(t):
            ln.kind = KIND_PLACEHOLDER
        elif is_credit(t):
            ln.kind = KIND_CREDIT
        elif t.strip() in force_translate:
            # 人工豁免：拟声判断是启发式，总会有边界误判。
            # 放进 force_translate 里的行一律当歌词处理。
            ln.kind = KIND_LYRIC
        elif is_vocal(t):
            ln.kind = KIND_VOCAL
        else:
            ln.kind = KIND_LYRIC


def _detect_primary_language(doc: LyricDoc) -> str:
    """判断整首歌的主体语言。用于决定中文行要不要翻。

    依据：整首里只要出现假名就是日文歌；否则有汉字就是中文歌；都没有就是纯外语。
    """
    has_kana = has_han = has_other = False
    for ln in doc.lines:
        if ln.kind != KIND_LYRIC:
            continue
        t = ln.text
        if RE_KANA.search(t):
            has_kana = True
        elif RE_HAN.search(t):
            has_han = True
        else:
            has_other = True

    if has_kana:
        return "ja"
    if has_han:
        return "zh"
    if has_other:
        return "other"
    return "unknown"


def _mark_translatable(doc: LyricDoc) -> None:
    """标记哪些行需要译文。

    规则（来自既有的手工实践）：
      - 中文歌里，**只有整行没有汉字的外语行**才加译文。中英混唱的句子里
        夹带的英文词不单独翻——那样会让中文句子被拆碎。
      - 日文/纯外语歌里，所有 lyric 行都翻。
    """
    for ln in doc.lines:
        if ln.kind != KIND_LYRIC:
            ln.needs_translation = False
            continue
        if doc.primary_lang == "zh":
            ln.needs_translation = not RE_HAN.search(ln.text)
        else:
            ln.needs_translation = True


def _mark_repeats(doc: LyricDoc) -> None:
    """标记重复行：文本完全相同的后续行，指向首次出现的位置。

    只标记待译行——不翻的行（拟声、中文行）重复与否没有意义。
    """
    first: dict[str, int] = {}
    for ln in doc.lines:
        if not ln.needs_translation:
            continue
        key = ln.text
        if key in first:
            ln.repeat_of = first[key]
        else:
            first[key] = ln.index


def looks_translated(doc: LyricDoc) -> bool:
    """判断这份歌词是不是**已经翻好的"原文+译文"交替**形态。

    为什么必须有这个：导入一首别处翻好的歌（旧流程翻的、别人给的），
    它没有备份。工具会当成未翻译再翻一遍，**在已有译文后面再插一条**，
    把歌词写坏——跟靠备份解决的那个幂等问题是同一个病。

    判据：拿"源语言行"和"中文行"的相邻关系数比例。
    未翻译的外语歌里，外语行后面跟的还是外语行，比例接近 0；
    翻好的歌里，外语行后面紧跟中文行，比例接近 1。

    注意日文歌：源行有假名，译文行有汉字无假名——两者都含汉字，
    所以不能只靠"含不含汉字"区分，必须看假名。

    这是启发式，宁可**放过**也不误判：误判成"已翻译"会让一首没翻的歌被跳过
    （看得见、可补救），漏判则会让译文叠加（写坏文件）。
    """
    # 判据刻意**不看"这首歌是什么语言"**。
    # 翻译后的文档里中外交混，`primary_lang` 会算成"zh"，
    # 拿它当开关会把翻好的英文歌全部漏掉（实测踩过）。
    #
    # 改看**交替模式**，两个条件同时成立才算：
    #   一、原文行占比 ≥ 35%。中文歌夹英文钩子时这个比例很低（实测 <15%）；
    #       翻好的外语歌接近 50%。这一条把中文歌的误判挡掉。
    #   二、原文行后面紧跟中文行的比例 ≥ 60%。未翻译的歌里外语行后面
    #       跟的还是外语行，这个比例接近 0。
    lines = [ln for ln in doc.lines if ln.kind == KIND_LYRIC and ln.text.strip()]
    if len(lines) < 6:
        return False

    def is_source(text: str) -> bool:
        """像不像"待翻译的原文行"：日文（有假名）或英文（有拉丁无汉字）。"""
        if RE_KANA.search(text):
            return True
        if RE_HAN.search(text):
            return False
        return bool(RE_LATIN.search(text))

    def is_target(text: str) -> bool:
        """像不像"中文译文行"：有汉字、无假名。"""
        return bool(RE_HAN.search(text)) and not RE_KANA.search(text)

    idx = [i for i, ln in enumerate(lines) if is_source(ln.text)]
    if not idx:
        return False
    if len(idx) / len(lines) < 0.35:
        return False

    matched = sum(
        1 for i in idx if i + 1 < len(lines) and is_target(lines[i + 1].text)
    )
    return matched / len(idx) >= 0.6


def _parse_offset(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return 0


def parse_file(path) -> LyricDoc:
    """从 .lrc / .txt 文件读取并解析（含编码嗅探）。"""
    from pathlib import Path

    raw = Path(path).read_bytes()
    text, encoding = sniff_decode(raw)
    return parse(text, encoding)


def parse_tag_text(text: str, force_translate: frozenset[str] | None = None) -> LyricDoc:
    """解析从 ©lyr 读出的文本（已经是 str，编码确定）。"""
    return parse(text, encoding="utf-8", force_translate=force_translate)


def load_force_translate(path) -> frozenset[str]:
    """读人工豁免清单：每行一条，内容与歌词行完全一致。

    空行和 # 开头的注释行忽略。文件不存在返回空集合，不报错。
    """
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return frozenset()
    out: set[str] = set()
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.add(s)
    return frozenset(out)


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    SAMPLE = """[ti:讨厌红楼梦]
[ar:陶喆]
[offset:+500]
[00:00.00]作词 : 张三
[00:01.00]编曲：李四
[00:02.00]
[00:03.00]Oh-oh
[00:04.00]La-la-la-la-la-la
[00:05.00]Say whoa-oh-oh-oh, whoa-oh-oh-oh
[00:06.00]Baby-baby-baby-baby, oh, baby-baby, oh, baby
[00:07.00]ㄅㄆㄇㄈㄉㄊㄋㄌ
[00:08.00](Oh oh, oh oh oh oh)
[00:09.00]Doo-doo-doo-doo-doo-doo
[00:10.00]Yeah, la-la-la-la-la, la
[00:11.00]纯音乐，请欣赏
[00:12.00]You're my love
[00:13.00]Yes, I do, do love you
[00:14.00]Man, I love my baby, ah
[00:15.00]So I
[00:16.00]
[00:17.00]我爱你 爱你
[00:18.00]I love you 我爱你
"""

    doc = parse_tag_text(SAMPLE)
    print(f"主体语言={doc.primary_lang}  有时间轴={doc.has_timestamps}  meta={doc.meta}\n")
    print(f"{'idx':>3} {'类型':<12} {'送翻':<5} 文本")
    for ln in doc.lines:
        mark = "✓" if ln.needs_translation else ""
        print(f"{ln.index:>3} {ln.kind:<12} {mark:<5} {ln.text!r}")

    print("\n送翻行：", doc.source_texts())

    # 回填演示
    trans = [f"【{i}】" for i in range(len(doc.source_texts()))]
    print("\n回填结果：")
    for row in doc.apply(trans):
        print("   ", repr(row))
