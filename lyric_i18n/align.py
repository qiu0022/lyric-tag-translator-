r"""对齐校验层——本产品的核心价值所在。

别的项目不做"把翻译写回内嵌歌词"，理由不是想不到，而是**不做校验就必然错位，
而错位的双语歌词比没有翻译更糟**（同类项目 songloft 明确写过这句话）。
所以真正的护城河不是"能翻译"，是"保证不错位"。

这一层只认**结构**，不认**语义**。
理由：风格要求（"忠于原曲""不要加戏"）本来就允许模型偏离字面，
任何基于词义的硬性检查都会大面积误杀。所以硬门槛全部是结构性指标。

盯五个真实失败模式，每一个都有来源：

  1. 合并/拆分行      → 行数不等
  2. 副歌偷懒          → "同上""略""repeat""〃"
     （实测：模型碰到连续重复行时会自作主张省略）
  3. 塌缩              → 相邻若干行原文不同，译文却完全相同
     最隐蔽：行数对得上，肉眼扫一眼也像那么回事，但整段副歌共享了同一句译文。
     语义检查查不出来，只能靠"原文不同而译文相同"这个结构特征。
  4. **模型拒答**      → 遇到露骨歌词时自作主张拒绝、说教或加免责声明。
     语料里有 CupcakKe，这条一定会踩到。宁可整曲失败，
     也不能把"抱歉，我无法翻译这段内容"写进用户的歌词标签。
  5. **加戏**          → 译文比原文长出好几倍，模型在替原文补逻辑、补语气。
     这是用户明确抱怨过的"个人风格味太重"。只报警不阻塞——
     因为"多长算长"没有硬标准，判断权留给人。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEV_ERROR = "error"
SEV_WARN = "warn"

# 加词检测的默认阈值
EXPAND_RATIO = 3.0
EXPAND_MIN_SRC_UNITS = 4


@dataclass
class Issue:
    code: str
    message: str
    severity: str = SEV_ERROR
    line_index: int | None = None

    def __str__(self) -> str:
        loc = f" 第{self.line_index + 1}行" if self.line_index is not None else ""
        tag = "错误" if self.severity == SEV_ERROR else "警告"
        return f"[{tag}]{loc} {self.message}"


# 偷懒标记。必须**整行**都是这个才算，避免误伤正文里的"省略""重复"等词。
_LAZY_TOKENS = {
    "同上", "同前", "如上", "见上", "略", "省略", "重复", "同上文", "同上句",
    "……", "...", "…", "repeat", "same", "same as above", "ditto", "idem",
    "as above", "unchanged", "do.", "〃", "同", "（同上）", "(同上)",
}

_STRIP_WRAP_RE = re.compile(r"^[\s\[\(（【\"'“”]+|[\s\]\)）】\"'“”]+$")
_LABEL_RE = re.compile(r"^\s*(译文|翻译|translation|trans)\s*[:：]", re.I)

# 拒答强信号：AI 自我指涉。正常歌词译文几乎不可能出现，命中即判死。
_REFUSAL_STRONG_RE = re.compile(
    r"(作为(一个)?\s*(AI|人工智能|语言模型|助手)"
    r"|(AI|人工智能|语言模型)的?(角度|身份|立场|能力)"
    r"|as an AI|I'?m sorry,? but"
    r"|I (cannot|can'?t|am unable to)\s+(assist|help|translate|provide|comply|fulfill)"
    r"|无法(为你|替你|协助|提供翻译|完成这个)"
    r"|违反(了)?(我的)?(使用)?(政策|准则|规定))",
    re.I,
)
# 拒答弱信号：单行命中不算，但大面积命中就是拒答。
_REFUSAL_WEAK_RE = re.compile(
    r"(抱歉|对不起|很遗憾|无法|不能|拒绝|不适当|不当内容|敏感内容"
    r"|I'?m sorry|I apologize|sorry,|inappropriate)",
    re.I,
)
# 原文里本来就存在的"道歉/请求"类词。
# 判断弱信号时必须拿它对照——否则会误杀歌词本身就是道歉的歌。
# 实测踩过：方大同《Sorry》整首都是 "I'm so sorry"，六行译文全含「抱歉」，
# 旧版本把它判成模型拒答，整首跳过。
_SRC_APOLOGY_RE = re.compile(
    r"(sorry|apolog|forgive|excuse|pardon|regret|beg\b)", re.I
)

RE_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
RE_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
_RE_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")


def units(text: str) -> int:
    """粗略计量"内容量"：汉字/假名一字算一个，拉丁按词算。标点不计。"""
    stripped = text.strip()
    if not stripped:
        return 0
    cjk = len(RE_HAN.findall(stripped)) + len(RE_KANA.findall(stripped))
    latin = len(_RE_LATIN_WORD.findall(stripped))
    return cjk + latin


def _norm_lazy(text: str) -> str:
    s = _STRIP_WRAP_RE.sub("", text.strip())
    return s.strip("。.！!？?~～ ").lower()


def validate_alignment(src: list[str], dst: list[str]) -> list[Issue]:
    """校验译文是否与原文结构对齐。src 是待译行，dst 是对应译文。"""
    issues: list[Issue] = []

    # 0. 先查拒答：行数可能刚好对上，但内容是"我无法翻译"
    issues.extend(_detect_refusal(src, dst))
    if has_errors(issues):
        return issues

    # 1. 行数（硬门槛，挂了就没必要往下查）
    if len(src) != len(dst):
        issues.append(
            Issue(
                "line_count",
                f"行数不一致：原文 {len(src)} 行，译文 {len(dst)} 行。模型很可能合并或拆分了行。",
            )
        )
        return issues

    if not src:
        return issues

    # 2. 逐行：空值、偷懒标记、结构破坏
    missing: list[int] = []
    for i, (s, d) in enumerate(zip(src, dst)):
        s_has, d_has = bool(s.strip()), bool(d.strip())
        if s_has and not d_has:
            missing.append(i)

        if d_has:
            if _norm_lazy(d) in _LAZY_TOKENS:
                issues.append(
                    Issue("lazy_marker",
                          f"译文是偷懒标记 {d.strip()!r}，重复行也必须逐行实译。",
                          SEV_ERROR, i)
                )
            if "\n" in d.strip():
                issues.append(
                    Issue("embedded_newline", "译文内部含换行，行结构被破坏。", SEV_ERROR, i)
                )
            if _LABEL_RE.match(d):
                issues.append(
                    Issue("label_prefix", f"译文带了多余前缀：{d.strip()[:30]!r}", SEV_WARN, i)
                )

    for i in missing[:5]:
        issues.append(Issue("missing_translation", "原文有内容但译文为空，疑似漏译。", SEV_ERROR, i))
    if len(missing) > 5:
        issues.append(Issue("missing_translation_more",
                            f"另有 {len(missing) - 5} 行漏译。", SEV_ERROR))

    # 3. 塌缩
    issues.extend(_detect_collapse(src, dst))

    # 4. 原样退回（模型没真翻译）
    if _looks_untranslated(src, dst):
        issues.append(Issue("not_translated", "译文与原文几乎完全相同，模型可能没有真正翻译。"))

    # 5. 加戏（只报警，不阻塞）
    issues.extend(_detect_expansion(src, dst))

    return issues


def _detect_refusal(src: list[str], dst: list[str]) -> list[Issue]:
    """识别模型的拒答/说教/免责声明。

    分两级：
    - **强信号**：AI 自我指涉（"作为一个人工智能"之类）。正常译文不可能出现，命中即判死。
    - **弱信号**：抱歉/无法/不能这类词大面积出现。
      这里**必须拿原文对照**——歌词本身就是道歉的歌（方大同《Sorry》整首
      "I'm so sorry"），译文里当然全是「抱歉」。不算拒答。
      只有"原文没有道歉词、译文却出现"才计入可疑。
    """
    filled = [d.strip() for d in dst if d.strip()]
    if not filled:
        return []

    for i, d in enumerate(dst):
        if d.strip() and _REFUSAL_STRONG_RE.search(d):
            return [
                Issue("refusal",
                      f"模型拒答或自我指涉：{d.strip()[:60]!r}。这类输出绝不能写进歌词标签。",
                      SEV_ERROR, i)
            ]

    considered = suspicious = 0
    for s, d in zip(src, dst):
        if not d.strip():
            continue
        considered += 1
        if _REFUSAL_WEAK_RE.search(d) and not _SRC_APOLOGY_RE.search(s):
            suspicious += 1

    if considered >= 2 and suspicious / considered >= 0.6:
        return [
            Issue("refusal_likely",
                  f"{suspicious}/{considered} 行含抱歉/无法等措辞，且原文并没有对应内容，"
                  f"疑似模型拒答或加免责声明。",
                  SEV_ERROR)
        ]
    return []


def _detect_collapse(src: list[str], dst: list[str]) -> list[Issue]:
    """相邻原文不同、译文却完全相同 → 模型把多行塌缩成一句。"""
    issues: list[Issue] = []
    i, n = 0, len(src)
    while i < n:
        j = i + 1
        while j < n and dst[j].strip() == dst[i].strip():
            j += 1
        if j - i >= 2 and dst[i].strip():
            variants = {s.strip() for s in src[i:j] if s.strip()}
            if len(variants) >= 2:
                issues.append(
                    Issue("collapse",
                          f"第{i + 1}~{j}行原文有 {len(variants)} 种不同内容，译文却全部相同"
                          f"（{dst[i].strip()[:24]!r}），疑似塌缩。",
                          SEV_ERROR, i)
                )
        i = j
    return issues


def _looks_untranslated(src: list[str], dst: list[str]) -> bool:
    pairs = [(s.strip(), d.strip()) for s, d in zip(src, dst) if s.strip()]
    if len(pairs) < 3:
        return False
    return sum(1 for s, d in pairs if s == d) / len(pairs) >= 0.9


def _detect_expansion(
    src: list[str], dst: list[str], ratio: float = EXPAND_RATIO
) -> list[Issue]:
    """译文比原文长太多 → 模型在替原文补逻辑、补语气（"个人风格味"）。

    只报警不阻塞：多长算长没有硬标准，判断权留给人。
    短行不做判断——两个词变六个词是正常的，不是加戏。
    """
    issues: list[Issue] = []
    for i, (s, d) in enumerate(zip(src, dst)):
        su = units(s)
        if su < EXPAND_MIN_SRC_UNITS:
            continue
        du = units(d)
        if su and du / su > ratio:
            issues.append(
                Issue("expansion",
                      f"译文疑似加戏：原文 {su} 个单位 → 译文 {du} 个（{du / su:.1f}×）。"
                      f"原文 {s.strip()[:30]!r} → {d.strip()[:40]!r}",
                      SEV_WARN, i)
            )
        if len(issues) >= 6:
            break
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == SEV_ERROR for i in issues)


def format_issues(issues: list[Issue], limit: int = 12) -> str:
    if not issues:
        return "  （无问题）"
    lines = [str(i) for i in issues[:limit]]
    if len(issues) > limit:
        lines.append(f"  ……另有 {len(issues) - limit} 条")
    return "\n".join(lines)


def corrective_message(issues: list[Issue], expected: int) -> str:
    """把校验失败转成给模型看的纠正指令（重试时追加）。"""
    bullets = "\n".join(f"- {i.message}" for i in issues if i.severity == SEV_ERROR)
    return (
        "上一次输出不合格，请修正后重新输出**完整**的 JSON。\n"
        f"必须恰好输出 {expected} 行译文。\n"
        f"问题清单：\n{bullets}\n"
        "再次强调：逐行翻译，行数与顺序严格一致，重复行也必须逐行实译，"
        "禁止任何省略形式、禁止评价内容、禁止加免责声明。只输出 JSON。"
    )


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    CASES: list[tuple[str, list[str], list[str]]] = [
        ("正常对齐",
         ["夜の風に揺れて", "君の名前を呼ぶ", "夜の風に揺れて"],
         ["在夜风中摇曳", "呼唤着你的名字", "在夜风中摇曳"]),
        ("行数不符", ["a", "b", "c"], ["甲", "乙"]),
        ("偷懒标记", ["夜の風に揺れて", "君の名前を呼ぶ"], ["在夜风中摇曳", "同上"]),
        ("塌缩",
         ["第一句", "第二句", "第三句", "第四句"],
         ["同样的译文", "同样的译文", "同样的译文", "同样的译文"]),
        ("模型拒答",
         ["line one", "line two"],
         ["抱歉，我无法翻译包含露骨内容的歌词。", "作为一个人工智能，我需要遵守相关规定。"]),
        ("正常含抱歉的行",
         ["ごめんね", "君の名前を呼ぶ", "夜の風に揺れて"],
         ["抱歉", "呼唤着你的名字", "在夜风中摇曳"]),
        ("加戏",
         ["Switch up my style", "Then she clean it with her face"],
         ["换什么风格都行，哪条道我都能走", "然后她用脸把它擦干净"]),
        # 回归：方大同《Sorry》整首都是 "I'm so sorry"，译文全是「抱歉」。
        # 旧版本把它判成模型拒答，整首跳过。原文有道歉词就不该算可疑。
        ("歌词本身是道歉（不该判拒答）",
         ["I'm so sorry", "Sorry that I hurt you", "I'm so sorry"],
         ["我很抱歉", "抱歉我伤害了你", "我很抱歉"]),
        # 真拒答但没用"作为AI"这种强信号，只靠弱信号也要能抓到
        ("疑似拒答（无 AI 自我指涉）",
         ["line one", "line two", "line three"],
         ["抱歉，我无法完成这个请求。", "我无法翻译这段内容。", "很抱歉。"]),
    ]
    for name, s, d in CASES:
        print(f"—— {name}")
        print(format_issues(validate_alignment(s, d)))
