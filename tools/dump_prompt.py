# -*- coding: utf-8 -*-
"""把渲染后的完整提示词导出成可读文本，便于人工审阅。

用法：python dump_prompt.py [输出路径]
"""
import _bootstrap  # noqa: F401  （把项目根加进 sys.path，见 _bootstrap.py）
import sys
from pathlib import Path

import lyric_i18n.translate as T
from lyric_i18n.translate import (
    EXPLICIT_KEEP,
    EXPLICIT_MASK,
    EXPLICIT_SOFTEN,
    PROMPT_VERSION,
    STYLE_EUPHEMISTIC,
    STYLE_LITERAL,
    STYLE_POETIC,
    TranslateOptions,
    build_system_prompt,
    build_user_prompt,
)

HINT = "流派：J-Pop；年份：2024；艺人：DECO*27；专辑：Monitoring - Single"
SAMPLE_LINES = ["夜の風に揺れて", "君の名前を呼ぶ", "夜の風に揺れて"]

out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("提示词.txt")
W = 72

opts = TranslateOptions()
system = build_system_prompt(opts, HINT)
user = build_user_prompt(SAMPLE_LINES)

parts: list[str] = []
add = parts.append

add("=" * W)
add(f"lyric-i18n 提示词   版本 v{PROMPT_VERSION}")
add("=" * W)
add("")
add("=== 默认配置 ===")
add(f"  target_lang = {opts.target_lang}")
add(f"  style       = {opts.style}       （见附录 A）")
add(f"  explicit    = {opts.explicit}    （见附录 B）")
add(f"  model       = {opts.model}")
add(f"  temperature = {opts.temperature}")
add("")
add(f"system 提示词每次请求都完整发一遍：{len(system)} 字符")
add("（这是每次调用的主要开销；歌词本身通常只占几百 token）")
add("")
add("-" * W)
add("【SYSTEM 提示词】")
add("-" * W)
add("")
add(system)
add("")
add("-" * W)
add("【USER 提示词】每次不同，歌词放在这里")
add("-" * W)
add("")
add(user)
add("")

add("")
add("=" * W)
add("附录 A：改写自由度（--style）三选一")
add("    替换上面「第四优先级」那一节")
add("=" * W)
for label, key in [("poetic 意象化（默认）", STYLE_POETIC),
                   ("literal 直译", STYLE_LITERAL),
                   ("euphemistic 含蓄化", STYLE_EUPHEMISTIC)]:
    add("")
    add(f"--- {label} ---")
    add(T._STYLE_TEXT[key])

add("")
add("=" * W)
add("附录 B：露骨内容处理（--explicit）三选一")
add("    替换上面「第五优先级」那一节")
add("=" * W)
for label, key in [("soften 只中和贬称（默认）", EXPLICIT_SOFTEN),
                   ("keep 照实", EXPLICIT_KEEP),
                   ("mask 遮蔽", EXPLICIT_MASK)]:
    add("")
    add(f"--- {label} ---")
    add(T._EXPLICIT_TEXT[key])

add("")
add("=" * W)
add(f"system {len(system)} 字符 ≈ {len(system) // 2} token")
add("=" * W)

text = "\n".join(parts)
out_path.write_text(text, encoding="utf-8")
print(f"已写出：{out_path}（{len(text)} 字符，{len(text.splitlines())} 行）")
