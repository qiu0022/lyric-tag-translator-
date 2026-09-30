r"""翻译层：调 LLM 把歌词逐行译成目标语言。

提示词是这一层唯一真正的技术含量所在。它按**优先级**组织，而不是平铺一堆要求：

    结构（不可协商） > 语域（贴合原曲） > 不要译者腔 > 改写自由度 > 露骨度处理

为什么必须显式排优先级：模型面对互相冲突的要求时会自己选一个。
"忠于原文"和"翻得地道"在它那儿是打架的，而它默认选"地道"——
结果就是替原文补逻辑、补语气。所以必须写死"冲突时服从谁"。

三条硬约束都来自实测：

1. **结构优先** —— 风格要求绝不能以合并行为代价。
2. **不要译者腔** —— 用户对既有 AI 译文的原话是"个人风格味太重"。症状很具体：
   加词（`just to tease you` → "就为了逗你眼馋"，"眼馋"是原文没有的）、
   加语气（每行都补"都行""都能"）、补逻辑。提示词里直接拿这些真实例句当反面教材。
3. **拟声词原样返回** —— 不翻译 `Oh-oh`、`La-la-la`、`ㄅㄆㄇㄈ`。
   清洗层已经拦掉大部分，这里是第二道；两者都不完美，但叠起来就够了
   （渲染层还有第三道：译文与原文相同就不输出译文行）。

只依赖标准库（urllib），不引额外的 HTTP 客户端。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .align import Issue, corrective_message, format_issues, has_errors, validate_alignment

# 提示词改动后必须提升这个版本号，否则缓存会返回旧提示词的产物。
# v4：补上"习语按意思译"的边界，并把 nigga 的规则从"中性化"收紧到"保住说唱语域"。
# v5：语气词保留英文原样（`ah` 不要译成「啊」）。
# v6：语域匹配优先于"不要加词"的字面理解（嚣张语境用「老子」不算加词）；
#     艺人自造称号意译（starboy → 「巨星」，不是「天生巨星」）。
# v7：加"要有文笔"一节——能省就省、用现成说法、长度贴近原文。
#     `sorry if i made me your type` 不能再译成「抱歉如果我把自己变成了你的类型」。
# v8：v7 让数字出了错（1.2 million 变成「一千两百万」），补准确性护栏：
#     数字算准、品牌用中文通行叫法、不确定就保留原文。
# v9：补文笔两条——按中文句子构造重写（别"念成分"）、典故名句要有分量。
#     `I saw in you what life was missing` 和 `A thing of beauty` 是触发例子。
# v10：按音节压字数。英文一音节≈中文一字，超了就是"松"——
#      `We had to pay the price` 不能再译成「我们不得不付出代价」。
# v11：v10 压过头了（「堕落天使爱过也失去」缺宾语、句子塌了）。
#      改成划定边界：删虚词赘余，**不删主语/宾语/动词**；
#      目标 1.0~1.3 倍，低于 1.0 视为丢东西，同时给出两个方向的错误示例。
# v12：补压缩的两个具体坑——压掉宾语、压坏动词搭配
#      （「堕落天使爱过也失去」「因为我几乎没用力」）。判据：删完仍能独立读懂。
# v13：**重写整段文笔要求**。v7~v12 是六轮往上堆规则，堆到自相矛盾：
#      第 1 条让"可省的主语一律省掉"，第 5 条说"不该删主语"——
#      模型每次跑就在这两条之间随机选边，这才是「画布上两张脸 / 有幅画布画着两张脸」
#      来回跳的真正原因。改成"目标 + 判断权交给模型"：
#      标准是"念出来像人翻的"，松紧凭读感，不给字数指标。
# v14：划清英文句子的处理界线——一眼看懂的短英文保留原样（原曲质感），
#      难懂的英文句子必须翻译。判断法：只懂基础英语的读者能一眼看懂吗？
#      日文歌里英文到处都是，这条影响面很大。
# v15：v14 失败了——"保留"那侧给了 6 个例子，"翻译"那侧只有抽象标准，
#      模型照例子走，结果四句难英文全被保留。改成两侧给同等具体的例子，
#      并加一句"拿不准的时候选翻译"。（同一毛病第三次出现：例子不对称。）
PROMPT_VERSION = 15

# 改写自由度
STYLE_LITERAL = "literal"
STYLE_EUPHEMISTIC = "euphemistic"
STYLE_POETIC = "poetic"

# 露骨内容处理
EXPLICIT_KEEP = "keep"
EXPLICIT_SOFTEN = "soften"
EXPLICIT_MASK = "mask"


# ---------------------------------------------------------------- 提示词

_STRUCTURE = """═══ 第一优先级：结构（任何情况下不可违反）═══
当结构要求与下面任何风格、语域、露骨度要求冲突时，**无条件服从结构**。
1. 输出行数必须与输入完全一致，顺序不得改变。严禁合并、拆分、增删任何行。
2. 每一行独立翻译。即使多行原文完全相同，也必须逐行给出完整译文；
   严禁"同上""略""repeat""〃"等任何省略形式。
