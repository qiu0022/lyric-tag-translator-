# -*- coding: utf-8 -*-
"""clean 层回归测试。

锁住三件事：
  1. 拟声识别的**误判**（真歌词被判成不可译）—— 这是最危险的失败模式，
     因为它是静默的：那行不翻，没人会注意到。
  2. 拟声识别的**漏判**（拟声被送去翻译）—— 会产生「哦-哦」「啦-啦-啦」。
  3. 重复识别与一致性强制。
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import io
import sys

from lyric_i18n.clean import (
    KIND_VOCAL,
    looks_translated,
    parse_tag_text,
    restore_trailing_sound,
)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 真实库里出现过、必须判为【不可译】的行
MUST_BE_VOCAL = [
    "Oh-oh",
    "La-la-la-la-la-la",
    "Say whoa-oh-oh-oh, whoa-oh-oh-oh",
    "Baby-baby-baby-baby, oh, baby-baby, oh, baby",
    "ㄅㄆㄇㄈㄉㄊㄋㄌ",
    "(Oh oh, oh oh oh oh)",
    "Doo-doo-doo-doo-doo-doo",
    "Yeah, la-la-la-la-la, la",
    "Mmm",
    "Ooh, ooh, ooh",
    "No",
    "Na-na-na, na-na-na, na-na-na",
]

# 真实库里出现过、**绝不能**判为不可译的行（都是踩过的坑）
MUST_BE_LYRIC = [
    "I'm comin' home, home",          # 曾被"剥离前缀后重复"规则误杀
    "And I, I, I, I, I",              # 你翻过：而我，我，我，我，我
    "♡ yeah, i, i, i",                # hate that i made you love me 的副歌钩子
    "Come on",                        # 你翻过：来吧
    "ㄜ 小姐 請問一下 有沒有賣半島鐵盒",   # 周杰伦真歌词，曾被注音规则整行误杀
    "Man, I love my baby, ah",        # "baby" 不该触发拟声
    "Yes, I do, do love you",
    "You're my love",
    "Tap Tap Tap",                    # 重复单元 "tap" 是实义词，不该当拟声
    "Girl, yeah",                     # 只有两个词，不该当拟声
]

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if not cond:
        failures.append(label)


def kind_of(line: str) -> str:
    doc = parse_tag_text(line)
    return doc.lines[0].kind


print("=== 必须判为不可译 ===")
for line in MUST_BE_VOCAL:
    k = kind_of(line)
    ok = k == KIND_VOCAL
    check(ok, f"应判 vocal 实为 {k}: {line!r}")
    print(f"  {'✓' if ok else '✗'} {k:<10} {line!r}")

print("\n=== 必须判为歌词 ===")
for line in MUST_BE_LYRIC:
    k = kind_of(line)
    ok = k == "lyric"
    check(ok, f"应判 lyric 实为 {k}: {line!r}")
    print(f"  {'✓' if ok else '✗'} {k:<10} {line!r}")

print("\n=== 重复识别 ===")
SAMPLE = "\n".join([
    "Look what you've done",
    "I'm a motherfuckin' starboy",
    "Look what you've done",          # 重复
    "I'm a motherfuckin' starboy",    # 重复
    "Something brand new here",
    "Look what you've done",          # 重复
])
doc = parse_tag_text(SAMPLE)
check(doc.translatable_count == 6, f"待译行应为 6，实为 {doc.translatable_count}")
check(len(doc.unique_translatable()) == 3, f"唯一行应为 3，实为 {len(doc.unique_translatable())}")
check(doc.repeat_saving == 3, f"应省 3 行，实为 {doc.repeat_saving}")
print(f"  待译 {doc.translatable_count} → 唯一 {len(doc.unique_translatable())}，省 {doc.repeat_saving}")
print(f"  唯一行：{doc.unique_translatable()}")

print("\n=== 一致性强制（模拟模型在副歌处漂移）===")
drifted = ["看看你干的好事", "老子就是他妈的巨星", "看看你做了什么", "我是该死的巨星", "全新的东西", "瞧瞧你的杰作"]
fixed_list, n_fixed = doc.enforce_consistency(drifted)
print(f"  模型原始输出：{drifted}")
print(f"  强制统一后  ：{fixed_list}")
print(f"  修正处数    ：{n_fixed}")
check(n_fixed == 3, f"应修正 3 处，实为 {n_fixed}")
check(fixed_list[0] == fixed_list[2] == fixed_list[5], "重复行译文未统一")
check(fixed_list[1] == fixed_list[3], "重复行译文未统一")

print("\n=== 人工豁免（force_translate）===")
d2 = parse_tag_text("Tap Tap Tap\nOh-oh", force_translate=frozenset({"Tap Tap Tap"}))
kinds = [(ln.text, ln.kind) for ln in d2.lines]
print(f"  {kinds}")
check(d2.lines[0].kind == "lyric", "豁免行应被当作歌词")
check(d2.lines[1].kind == KIND_VOCAL, "未豁免的拟声应保持不可译")

print("\n=== 译文与原文相同 → 不输出译文行（第二道保险）===")
# 模拟：某个拟声词漏过了 is_vocal 被送去翻译，模型按提示词要求原样返回。
d3 = parse_tag_text(
    "You're my love\nBaby-baby-baby-baby, oh, baby\nYes, I do, do love you",
    force_translate=frozenset({"Baby-baby-baby-baby, oh, baby"}),
)
trans = ["你是我的爱", "Baby-baby-baby-baby, oh, baby", "是的，我真的爱你"]
rows = d3.apply(trans)
print(f"  输入 {[l.text for l in d3.lines]}")
print(f"  模型返回 {trans}")
print(f"  实际输出 {rows}")
check(
    rows.count("Baby-baby-baby-baby, oh, baby") == 1,
    "与原文相同的译文行不应被重复输出",
)
check("你是我的爱" in rows, "正常译文应保留")
check("是的，我真的爱你" in rows, "正常译文应保留")

print("\n=== 已翻译检测（导入外部成品时避免重复翻译）===")
# 未翻译的英文歌：原文行后面跟的还是原文行 → 不是双语
EN_PLAIN = "\n".join([
    "i can't tell you why", "but something inside", "is dancing with fire",
    "eyes lit like the sky", "turned tears into diamonds", "got good at goodbyes",
])
# 翻好的英文歌：原文行后面紧跟中文行 → 是双语
EN_DONE = "\n".join([
    "i can't tell you why", "我说不清为什么",
    "but something inside", "但心里有什么",
    "is dancing with fire", "在跟火共舞",
    "eyes lit like the sky", "双眼亮如天空",
])
# 中文歌夹英文钩子（**未翻译**）：英文行占比很低，不能判成双语。
# 这是实测踩过的误判：方大同《Sorry》、陶喆《多谢你》都被误判过。
ZH_WITH_HOOKS = "\n".join([
    "當我回頭 發現是我", "傷妳最多 欠妳最多", "曾經擁有 一種幸福",
    "當妳流淚 還問妳到底 想要什麼", "I'm so sorry", "我現在知道妳傷心 有同樣的心情",
    "I'm so sorry", "我現在終於能明白", "愛能溫柔 愛能殘酷",
    "愛的自私 愛的自由", "這些我都走過 妳的痛 我現在也都有",
])
# 未翻译的日文歌
JA_PLAIN = "\n".join([
    "無我夢中で踊る", "ぎこちないステップ刻んだ", "擦り切れる日々の喧騒も",
    "この声で響かせて", "ほら 夜空に瞬く青い彗星も", "トリコにするの",
])

TRANS_CASES = [
    ("未翻译的英文歌", EN_PLAIN, False),
    ("翻好的英文歌", EN_DONE, True),
    ("中文歌夹英文钩子（未翻）", ZH_WITH_HOOKS, False),
    ("未翻译的日文歌", JA_PLAIN, False),
]
for label, text, want in TRANS_CASES:
    got = looks_translated(parse_tag_text(text))
    ok = got == want
    check(ok, f"已翻译检测：{label} 期望 {want}，实为 {got}")
    print(f"  {'✓' if ok else '✗'} {label:<24} → {got}")

print("\n=== 行尾语气词还原成英文原样 ===")
SOUND_CASES = [
    # (原文, 模型译文, 期望)
    ("I'm tryna put you in the worst mood, ah", "我想让你心情跌到谷底，啊",
     "我想让你心情跌到谷底，ah"),
    ("Made your whole year in a week too, yeah", "一周就赚够你一整年的钱，yeah",
     "一周就赚够你一整年的钱，yeah"),          # 模型已保留 → 不动
    ("I'm tryna put you in the worst mood, ah", "我想让你心情跌到谷底，ah",
     "我想让你心情跌到谷底，ah"),               # 已保留 → 不动
    ("Every day, a nigga try to test me, ah", "每天都有人想试我，啊",
     "每天都有人想试我，ah"),
    # 以下两个**不该**被改：no 是有实义的词，不是语气词
    ("I said no", "我说不", "我说不"),
    ("You don't say", "你别说了", "你别说了"),
    # 句尾不是语气词
    ("Look what you've done", "看看你干了什么", "看看你干了什么"),
]
for src, dst, want in SOUND_CASES:
    got = restore_trailing_sound(src, dst)
    ok = got == want
    check(ok, f"语气词还原：{src!r} + {dst!r} → 期望 {want!r}，实为 {got!r}")
    print(f"  {'✓' if ok else '✗'} {dst!r} → {got!r}")

print()
if failures:
    print(f"❌ {len(failures)} 项失败：")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
