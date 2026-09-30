r"""命令行编排：把各层串成一条流水线。

    scan     扫描：报告每首歌会翻多少行、跳过什么。**不联网、不写盘。**
    run      翻译并写回。带缓存、断点续跑、写前备份。
    check    体检：拿备份当基准，核对已写回的双语歌词有没有漏译或错位。
    restore  从备份还原。

设计原则：**默认不动你的文件。**
`scan` / `check` 是纯只读的，建议每次先跑一遍再决定要不要 `run`；
`run` 也提供 `--dry-run`。

`--backend echo` 是离线后端，不需要 API key，让你先走通
"解析 → 渲染 → 写回"这一段，确认无误再花钱调模型。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import clean
from .cache import Cache
from .identity import read_tags
from .lyrics import (
    backup_dir_for,
    clear_backups,
    iter_audio,
    list_backups,
    read_lyrics,
    write_lyrics,
)
from .lyrics import restore as restore_lyrics
from .translate import (
    EXPLICIT_KEEP,
    EXPLICIT_MASK,
    EXPLICIT_SOFTEN,
    PROMPT_VERSION,
    STYLE_EUPHEMISTIC,
    STYLE_LITERAL,
    STYLE_POETIC,
    TranslateOptions,
    TranslationResult,
    Translator,
)

STYLES = {
    "literal": STYLE_LITERAL,
    "euphemistic": STYLE_EUPHEMISTIC,
    "poetic": STYLE_POETIC,
}
EXPLICITS = {
    "keep": EXPLICIT_KEEP,
    "soften": EXPLICIT_SOFTEN,
    "mask": EXPLICIT_MASK,
}


# ---------------------------------------------------------------- 单曲计划


@dataclass
class Plan:
    path: Path
    key: str
    display: str
    style_hint: str
    doc: clean.LyricDoc
    text: str

    @property
    def count(self) -> int:
        return self.doc.translatable_count

    @property
    def unique(self) -> int:
        return len(self.doc.unique_translatable())


@dataclass
class CollectResult:
    plans: list[Plan] = field(default_factory=list)
    already: int = 0        # 有备份 → 我们动过它
    translated: int = 0     # 形态上已是双语 → 别处翻好的成品
    unsafe_redo: int = 0    # 要重做但没有备份（= 没有原始版本），拒绝执行

    def skip_note(self) -> str:
        bits = []
        if self.already:
            bits.append(f"{self.already} 首已有备份")
        if self.translated:
            bits.append(f"{self.translated} 首已是双语形态")
        if self.unsafe_redo:
            bits.append(f"{self.unsafe_redo} 首要求重做但没有备份")
        return "，".join(bits)


def collect(
    root: Path,
    force: frozenset[str],
    only: str = "",
    limit: int = 0,
    *,
    backup_dir: Path | None = None,
    redo: bool = False,
) -> "CollectResult":
    """收集待处理的歌。

    **幂等性有两道判定**，缺一不可：

    1. **备份**：某个 key 已经有备份，说明我们动过它。
    2. **形态**：歌词本身已经是"原文+译文"交替的样子。
       这一条是为**导入的外部已翻译歌**准备的——它们没有备份
       （旧流程翻的、别人给的），只靠备份判定会当成未翻译再翻一遍。

    为什么必须防：©lyr 一旦是双语形态，那些**没被翻译的原文行**依然满足
    "待译"条件，会在已有译文后面再插一条。实测过连跑两次：78 → 83 → 88 行。

    `redo=True` 时以**备份里的原始版本**为源重翻。
    **没有备份、歌词又已是双语时，重做是危险的**——拿不到原始版本，
    只能拿现在这份双语文本再翻一遍，等于叠加。这种情况直接拒绝（计入 `unsafe`），
    而不是放任它把文件写坏。用户把处理记录清掉之后就可能撞上这个。
    """
    from .lyrics import load_backup

    res = CollectResult()
    for f in iter_audio(root):
        tags = read_tags(f)
        if only and only not in tags.display() and only != tags.key:
            continue
        text = read_lyrics(f)
        if text is None:
            continue

        doc = clean.parse_tag_text(text, force_translate=force)
        if doc.translatable_count == 0:
            continue

        data = load_backup(backup_dir, tags.key) if backup_dir else None

        if redo:
            # 「重做」的语义是"拿原始版本再翻一遍"，所以**必须有备份**。
            # 没有备份就没有原始版本——现在这份可能已经是双语文本，
            # 再翻一遍等于在已有译文后面再插一条。
            #
            # 早先这里只拦"看起来已是双语"的，结果**中文歌漏了**：
            # `looks_translated()` 对中文歌必然返回 False（夹英文钩子是常态，
            # 判不出来），于是一个删掉备份的中文歌会走进来被翻第二遍。
            # 收紧成"重做一律需要备份"，就没有这个口子了。
            if data is None:
                res.unsafe_redo += 1
                continue
            text = data["original"]
            doc = clean.parse_tag_text(text, force_translate=force)
        elif data is not None:
            res.already += 1
            continue
        elif clean.looks_translated(doc):
            res.translated += 1
            continue

        res.plans.append(
            Plan(f, tags.key, tags.display(), tags.style_hint(), doc, text)
        )
        if limit and len(res.plans) >= limit:
            break
    return res


# ---------------------------------------------------------------- 离线后端


class EchoTranslator:
    """离线后端：原样加前缀，不需要 API key。

    用来验证流水线的后半段（渲染、写回、备份、还原）是否正确。
    """

    def translate(self, lines: list[str], style_hint: str = "") -> TranslationResult:
        return TranslationResult(lines=[f"【译】{t}" for t in lines], register="echo")


# ---------------------------------------------------------------- 渲染


def render(doc: clean.LyricDoc, translations: list[str]) -> str:
    lines = doc.apply(translations)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------- 命令


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    force = clean.load_force_translate(args.force_translate) if args.force_translate else frozenset()
    res = collect(
        root, force, args.only, args.limit,
        backup_dir=backup_dir_for(root), redo=args.redo,
    )
    plans = res.plans

    if res.skip_note():
        print(f"（{res.skip_note()}，跳过）")
    if res.unsafe_redo:
        print("    ↑ 「重做」需要备份里的原始版本。这些歌没有备份，")
        print("      拿现在这份再翻一遍会在已有译文后面再插一条，所以拒绝执行。")
        print("      如果它们本来就还没翻译，去掉 --redo 正常跑即可。")
    if res.skip_note():
        print()
    if not plans:
        print("没有找到需要翻译的歌。")
        return 0

    print(f"{'歌曲':<38} {'语言':<6} {'待译':>4} {'唯一':>4} {'省':>4} {'拟声':>4} {'空行':>4}")
    print("-" * 76)
    tot = {"need": 0, "uniq": 0, "vocal": 0, "blank": 0}
    vocals: dict[str, int] = {}
    for p in plans:
        kinds = {k: 0 for k in ("vocal", "blank")}
        for ln in p.doc.lines:
            if ln.kind in kinds:
                kinds[ln.kind] += 1
            if ln.kind == "vocal":
                vocals[ln.text.strip()] = vocals.get(ln.text.strip(), 0) + 1
        tot["need"] += p.count
        tot["uniq"] += p.unique
        tot["vocal"] += kinds["vocal"]
        tot["blank"] += kinds["blank"]
        print(f"{p.display[:36]:<38} {p.doc.primary_lang:<6} {p.count:>4} {p.unique:>4} "
              f"{p.count - p.unique:>4} {kinds['vocal']:>4} {kinds['blank']:>4}")

    print()
    print(f"歌曲 {len(plans)} 首")
    print(f"待译（逐行） {tot['need']}")
    print(f"去重后需翻   {tot['uniq']}   （省 {tot['need'] - tot['uniq']}，"
          f"{100 * (tot['need'] - tot['uniq']) / tot['need']:.1f}%）" if tot["need"] else "")
    print(f"拟声/跳过    {tot['vocal']}")
    print(f"空行         {tot['blank']}")

    if args.show_vocal and vocals:
        print("\n被判为拟声、不会翻译的行：")
        for text, n in sorted(vocals.items(), key=lambda kv: -kv[1]):
            print(f"  ×{n:<3} {text[:66]!r}")
        print("\n如果上面混进了真歌词，把它们整行加进 --force-translate 指定的文件。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    force = clean.load_force_translate(args.force_translate) if args.force_translate else frozenset()
    backup_dir = backup_dir_for(root)
    res = collect(
        root, force, args.only, args.limit,
        backup_dir=backup_dir, redo=args.redo,
    )
    plans = res.plans
    if res.skip_note():
        print(f"{res.skip_note()}，跳过")
    if res.unsafe_redo:
        print("  重新翻译这些歌需要原始版本，但备份已经不在了。")
        print("  再翻一遍会在已有译文后面再插一条，所以拒绝执行。")
        print("  如果确实要重做，请先恢复原始歌词（或从别处拿到未翻译的版本）。")
    if not plans:
        print("没有找到需要翻译的歌。")
        return 0

    # 先验 Key 再打印运行信息——否则会先告诉用户"要写哪儿了"，
    # 紧接着才报 Key 错，看着像已经开始了。
    if args.backend == "echo":
        translator = EchoTranslator()  # type: ignore[assignment]
    else:
        key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LYRIC_API_KEY")
        if not key:
            print("缺少 API key。用 --api-key 或设置环境变量 DEEPSEEK_API_KEY。", file=sys.stderr)
            print("想先离线验证流程，加 --backend echo。", file=sys.stderr)
            return 2
        try:
            translator = Translator(
                key,
                TranslateOptions(
                    target_lang=args.target_lang,
                    style=STYLES[args.style],
                    explicit=EXPLICITS[args.explicit],
                    model=args.model,
                    base_url=args.base_url,
                    thinking=args.thinking,
                    max_retries=args.retries,
                ),
            )  # type: ignore[assignment]
        except ValueError as exc:
            print(f"API Key 有问题：\n  {exc}", file=sys.stderr)
            print("\n想先离线验证流程，把 --backend llm 换成 --backend echo。", file=sys.stderr)
            return 2

    out_root = Path(args.out) if args.out else None
    if out_root is not None:
        print(f"输出目录：{out_root}（源文件不动）")
    else:
        print("写回方式：原地覆盖源文件（有备份可还原）")

    print(f"共 {len(plans)} 首待处理")
    if args.dry_run:
        print("【试运行】不会写入任何文件\n")
    elif out_root is None:
        print(f"备份目录：{backup_dir}\n")
    else:
        print()

    ok = skipped = failed = 0
    api_calls = 0
    tok_in = tok_out = 0
    with Cache(args.db) as cache:
        if args.backend != "echo":
            translator.cache = cache  # type: ignore[attr-defined]

        for i, p in enumerate(plans, 1):
            head = f"[{i}/{len(plans)}] {p.display}"
            try:
                res = translator.translate(p.doc.source_texts(), p.style_hint)
                consistent, drifted = p.doc.enforce_consistency(res.lines)
                # 行尾语气词还原成英文原样。提示词已经要求了，这里是确定性兜底：
                # 这件事能机械判断，不该指望模型每次都自觉。
                targets = p.doc.to_translate()
                consistent = [
                    clean.restore_trailing_sound(t.text, tr)
                    for t, tr in zip(targets, consistent)
                ]
                text = render(p.doc, consistent)

                if args.dry_run:
                    api_calls += res.api_calls
                    tok_in += res.prompt_tokens
                    tok_out += res.completion_tokens
                    print(f"{head}  {p.count} 行 → {len(text.splitlines())} 行  "
                          f"（试运行  API×{res.api_calls} {res.prompt_tokens}+{res.completion_tokens}tok）")
                    if args.verbose:
                        print(_indent(text))
                    cache.log(p.key, str(p.path), "skipped", "dry-run")
                    skipped += 1
                    continue

                # 输出模式：复制到目标目录再写，源文件一个字节都不动。
                # 这种情况下不需要备份——备份的意义是"写坏了能回退"，
                # 而源文件压根没被碰过。
                target = p.path
                if out_root is not None:
                    target = out_root / p.path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p.path, target)

                result = write_lyrics(
                    target, text, key=p.key,
                    backup_dir=None if out_root is not None else backup_dir,
                )
                api_calls += res.api_calls
                tok_in += res.prompt_tokens
                tok_out += res.completion_tokens

                tags = ""
                if res.from_cache:
                    tags += " [缓存·未调API]"
                else:
                    tags += f" [API×{res.api_calls} {res.prompt_tokens}+{res.completion_tokens}tok]"
                if drifted:
                    tags += f" [副歌纠偏 {drifted}]"
                if res.register:
                    tags += f" [{res.register}]"
                print(f"{head}  {result.before_lines} → {result.after_lines} 行{tags}")
                cache.log(p.key, str(p.path), "ok",
                          f"lines={result.after_lines} drifted={drifted} cached={res.from_cache}")
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单曲失败不能中断整批
                print(f"{head}  ❌ 失败：{exc}")
                cache.log(p.key, str(p.path), "failed", str(exc))
                failed += 1

    print()
    print(f"完成：写入 {ok}，跳过 {skipped}，失败 {failed}")
    if api_calls:
        # 单价按 DeepSeek 低峰价（$0.15 / $0.6 每百万 token）估算，仅供心里有数
        cost = tok_in / 1e6 * 0.15 + tok_out / 1e6 * 0.6
        print(f"API 调用 {api_calls} 次，token {tok_in} 入 + {tok_out} 出"
              f"，约 ${cost:.4f}")
    else:
        print("本次没有调用 API（全部命中缓存或未翻译）。")
    if failed:
        print("失败清单见 check 或数据库的 run_log 表。原始歌词都在备份里，可随时 restore。")
    return 0 if failed == 0 else 1


def cmd_check(args: argparse.Namespace) -> int:
    """拿备份当基准核对：现在的双语歌词里，有没有外语行没配上译文。"""
    root = Path(args.dir)
    backup_dir = backup_dir_for(root)
    if not list_backups(backup_dir):
        print(f"没有找到备份（{backup_dir}），无法核对。")
        return 1

    files = iter_audio(root)
    bad: list[tuple[str, list[str]]] = []
    checked = 0

    for f in files:
        tags = read_tags(f)
        cur_text = read_lyrics(f)
        if cur_text is None:
            continue
        from .lyrics import load_backup

        data = load_backup(backup_dir, tags.key)
        if data is None:
            continue
        checked += 1

        base = [ln.strip() for ln in data["original"].splitlines() if ln.strip()]
        cur = [ln.strip() for ln in cur_text.splitlines() if ln.strip()]

        marks, p = [], 0
        for line in cur:
            if p < len(base) and line == base[p]:
                marks.append(("orig", p))
                p += 1
            else:
                marks.append(("trans", -1))

        gaps = []
        for i, (kind, idx) in enumerate(marks):
            if kind != "orig":
                continue
            nxt = marks[i + 1] if i + 1 < len(marks) else None
            if nxt is None or nxt[0] != "trans":
                src = base[idx]
                # 用宽松版：check 是报告工具，宁可少报也不要淹在噪音里。
                # 严格的 is_vocal 留着管"要不要翻译"——那里误判会静默漏掉真歌词。
                if not clean.RE_HAN.search(src) and not clean.is_vocal_loose(src):
                    gaps.append(src)
        if gaps:
            bad.append((tags.display(), gaps))

    print(f"核对 {checked} 首")
    if not bad:
        print("每一行外语原文后面都跟上了译文。✅")
        return 0

    total = sum(len(g) for _, g in bad)
    print("下面是**外语原文后面没有跟译文**的行。")
    print("注意：这不一定是漏译——拟声词、人名被有意保留原样时也会出现在这里。")
    print("需要你人工扫一眼。\n")
    for name, gaps in bad:
        print(f"=== {name}  （{len(gaps)} 行）")
        for g in gaps[:15]:
            print(f"    {g[:78]}")
        if len(gaps) > 15:
            print(f"    ……还有 {len(gaps) - 15} 行")
        print()
    print(f"合计 {len(bad)} 首 / {total} 行待确认")
    return 1


def cmd_restore(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    backup_dir = backup_dir_for(root)
    backups = list_backups(backup_dir)
    if not backups:
        print(f"没有备份可还原（{backup_dir}）。")
        return 1

    print(f"共 {len(backups)} 份备份")
    if not args.yes:
        print("这会用备份覆盖当前的 ©lyr。确认请加 --yes。")
        return 0

    n = 0
    for f in iter_audio(root):
        tags = read_tags(f)
        try:
            restore_lyrics(f, tags.key, backup_dir)
            n += 1
            print(f"[还原] {tags.display()}")
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {tags.display()}：{exc}")
    print(f"\n还原 {n} 首")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """清空处理记录（备份目录）。

    备份兼着两个作用：还原用 + "我们动过它"的标记。删掉之后两个都没了。
    说清楚后果再动手，别让用户以为清掉记录就能"从头再来"——
    歌词已经写进音频文件了，那是清不掉的。
    """
    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1
    backup_dir = backup_dir_for(root)
    backups = list_backups(backup_dir)
    if not backups:
        print(f"没有处理记录可清（{backup_dir}）")
        return 0

    print(f"处理记录：{backup_dir}")
    print(f"共 {len(backups)} 份\n")
    print("清掉的后果：")
    print("  · 不能再从备份还原原始歌词")
    print("  · 之后重跑时，别处翻好的歌会被重新翻译")
    print("  · 已经是双语的歌若勾「重做」，会因拿不到原始版本而被拒绝执行")
    print("  · **音频文件里已有的歌词不受影响**——那是清不掉的")
    print()
    if not args.yes:
        print("确认请加 --yes")
        return 0

    n = clear_backups(backup_dir)
    print(f"已删除 {n} 份备份。")
    return 0


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + ln for ln in text.splitlines()[:40])


def _print_stats(stats: dict) -> None:
    print(f"  翻译缓存   {stats['cached_songs']} 条")
    for ver, n in sorted(stats.get("by_prompt_version", {}).items()):
        mark = "  ← 当前版本" if ver == PROMPT_VERSION else ""
        print(f"      v{ver}: {n} 条{mark}")
    runs = stats.get("runs") or {}
    if runs:
        print("  运行日志   " + "，".join(f"{k} {v}" for k, v in runs.items()))
    print(f"  文件大小   {stats['db_bytes']:,} 字节")


def cmd_prune(args: argparse.Namespace) -> int:
    """缓存维护。两种模式，别搞混：

    - 默认（prune）：只删**非当前版本**的产物 + 过期日志。当前版本的缓存有效，保留。
    - `--purge`（clear）：**全删**，让工具忘掉翻过什么，下次重新调 API。

    这两者被搞混过一次：界面上的「清理缓存」按钮实际调的是 prune，
    缓存全是当前版本时一条都删不掉，用户以为工具"记住了、清不掉"。
    """
    db = Path(args.db)
    if not db.exists():
        print(f"没有缓存文件：{db}（说明还没跑过翻译）")
        return 1

    with Cache(db) as cache:
        print(f"缓存文件：{db}\n")
        print("清理前：")
        _print_stats(cache.stats())
        print()

        if args.purge:
            out = cache.clear(cache=not args.logs_only)
            print(f"【清空】删除译文 {out['cleared_cache']} 条、日志 {out['cleared_logs']} 条")
            print("下次翻译会重新调用 API。")
        else:
            out = cache.prune(
                current_version=None if args.logs_only else PROMPT_VERSION,
                keep_days=args.keep_days,
            )
            if args.logs_only:
                print("（--logs-only：只清日志，不动任何译文缓存）")
            else:
                print(f"删除非当前版本（v{PROMPT_VERSION}）的译文缓存：{out['dead_cache']} 条")
                if out["dead_cache"] == 0:
                    print("    ↑ 是 0？说明缓存全是当前版本，没有旧产物。")
                    print("      想连当前版本的缓存一起清掉（让工具重翻），加 --purge。")
            print(f"删除超过 {args.keep_days} 天的日志：{out['old_logs']} 条")
            print(f"删除超出保留量的日志：{out['excess_logs']} 条")

        print(f"文件 {out['bytes_before']:,} → {out['bytes_after']:,} 字节")
        print()
        print("清理后：")
        _print_stats(cache.stats())

    print("\n注意：歌词备份不在这里，不会被清理（.lyric_i18n_backup/）")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="lyric-i18n",
        description="批量把歌词翻译并写回 m4a 的内嵌歌词字段（供 iPod 这类纯文本显示设备使用）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("dir", help="音乐目录（递归扫描 m4a/m4b/mp4）")
        p.add_argument("--only", default="", help="只处理名字或 key 含此串的歌")
        p.add_argument("--limit", type=int, default=0, help="最多处理几首")
        p.add_argument("--force-translate", default="", help="人工豁免清单文件")
        p.add_argument("--redo", action="store_true",
                       help="重做已有备份的歌（以备份里的原始版本为源重翻）")

    p_scan = sub.add_parser("scan", help="扫描报告（只读、不联网）")
    common(p_scan)
    p_scan.add_argument("--show-vocal", action="store_true", help="列出被判为拟声的行")
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="翻译并写回")
    common(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="只演示，不写盘")
    p_run.add_argument("--out", default="",
                       help="输出到指定目录（保持相对结构），源文件不动。"
                            "不填则原地覆盖源文件")
    p_run.add_argument("--verbose", action="store_true", help="试运行时打印全文")
    p_run.add_argument("--backend", choices=["llm", "echo"], default="llm",
                       help="echo = 离线假翻译，用于验证流程")
    p_run.add_argument("--api-key", default="")
    p_run.add_argument("--model", default="deepseek-flash")
    p_run.add_argument("--base-url", default="https://api.deepseek.com")
    p_run.add_argument("--target-lang", default="简体中文")
    p_run.add_argument("--style", choices=list(STYLES), default="poetic")
    p_run.add_argument("--explicit", choices=list(EXPLICITS), default="soften")
    p_run.add_argument("--thinking", action="store_true", help="开启思考模式（更慢更贵）")
    p_run.add_argument("--retries", type=int, default=3)
    p_run.add_argument("--db", default="lyric_i18n.db", help="缓存数据库路径")
    p_run.set_defaults(func=cmd_run)

    p_check = sub.add_parser("check", help="核对已写回的双语歌词（只读）")
    common(p_check)
    p_check.set_defaults(func=cmd_check)

    p_res = sub.add_parser("restore", help="从备份还原")
    common(p_res)
    p_res.add_argument("--yes", action="store_true", help="确认执行")
    p_res.set_defaults(func=cmd_restore)

    p_forget = sub.add_parser(
        "forget", help="清空处理记录（备份目录）——之后就还原不了了")
    p_forget.add_argument("dir", help="音乐目录")
    p_forget.add_argument("--yes", action="store_true", help="确认执行")
    p_forget.set_defaults(func=cmd_forget)

    p_prune = sub.add_parser(
        "prune", help="缓存维护：默认只清旧版本产物；--purge 全清")
    p_prune.add_argument("--db", default="lyric_i18n.db")
    p_prune.add_argument("--keep-days", type=int, default=30,
                         help="日志保留天数，默认 30")
    p_prune.add_argument("--purge", action="store_true",
                         help="全清（连当前版本的译文缓存一起删），"
                              "下次翻译会重新调 API")
    p_prune.add_argument("--logs-only", action="store_true",
                         help="只清日志，不动任何译文缓存")
    p_prune.set_defaults(func=cmd_prune)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。已写入的歌都有备份，可用 restore 还原。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