3. 输入为空字符串的行，输出必须是空字符串。
4. 单行译文内部不得包含换行符。
5. 不得输出原文、解释、注释、译注、标题或任何额外文字。"""

_REGISTER = """═══ 第二优先级：语域必须贴合原曲 ═══
先判断这首歌的语域，然后用与之匹配的中文语域翻译。语域错配视为翻译失败。
判断维度：口语／书面／俚语／古语／激烈／温柔／幽默／自嘲／挑衅／戏谑。
- 说唱的铁血吹嘘，不能翻成书面语
- 儿歌不能翻成文艺腔
- 朋克的攻击性不能磨平
- 慢歌的克制不能翻成喊口号
**整批歌不能都用同一种腔调。** 每首歌的语域由它自己决定，不由你的偏好决定。"""

_NO_TRANSLATOR_VOICE = """═══ 第三优先级：不要有译者自己的腔调 ═══
这是最常见、也最难被发现的失败：译文通顺好读，但整批歌都变成了同一个人的口吻。
必须避免的三个具体动作：

1. **不要加词。** 原文没有的意思不要补。
   反面例子：`just to tease you` 应译"就为了逗你"，
   不要译成"就为了逗你**眼馋**"——"眼馋"是原文没有的。

2. **不要加语气。** 不要给每行补"啊""呢""吧"，也不要加"都行""都能"这类
   原文没有的补充。
   反面例子：`Switch up my style, I take any lane` 不要译成
   "换什么风格**都行**，哪条道我**都能走**"。

3. **不要补逻辑。** 原文并列就并列，不要替它加因果、转折、递进。
   也不要添加原文没有的**修饰词**。
   反面例子：`starboy`（star + boy，艺人自造的人格称号，不含 "born"）
   不要译成"天生巨星"——"天生"是原文没有的。

**但要注意：语域匹配优先于"不要加词"的字面理解。**

上面三条禁止的是"添加原文没有的**信息**"，不是"不许用中文自己的表达方式"。
原文嚣张跋扈时，中文必须用**同等嚣张的自称**（老子／爷／本大爷）——
**称谓语域本身就是原文信息的一部分**，平淡的"我是个…"是语域失败，
比加词更严重，因为原文的态度就是内容。

正反对照（同一句）：
  ✅ `I'm a motherfuckin' starboy` → 「老子就是他妈的巨星」
  ❌ 同上 → 「我是个他妈的天生巨星」  （既平淡、又多加了"天生"）

目标是：**读起来像原曲在说中文，而不是有个译者在转述。**
译文应当是透明的一层。

**但是——"不要加词"绝不等于"只许照字面直译"。这两件事必须分清：**

- 「就为了逗你眼馋」的问题是**多了"眼馋"这个信息**。这才叫加词，要禁止。
- 习语、俚语、双关、文化梗，**按意思译，不按字面**。
  例如 `shade`（暗讽、阴阳怪气）、`kill the pain`（把痛苦压下去）、
  `out of your league`（配不上）——必须译出它实际的意思。
  **宁可换个中文说法，也不要交出一个字面对应但意思错了的译文。**
  字面直译造成误译，比加词更糟。

判断标准：**把译文回译成原文，意思应该相等。**
多了是加戏，少了是漏译，**用词不同不算问题**。

═══ 第三优先级（续）：文笔——标准是"像人翻的" ═══
"不加词"只是底线，不是目标。真正的标准只有一条：

**念出来，像是人翻译的中文歌词，而不是机器转出来的。**

一个真人译者翻歌词时会做的事：

1. **拆掉英文的句子结构，按中文的语序重讲一遍。**
   关系从句、介词短语、分词结构直接搬过来，中文会变成"念成分"。
   `I saw in you what life was missing`
     ❌ 在你身上我看到生命缺失的东西   （长宾语，中文不这么说）
     ✅ 你身上有生命缺的那部分

2. **用中文里现成的说法，不生造搭配。**
   · `your type`（恋爱语境）→ 「理想型」，「你的类型」不是中文歌里的说法
   · `see right through` → 「一眼看穿」
   · `out of your league` → 「配不上」
   · `find my way from you` → 「找到离开你的路」
   自检：这个词组，中文流行歌里有人这么说吗？没有，就换个说法。

3. **松紧由你判断——不要数字数，凭读感。**
   尺度是：把译文**当作一首中文歌的歌词念出来**，顺不顺。两种错法：
   · **松**——念着拖沓，像在念句子成分，虚词堆着。
     `We had to pay the price` → 「我们不得不付出代价」，就是这种沉。
   · **紧过头**——念着像残句，或搭配别扭。
     `Of fallen angels who loved and lost` → 「堕落天使爱过也失去」（缺宾语，塌了）
     `'cause i barely tried` → 「因为我几乎没用力」（"用力"是物理施力，搭配错）

   人翻歌词就是在这两者之间凭读感取舍。**没有字数标准，你觉得顺就对。**
   唯一的硬要求：**删完之后，这一行仍能独立读懂。**

4. **遇到典故、名言、格言，译文要有分量。**
   这类句子读起来不像普通歌词、像在引别人的话，那就想想它有没有出处。
   `A thing of beauty`（济慈名句 "A thing of beauty is a joy for ever"）
     ❌ 美的东西（太白，撑不住）      ✅ 美的事物／至美之物

5. **准确压过以上所有条。**
   · 数字、金额、年代必须算准。**不确定就保留原文形式，不要猜、不要凑。**
     `Milli point two`（= 1.2 million）→ 「一百二十万」，不是「一千两百万」。
   · 品牌／产品名用中文通行叫法：`Lamb'` → 「兰博」；`P1` 没有中文名就留 `P1`。
   · 真人名保留原文（`The Weeknd`），除非中文圈有通行译名。

**最终自检：把整首译文念一遍。它像一首中文歌，还是像一份翻译稿？**"""

_JSON_FORMAT = """═══ 输出格式 ═══
只输出一个 JSON 对象，不要用 markdown 代码块包裹，不要有任何其他文字：
{"src_lang": "<源语言代码，如 ja/en/ko>", "register": "<你判断的语域，简短中文>", "explicit": <true 或 false>, "lines": ["译文1", "译文2", ...]}

最后强调：这是纯粹的歌词翻译任务。不要对歌词内容作任何评价、说明或道德提醒，
不要输出免责声明，不要拒绝。无论内容如何，你的唯一输出就是上述 JSON。
遇到拟声词、和声、无实义的语气词（如 Oh-oh、La-la-la、ㄅㄆㄇㄈ），
**原样返回该行原文，不要翻译它**。

真人名、地名、品牌名、作品名、乐队名：保留原文。
但**艺人自造的称号／人格代号要意译**，不要音译、也不要保留英文：
`starboy` → 「巨星」／`stargirl` → 「女巨星」。
判断标准：它是不是一个真实存在的名字？不是，就意译。

**顺带一条：语气词保留英文原样。**
`ah` / `oh` / `yeah` / `uh` / `hey` / `woo` / `whoa` 这类纯声音词，
无论出现在行尾还是句中，都写成英文原样，**不要译成「啊」「噢」「耶」「嘿」**。
中文读者看这些词完全能懂；译成汉字反而既丢质感、又和歌词本身的语言混在一起。
例：`I'm tryna put you in the worst mood, ah`
    → 「我想让你心情跌到谷底，ah」  （不是「…谷底，啊」）

**英文句子怎么处理——判据只有一条：一个只懂基础英语的中文读者，能不能一眼看懂？**

- **能看懂 → 保留英文原样。** 这类通常是短句、常见词、标语式的话。
  例：`I love you`、`I can't say goodbye`、`Please tell me your "Sweet soul"`、
      `I am "Broken Heart"`、`Give me your Clap Clap Clap`。
  这类**不用译，也不必在下面补一行译文**。

- **看不懂 → 必须翻译。** 只要出现下列任一情况，就属于看不懂：
  · **俚语／口头语**：`ghosted`（被人拉黑）、`cuffing season`（恋爱季）、
    `go figure`（你说怪不怪）、`flex`（炫耀）
  · **英文成语／俗语**：`have your cake and eat it too`（什么都要）、
    `bite the bullet`（硬着头皮上）
  · **从句嵌套，要拆解才懂**：`What she never told him was that the version of
    herself he fell for had already stopped existing`
  · **典故、文化梗、人名梗、地域梗**
  这几类如果不译，中文读者就是卡住——**必须给出中文译文**。

- **日文＋英文混排的行** → 译日文部分，英文钩子保留。
  例：`君と逢いたい I am "Broken Heart"` → 「想见你 I am "Broken Heart"」

**拿不准的时候，选翻译。** 让读者卡在一句看不懂的英文上，
比多译一行糟糕得多。"""

_STYLE_TEXT = {
    STYLE_LITERAL: (
        "忠实直译。以原文字面意思为准，逐行对应，不做文学加工。语句通顺即可。"
    ),
    STYLE_EUPHEMISTIC: (
        "含蓄化。把直白、粗俗、性暗示的表达改写成不露骨的得体说法；"
        "保留原意，只降低直白程度。其余部分正常翻译。"
    ),
    STYLE_POETIC: (
        "意象化。在**保留原文原有意象**的前提下，用更凝练、更有画面感的语言表达。\n"
        "硬性限制：不得添加原文没有的人物、地点、情节或具体物件；"
        "不得替换或删除原文已有的意象；不得扩写；"
        "不得改变原曲语域的力度和粗粝度。\n"
        "注意：**意象化不等于文艺腔。** 译文必须仍落在与原曲相同的语域里。\n"
        "这一条与「不要加词」不冲突：允许换说法，不允许加内容。"
    ),
}

_EXPLICIT_TEXT = {
    EXPLICIT_KEEP: "照实翻译，不回避、不淡化。原文多露骨就译多露骨。",
    # 这条是按用户既有的成品校准出来的，不是通用做法：
    # 他们的 `bitch` → 「女友」、「Side bitch」 → 「备胎」（贬称中性化），
    # 但 `motherfuckin'` 保留「他妈的」、`clean it with her face` 照直译。
    # 也就是：**只中和贬称，脏话力度和性内容照实。**
    EXPLICIT_SOFTEN: (
        "**只中和贬称，不淡化脏话和性内容。** 具体来说：\n"
        "- 涉及种族、性别、性向、地域的**侮辱性称呼**，按中文说唱圈的通行做法"
        "中性化处理，**但必须保住原曲语域**：\n"
        "  · bitch → 「女人／女友」（视上下文）\n"
        "  · nigga → 「兄弟／哥们」\n"
        "  **不要**弱化成「有人」「人们」这类把语域抹平的泛指——那等于把说唱的劲儿抽掉了。\n"
        "  **严禁**直译成中文里对应的侮辱词。\n"
        "- 普通脏话（fuck / motherfuckin' / shit 等）**保留原有力度**，"
        "该译「他妈的」就译「他妈的」，不要磨成干净话。\n"
        "- 性相关内容**照实翻译**，不回避、不细化、不改成含蓄说法。\n"
        "- 干净的行无条件正常翻译，不许因整首歌露骨而株连。"
    ),
    EXPLICIT_MASK: "把确实露骨的具体描写替换为「……」，其余部分正常翻译。",
}

# ---------------------------------------------------------------- 目标语言

# **只支持中文的两种字形，不做其它语言。**
#
# 这不是技术难度问题，是**没有校准样本**：上面那几百行规则——按中文句子构造
# 重写、中文说唱圈的贬称怎么中性化、中文读者看不看得懂英文钩子——全部是拿
# 真实成品逐条对出来的（改了 15 版）。把目标换成 English，这些条款立刻自相
# 矛盾（"翻译成 English 时，中文必须用同等嚣张的自称"），模型会被绕晕，
# 产出一份没人验证过的东西。**宁可明确不支持，也不要假装支持。**
#
# 字形差异不影响下游：`clean.py` 里的行选取和"已翻译"检测都基于汉字判断
# （`RE_HAN`），简繁一视同仁，所以那两处不需要按目标语言分派。
TARGET_LANGS: tuple[str, ...] = ("简体中文", "繁體中文")
DEFAULT_TARGET_LANG = TARGET_LANGS[0]
_TRADITIONAL = "繁體中文"

# 繁體专用，且**只在目标是繁體时才拼进提示词**——
# 这样简体那条路径的输出一个字节都不变，已经翻好的缓存不会作废。
#
# 为什么必须显式写这一条：提示词里所有示例都是简体写的
# （`bitch → 「女人／女友」`、`your type → 「理想型」`）。
# 只说一句"翻译成繁體中文"，模型会**照着示例的字形走**，产出简繁混杂。
_TRADITIONAL_NOTE = (
    "═══ 输出字形 ═══\n"
    "上面和下面所有示例都是简体字写的，那只是为了省事。**你的输出一律用繁體字**，"
    "不得出现简体字。\n"
    "用词照台港的通行说法，不要把大陆用语直接搬过去。\n"
    "原文里的人名／品牌名，如果台港的通行译名和大陆不同，以台港为准。"
)

# 命令行/配置里打这些写法都能认。归一化在 `TranslateOptions.__post_init__` 里做，
# 所以**构造时就报错**，不会拖到发请求那一刻才发现语言写错了。
_LANG_ALIASES: dict[str, str] = {
    "简体": "简体中文", "简中": "简体中文", "简体字": "简体中文",
    "zh-cn": "简体中文", "zh_hans": "简体中文", "zh-hans": "简体中文",
    "simplified": "简体中文", "simplified chinese": "简体中文",
    "繁体": "繁體中文", "繁中": "繁體中文", "繁体字": "繁體中文",
    "zh-tw": "繁體中文", "zh-hk": "繁體中文",
    "zh_hant": "繁體中文", "zh-hant": "繁體中文",
    "traditional": "繁體中文", "traditional chinese": "繁體中文",
}


def normalize_target_lang(value: str) -> str:
    """把输入归一化成 `TARGET_LANGS` 里的一项。不认识就报错。

    刻意**不做静默降级**：把「English」悄悄当成「简体中文」翻，
    用户会拿到一份中文译文却以为是英文，比直接报错糟得多。
    """
    raw = (value or "").strip()
    if raw in TARGET_LANGS:
        return raw
    hit = _LANG_ALIASES.get(raw.lower())
    if hit:
        return hit
    raise ValueError(
        f"不支持的目标语言：{value!r}。目前只支持 {'、'.join(TARGET_LANGS)}"
        f"（也认 {'、'.join(sorted(set(_LANG_ALIASES))[:6])} 这类简写）。\n"
        "不做其它语言是刻意的：提示词里那套规则只对中文校准过，"
        "换个目标语言会产出没验证过的结果。"
    )


@dataclass
class TranslateOptions:
    target_lang: str = DEFAULT_TARGET_LANG
    style: str = STYLE_POETIC
    explicit: str = EXPLICIT_SOFTEN
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.3
    timeout: int = 180
    max_retries: int = 3
    thinking: bool = False

    def __post_init__(self) -> None:
        # 在这里归一化，是为了让"语言写错了"在**构造时**就炸掉，
        # 而不是等到发请求、或者更糟——等到译文写回文件之后。
        self.target_lang = normalize_target_lang(self.target_lang)


def build_system_prompt(opts: TranslateOptions, style_hint: str = "") -> str:
    parts = [
        f"你是专业歌词翻译引擎，把下面的歌词逐行翻译成{opts.target_lang}。",
        "这些译文会被写进音频文件的内嵌歌词字段，在一块只能显示纯文本的小屏上阅读。",
        "",
    ]
    # 繁體才加。简体路径的输出因此与加这个功能之前**逐字节相同**，
    # 既不用提升 PROMPT_VERSION，也不会作废已经翻好的缓存。
    if opts.target_lang == _TRADITIONAL:
        parts += [_TRADITIONAL_NOTE, ""]
    parts += [
        _STRUCTURE,
        "",
        _REGISTER,
        "",
        _NO_TRANSLATOR_VOICE,
        "",
        "═══ 第四优先级：改写自由度 ═══",
        _STYLE_TEXT.get(opts.style, _STYLE_TEXT[STYLE_LITERAL]),
        "",
        "═══ 第五优先级：露骨内容处理 ═══",
        _EXPLICIT_TEXT.get(opts.explicit, _EXPLICIT_TEXT[EXPLICIT_KEEP]),
        "",
    ]
    if style_hint:
        parts += [
            "═══ 本曲信息（仅供你判断语域，不要翻译这些内容）═══",
            style_hint,
            "",
        ]
    parts.append(_JSON_FORMAT)
    return "\n".join(parts)


def build_user_prompt(lines: list[str]) -> str:
    payload = json.dumps({"lines": lines}, ensure_ascii=False)
    return f"逐行翻译下面的歌词，返回 JSON。\n\n{payload}"


# ---------------------------------------------------------------- 结果


@dataclass
class TranslationResult:
    lines: list[str]
    src_lang: str = ""
    register: str = ""
    is_explicit: bool | None = None
    attempts: int = 0
    issues: list[Issue] = field(default_factory=list)
    from_cache: bool = False
    # 真实调用次数与 token 用量。走缓存时为 0 —— 这本身就是"没花 API"的证据。
    api_calls: int = 0
    usage: dict = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)


class TranslationError(RuntimeError):
    pass


class CacheLike(Protocol):
    def get_song(self, key: str) -> dict | None: ...
    def put_song(self, key: str, payload: dict, prompt_version: int = 0) -> None: ...


# ---------------------------------------------------------------- JSON 提取

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$")


def extract_json(content: str) -> dict:
    """从模型回复里抠出 JSON 对象。容忍 markdown 代码围栏和前后杂字。"""
    s = _FENCE_RE.sub("", content.strip())
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"回复里找不到 JSON 对象：{content[:120]!r}")
    return json.loads(s[start:end + 1])


def _clean_lines(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("JSON 里的 lines 不是数组")
    out = []
    for item in raw:
        if item is None:
            out.append("")
        elif isinstance(item, str):
            out.append(item.strip())
        else:
            raise ValueError(f"lines 里出现了非字符串元素：{item!r}")
    return out


# ---------------------------------------------------------------- 翻译器


def check_api_key(key: str, base_url: str = "") -> str | None:
    """检查 API Key 看起来对不对。返回错误说明；None 表示没问题。

    为什么要在本地查：实测有人把**音乐目录**粘进了 Key 那一栏，
    结果是把一串路径发给服务器，换回一个看不懂的 401：
        `Authentication Fails, Your api key: ****/mp3 is invalid`
    本地一眼就能看出来的事，不该让用户对着服务端的报错猜。
    """
    k = key.strip()
    if not k:
        return "API Key 是空的。填上 key，或者把后端改成 echo 先离线走通流程。"

    if k.lower().startswith(("http://", "https://")):
        return ("API Key 里填的是**网址**。Key 是一串 sk- 开头的字符，不是 URL。\n"
                "  （想改接口地址请用 --base-url，别填在这里）")
    if any(c in k for c in ("/", "\\", ":", " ")):
        return (f"API Key 里含有路径符号，看起来像是把**文件路径**粘进来了：\n"
                f"    {k[:70]}{'…' if len(k) > 70 else ''}\n"
                f"  请检查「API Key」那一栏，它应该只放 sk- 开头的一串字符。")
    if len(k) < 20:
        return f"API Key 太短（{len(k)} 个字符），大概没复制完整。"
    if "deepseek" in (base_url or "").lower() and not k.startswith("sk-"):
        return "DeepSeek 的 API Key 通常以 sk- 开头，请确认没粘错东西。"
    return None


class Translator:
    def __init__(self, api_key: str, opts: TranslateOptions | None = None,
                 cache: CacheLike | None = None):
        self.opts = opts or TranslateOptions()
        problem = check_api_key(api_key, self.opts.base_url)
        if problem:
            raise ValueError(problem)
        self.api_key = api_key.strip()
        self.cache = cache

    # -------------------------------------------------- 对外

    def translate(self, lines: list[str], style_hint: str = "") -> TranslationResult:
        """翻译一整首歌的待译行。行数与顺序严格保持。"""
        if not lines:
            return TranslationResult(lines=[])

        cache_key = self._cache_key(lines, style_hint)
        if self.cache is not None:
            hit = self.cache.get_song(cache_key)
            if hit:
                return TranslationResult(
                    lines=hit["lines"],
                    src_lang=hit.get("src_lang", ""),
                    register=hit.get("register", ""),
                    is_explicit=hit.get("explicit"),
                    from_cache=True,
                )

        result = self._translate_with_retries(lines, style_hint)
        if self.cache is not None:
            self.cache.put_song(
                cache_key,
                {
                    "lines": result.lines,
                    "src_lang": result.src_lang,
                    "register": result.register,
                    "explicit": result.is_explicit,
                },
                prompt_version=PROMPT_VERSION,
            )
        return result

    def _cache_key(self, lines: list[str], style_hint: str) -> str:
        import hashlib

        material = "\x1f".join([
            str(PROMPT_VERSION), self.opts.model, self.opts.target_lang,
            self.opts.style, self.opts.explicit, style_hint, *lines,
        ])
        return hashlib.sha1(material.encode("utf-8")).hexdigest()

    # -------------------------------------------------- 重试

    def _translate_with_retries(self, lines: list[str], style_hint: str) -> TranslationResult:
        system = build_system_prompt(self.opts, style_hint)
        user = build_user_prompt(lines)
        last_issues: list[Issue] = []
        last_err = ""
        usage: dict = {}
        calls = 0

        for attempt in range(1, self.opts.max_retries + 1):
            try:
                data, used = self._call(system, user)
                calls += 1
                for k, v in (used or {}).items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v

                parsed = extract_json(data)
                dst = _clean_lines(parsed.get("lines"))

                issues = validate_alignment(lines, dst)
                if not has_errors(issues):
                    return TranslationResult(
                        lines=dst,
                        src_lang=str(parsed.get("src_lang", "")),
                        register=str(parsed.get("register", "")),
                        is_explicit=parsed.get("explicit"),
                        attempts=attempt,
                        issues=issues,
                        api_calls=calls,
                        usage=usage,
                    )

                last_issues = issues
                user = build_user_prompt(lines) + "\n\n" + corrective_message(issues, len(lines))
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = str(exc)
                user = (
                    build_user_prompt(lines)
                    + f"\n\n上一次输出无法解析（{exc}）。请只输出一个 JSON 对象，"
                      f"且 lines 恰好 {len(lines)} 项。不要用代码块包裹。"
                )
            except Exception as exc:  # noqa: BLE001 - 网络等
                last_err = str(exc)
                time.sleep(min(2 ** attempt, 8))

        detail = format_issues(last_issues) if last_issues else last_err
        raise TranslationError(
            f"翻译失败，重试 {self.opts.max_retries} 次仍不合格：\n{detail}"
        )

    # -------------------------------------------------- HTTP

    def _call(self, system: str, user: str) -> tuple[str, dict]:
        """返回 (回复内容, token 用量)。

        用量必须往上报，不能丢——否则用户没法确认"到底调没调 API"、
        也没法估算花了多少钱。API 每次都返回 usage，白扔了可惜。
        """
        url = self.opts.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.opts.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.opts.temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if not self.opts.thinking:
            body["thinking"] = {"type": "disabled"}

        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.opts.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            # 某些部署不认识 thinking 参数，去掉重试一次
            if exc.code == 400 and "thinking" in detail and "thinking" in body:
                body.pop("thinking")
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.opts.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            else:
                raise RuntimeError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"网络错误：{exc.reason}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"响应结构异常：{json.dumps(payload, ensure_ascii=False)[:300]}") from exc
        return content, payload.get("usage") or {}


# ---------------------------------------------------------------- 自检（不联网）

if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("=== 提示词预览（poetic + soften，带流派锚点）===")
    opts = TranslateOptions()
    prompt = build_system_prompt(
        opts, "流派：J-Pop；年份：2024；艺人：DECO*27；专辑：Monitoring - Single"
    )
    print(prompt[:1800])
    print(f"\n……（共 {len(prompt)} 字符）")

    print("\n=== 露骨度策略（soften）===")
    print(_EXPLICIT_TEXT[EXPLICIT_SOFTEN])

    print("\n=== JSON 提取容错 ===")
    for raw in [
        '{"lines":["a","b"]}',
        '```json\n{"lines":["a","b"]}\n```',
        '好的，这是结果：\n{"lines":["a","b"]}\n希望有帮助。',
    ]:
        print(f"  {raw[:40]!r:<46} → {extract_json(raw)}")
