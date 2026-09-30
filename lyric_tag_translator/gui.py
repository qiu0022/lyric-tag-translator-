# -*- coding: utf-8 -*-
r"""图形界面（tkinter，无第三方依赖）。

给这个工具包一层壳，重点解决两件事：

1. **不用记命令行参数。** 选目录、点按钮。
2. **能直接看歌词。** 选中一首歌，右边显示它当前的 ©lyr 内容——
   译文对不对味，在这个窗口里就能判断，不用导入 iTunes 再翻。

线程模型：翻译在后台线程跑，日志通过 queue 送回主线程。
tkinter 不是线程安全的，所有 UI 更新都走 `_drain()` 轮询。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import clean
from .cache import Cache, default_db_path
from .identity import read_tags
from .lyrics import (
    backup_dir_for,
    clear_backups,
    iter_audio,
    list_backups,
    load_backup,
    read_lyrics,
    restore as restore_lyrics,
    write_lyrics,
)
from .translate import (
    DEFAULT_TARGET_LANG,
    EXPLICIT_KEEP,
    EXPLICIT_MASK,
    EXPLICIT_SOFTEN,
    PROMPT_VERSION,
    STYLE_EUPHEMISTIC,
    STYLE_LITERAL,
    STYLE_POETIC,
    TARGET_LANGS,
    TranslateOptions,
    Translator,
    normalize_target_lang,
)

STYLES = {"意象化 poetic": STYLE_POETIC, "直译 literal": STYLE_LITERAL,
          "含蓄化 euphemistic": STYLE_EUPHEMISTIC}
EXPLICITS = {"只中和贬称 soften": EXPLICIT_SOFTEN, "照实 keep": EXPLICIT_KEEP,
             "遮蔽 mask": EXPLICIT_MASK}
# 目标语言。这里**只列中文的两种字形**，不做成自由输入框——
# 原因见 translate.TARGET_LANGS 的注释：提示词只对中文校准过，
# 放开输入只会让人翻出一堆没人验证过的东西。
LANGUAGES = list(TARGET_LANGS)

FONT_UI = ("Microsoft YaHei UI", 9)
FONT_LYR = ("Microsoft YaHei", 10)

# 界面配置（只存目录，不存任何凭据）。
# 早先这里硬编码了一个默认音乐目录——那是按当时那台机器的情况写死的，
# 换目录之后就变成过期垃圾。改成记住上次用的，没有记录就是空的。
CONFIG_PATH = Path("gui_config.json")


def suggest_out_dir(music_dir: str) -> str:
    """给一个默认的输出目录：源目录旁边的 `<名字>_已翻译`。"""
    p = Path(music_dir)
    return str(p.with_name(p.name + "_已翻译"))


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置读不了不该拦住启动
        return {}


def save_config(**kw) -> None:
    try:
        data = load_config()
        data.update(kw)
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - 存不下也不该拦住使用
        pass


@dataclass
class Row:
    path: Path
    key: str
    display: str
    style_hint: str
    has_lyrics: bool
    done: bool
    need: int = 0
    unique: int = 0
    note: str = ""
    # 歌词形态上已经是"原文+译文"交替 → 别处翻好的成品，默认不碰
    translated: bool = False


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("歌词嵌译 · lyric-tag-translator")
        self.geometry("1180x760")
        self.minsize(940, 600)

        cfg = load_config()
        self.dir_var = tk.StringVar(value=cfg.get("last_dir", ""))
        self.backend_var = tk.StringVar(value=cfg.get("backend", "llm"))
        self.key_var = tk.StringVar(value=os.environ.get("DEEPSEEK_API_KEY", ""))
        self.model_var = tk.StringVar(value=cfg.get("model", "deepseek-flash"))
        self.style_var = tk.StringVar(value=cfg.get("style", list(STYLES)[0]))
        self.explicit_var = tk.StringVar(value=cfg.get("explicit", list(EXPLICITS)[0]))
        self.target_var = tk.StringVar(
            value=cfg.get("target_lang", DEFAULT_TARGET_LANG))
        self.dry_var = tk.BooleanVar(value=True)
        self.force_var = tk.BooleanVar(value=False)
        # **默认不勾"原地覆盖"**：默认把译文写到另一个目录，源文件不动。
        # 覆盖源文件是个不小的动作，不该是默认行为。
        self.inplace_var = tk.BooleanVar(value=cfg.get("inplace", False))
        self.out_var = tk.StringVar(value=cfg.get("last_out", ""))
        # 记下来的值可能已经不在候选列表里（比如改过选项），兜一下
        if self.style_var.get() not in STYLES:
            self.style_var.set(list(STYLES)[0])
        if self.explicit_var.get() not in EXPLICITS:
            self.explicit_var.set(list(EXPLICITS)[0])
        # 语言多一层：旧配置里可能存的是 "zh-tw" 这类简写，
        # 也可能存了一个早先支持、现在不支持的写法。归一化不成的一律退回默认。
        try:
            self.target_var.set(normalize_target_lang(self.target_var.get()))
        except ValueError:
            self.target_var.set(DEFAULT_TARGET_LANG)

        self.rows: list[Row] = []
        self.msg_q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.busy = False

        self._build()
        self.after(120, self._drain)

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text="音乐目录", font=FONT_UI).grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.dir_var, font=FONT_UI).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="浏览…", command=self._pick_dir).grid(row=0, column=2)
        self.btn_scan = ttk.Button(top, text="扫描", command=self._scan)
        self.btn_scan.grid(row=0, column=3, padx=6)
        top.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self, text="翻译设置", padding=(10, 6))
        opts.pack(fill="x", padx=10, pady=4)

        ttk.Label(opts, text="后端", font=FONT_UI).grid(row=0, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.backend_var, values=["llm", "echo"],
                     width=7, state="readonly", font=FONT_UI).grid(row=0, column=1, padx=(4, 14))

        ttk.Label(opts, text="模型", font=FONT_UI).grid(row=0, column=2, sticky="w")
        ttk.Entry(opts, textvariable=self.model_var, width=18, font=FONT_UI).grid(
            row=0, column=3, padx=(4, 14))

        ttk.Label(opts, text="API Key", font=FONT_UI).grid(row=0, column=4, sticky="w")
        ttk.Entry(opts, textvariable=self.key_var, width=34, show="•", font=FONT_UI).grid(
            row=0, column=5, padx=(4, 14), sticky="ew")

        ttk.Label(opts, text="风格", font=FONT_UI).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(opts, textvariable=self.style_var, values=list(STYLES),
                     width=18, state="readonly", font=FONT_UI).grid(
            row=1, column=1, columnspan=2, padx=(4, 14), pady=(6, 0), sticky="w")

        ttk.Label(opts, text="露骨度", font=FONT_UI).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Combobox(opts, textvariable=self.explicit_var, values=list(EXPLICITS),
                     width=18, state="readonly", font=FONT_UI).grid(
            row=1, column=4, columnspan=2, padx=(4, 14), pady=(6, 0), sticky="w")

        ttk.Label(opts, text="目标语言", font=FONT_UI).grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(opts, textvariable=self.target_var, values=LANGUAGES,
                     width=14, state="readonly", font=FONT_UI).grid(
            row=2, column=1, columnspan=2, padx=(4, 14), pady=(6, 0), sticky="w")

        ttk.Checkbutton(opts, text="试运行（不写盘）", variable=self.dry_var).grid(
            row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="重做（忽略已有译文/备份）", variable=self.force_var).grid(
            row=2, column=4, columnspan=2, sticky="w", pady=(6, 0))

        # 输出位置：原地覆盖 or 输出到指定目录（源文件一个字节不动）
        ttk.Checkbutton(opts, text="原地覆盖源文件", variable=self.inplace_var,
                        command=self._toggle_out).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.out_entry = ttk.Entry(opts, textvariable=self.out_var, font=FONT_UI, width=40)
        self.out_entry.grid(row=3, column=2, columnspan=3, padx=(4, 4),
                            pady=(6, 0), sticky="ew")
        self.out_btn = ttk.Button(opts, text="输出目录…", command=self._pick_out)
        self.out_btn.grid(row=3, column=5, pady=(6, 0), sticky="w")
        opts.columnconfigure(5, weight=1)
        self._toggle_out()

        mid = ttk.Panedwindow(self, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.Frame(mid)
        cols = ("state", "song", "need", "uniq")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        # 列名用"可译行"而不是"待译"：已译的歌里那些原文行依然在，
        # 显示"已译 + 待译52"会自相矛盾。可译行 = 勾选「重做」时会被翻译的行数。
        for c, t, w in [("state", "状态", 92), ("song", "歌曲", 300),
                        ("need", "可译行", 58), ("uniq", "去重后", 58)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "song" else "center")
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        mid.add(left, weight=3)

        right = ttk.Frame(mid)
        self.head = ttk.Label(right, text="（选中左侧歌曲查看歌词）", font=FONT_UI,
                              anchor="w", padding=(4, 4))
        self.head.pack(fill="x")
        box = ttk.Frame(right)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, wrap="word", font=FONT_LYR, undo=False,
                            background="#fbfbfb", relief="flat", padx=10, pady=8)
        ts = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ts.set)
        self.text.pack(side="left", fill="both", expand=True)
        ts.pack(side="right", fill="y")
        self.text.tag_configure("orig", foreground="#111111")
        self.text.tag_configure("trans", foreground="#0a6bb5")
        self.text.tag_configure("head", foreground="#888888")
        mid.add(right, weight=4)

        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        self.btn_run = ttk.Button(bar, text="翻译选中", command=lambda: self._run(True))
        self.btn_run.pack(side="left")
        self.btn_all = ttk.Button(bar, text="翻译全部", command=lambda: self._run(False))
        self.btn_all.pack(side="left", padx=6)
        ttk.Button(bar, text="核对", command=self._check).pack(side="left", padx=6)
        ttk.Button(bar, text="从备份还原", command=self._restore).pack(side="left", padx=6)
        ttk.Button(bar, text="清空缓存", command=self._prune).pack(side="left", padx=6)
        ttk.Button(bar, text="清除处理记录", command=self._forget).pack(side="left", padx=6)
        self.prog = ttk.Progressbar(bar, mode="determinate", length=220)
        self.prog.pack(side="right")

        logbox = ttk.LabelFrame(self, text="日志", padding=(6, 4))
        logbox.pack(fill="both", padx=10, pady=(0, 8))
        self.log = tk.Text(logbox, height=8, wrap="word", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4", relief="flat")
        ls = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")

    # ------------------------------------------------------------ 日志

    def _say(self, msg: str) -> None:
        self.msg_q.put(("log", msg))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                elif kind == "progress":
                    cur, total = payload  # type: ignore[misc]
                    self.prog["maximum"] = max(total, 1)
                    self.prog["value"] = cur
                elif kind == "done":
                    self._set_busy(False)
                    if payload:
                        messagebox.showinfo("完成", payload)
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("出错", payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in (self.btn_scan, self.btn_run, self.btn_all):
            b["state"] = st

    def _bg(self, fn, *a) -> None:
        """在后台线程跑 fn，异常回传到主线程弹窗。"""
        if self.busy:
            messagebox.showwarning("正在忙", "上一件事还没跑完。")
            return
        self._set_busy(True)

        def wrap() -> None:
            try:
                fn(*a)
            except Exception as exc:  # noqa: BLE001
                self.msg_q.put(("error", f"{type(exc).__name__}: {exc}"))
                self.msg_q.put(("done", ""))
            else:
                self.msg_q.put(("done", ""))
        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------------------ 扫描

    def _remember(self) -> None:
        """记住这次的设置。**只存目录和选项，不存 API Key。**"""
        save_config(
            last_dir=self.dir_var.get().strip(),
            backend=self.backend_var.get(),
            model=self.model_var.get().strip(),
            style=self.style_var.get(),
            explicit=self.explicit_var.get(),
            target_lang=self.target_var.get(),
            inplace=self.inplace_var.get(),
            last_out=self.out_var.get().strip(),
        )

    @staticmethod
    def _start_dir(*candidates: str) -> str:
        """挑一个存在的目录当文件对话框的起点，都没有就退回用户主目录。"""
        for c in candidates:
            if c and Path(c).is_dir():
                return c
        return str(Path.home())

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self._start_dir(self.dir_var.get()))
        if d:
            self.dir_var.set(d)

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(
            initialdir=self._start_dir(self.out_var.get(), self.dir_var.get())
        )
        if d:
            self.out_var.set(d)

    def _toggle_out(self) -> None:
        state = "disabled" if self.inplace_var.get() else "normal"
        self.out_entry["state"] = state
        self.out_btn["state"] = state
        # 取消原地覆盖、又没指定输出目录时，顺手填一个建议值，
        # 免得用户还要先自己去建目录才能跑。
        if not self.inplace_var.get() and not self.out_var.get().strip():
            src = self.dir_var.get().strip()
            if src:
                self.out_var.set(suggest_out_dir(src))

    def _scan(self) -> None:
        self._toggle_out()      # 目录可能变了，刷新输出目录的建议值
        self._remember()
        self._bg(self._scan_work)

    def _scan_work(self) -> None:
        root = Path(self.dir_var.get())
        if not root.is_dir():
            raise NotADirectoryError(f"目录不存在：{root}")
        self._say(f"扫描 {root} …")
        backup_dir = backup_dir_for(root)
        rows: list[Row] = []

        for f in iter_audio(root):
            tags = read_tags(f)
            text = read_lyrics(f)
            if text is None:
                rows.append(Row(f, tags.key, tags.display(), tags.style_hint(),
                                False, False, note="无歌词"))
                continue
            doc = clean.parse_tag_text(text)
            done = load_backup(backup_dir, tags.key) is not None
            rows.append(Row(f, tags.key, tags.display(), tags.style_hint(), True, done,
                            need=doc.translatable_count,
                            unique=len(doc.unique_translatable()),
                            translated=clean.looks_translated(doc)))

        self.rows = rows
        self.msg_q.put(("log", f"共 {len(rows)} 个音频文件"))
        self.after(0, self._fill_tree)

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            if not r.has_lyrics:
                state = "无歌词"
            elif r.need == 0:
                state = "无需翻译"
            elif r.done:
                state = "已译"
            elif r.translated:
                state = "已是双语"
            else:
                state = "待译"
            self.tree.insert("", "end", iid=str(i),
                             values=(state, r.display, r.need or "", r.unique or ""))

    def _on_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows[int(sel[0])]
        self.head["text"] = f"{r.display}    key={r.key}"
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        text = read_lyrics(r.path)
        if text is None:
            self.text.insert("end", "（这个文件没有内嵌歌词）\n", "head")
        else:
            backup = load_backup(backup_dir_for(Path(self.dir_var.get())), r.key)
            orig = set()
            if backup:
                orig = {ln.strip() for ln in backup["original"].splitlines() if ln.strip()}
            for ln in text.splitlines():
                if not ln.strip():
                    self.text.insert("end", "\n")
                elif ln.strip() in orig:
                    self.text.insert("end", ln + "\n", "orig")
                else:
                    self.text.insert("end", ln + "\n", "trans")
            if backup:
                self.text.insert("end", "\n—— 深色=原文，蓝色=译文 ——\n", "head")
        self.text.configure(state="disabled")

    # ------------------------------------------------------------ 翻译

    def _make_translator(self):
        if self.backend_var.get() == "echo":
            class Echo:
                def translate(self, lines, style_hint=""):
                    from .translate import TranslationResult
                    return TranslationResult(lines=[f"【译】{t}" for t in lines], register="echo")
            return Echo(), None

        key = self.key_var.get().strip()
        if not key:
            raise ValueError("没有填 API Key。可以先用「后端 = echo」离线走通流程。")
        opts = TranslateOptions(
            target_lang=self.target_var.get(),
            style=STYLES[self.style_var.get()],
            explicit=EXPLICITS[self.explicit_var.get()],
            model=self.model_var.get().strip() or "deepseek-flash",
        )
        return Translator(key, opts), opts

    def _run(self, selected_only: bool) -> None:
        self._remember()
        self._bg(self._run_work, selected_only)

    def _run_work(self, selected_only: bool) -> None:
        root = Path(self.dir_var.get())
        backup_dir = backup_dir_for(root)
        backend = self.backend_var.get()

        if selected_only:
            sel = [int(i) for i in self.tree.selection()]
            if not sel:
                raise ValueError("没有选中任何歌。")
            targets = [self.rows[i] for i in sel]
        else:
            targets = [r for r in self.rows
                       if r.has_lyrics and r.need > 0
                       and (self.force_var.get() or not r.translated)]

        if not targets:
            raise ValueError("没有需要翻译的歌。先点「扫描」。")

        translator, opts = self._make_translator()
        dry = self.dry_var.get()
        force = self.force_var.get()

        # 输出位置：勾了"原地覆盖"就写回源文件，否则复制到指定目录再写。
        out_root: Path | None = None
        if not self.inplace_var.get():
            raw = self.out_var.get().strip()
            if not raw:
                src = self.dir_var.get().strip()
                if not src:
                    raise ValueError("还没选音乐目录。")
                # 没指定就自动给一个，并把值填回界面——
                # 让用户看得见实际写到哪儿，而不是默默写到某个地方
                raw = suggest_out_dir(src)
                self.out_var.set(raw)
            out_root = Path(raw)
            out_root.mkdir(parents=True, exist_ok=True)

        self._say(f"{'试运行' if dry else '正式写入'}：{len(targets)} 首，"
                  f"后端={backend}，语言={self.target_var.get()}，"
                  f"风格={self.style_var.get()}，露骨={self.explicit_var.get()}")
        if not dry:
            if out_root is not None:
                self._say(f"输出目录：{out_root}（源文件不动，不产生备份）")
            else:
                self._say(f"写回源文件；备份目录：{backup_dir}")

        ok = failed = 0
        calls = tin = tout = 0
        db = default_db_path()

        with Cache(db) as cache:
            if backend != "echo":
                translator.cache = cache  # type: ignore[attr-defined]

            for i, r in enumerate(targets, 1):
                self.msg_q.put(("progress", (i - 1, len(targets))))
                head = f"[{i}/{len(targets)}] {r.display}"
                try:
                    text = read_lyrics(r.path)
                    if text is None:
                        self._say(f"{head}  跳过：没有内嵌歌词")
                        continue

                    backup = load_backup(backup_dir, r.key)
                    if not force and backup is not None:
                        self._say(f"{head}  跳过：已有备份（勾选「重做」可强制）")
                        continue

                    # 「重做」一律需要备份——没有原始版本就没法安全重翻。
                    # 与 CLI 的 collect() 保持同一条规则，两边别跑偏。
                    if force and backup is None:
                        self._say(f"{head}  ✗ 拒绝重做：没有备份，拿不到原始版本，"
                                  f"再翻一遍会在已有译文后面再插一条")
                        failed += 1
                        continue
                    # 重做时以备份里的原始版本为源，而不是现在这份双语文本
                    if force and backup is not None:
                        text = backup["original"]

                    if r.translated and backup is None:
                        self._say(f"{head}  跳过：歌词已是双语形态（别处翻好的成品）")
                        continue

                    doc = clean.parse_tag_text(text)
                    res = translator.translate(doc.source_texts(), r.style_hint)
                    calls += res.api_calls
                    tin += res.prompt_tokens
                    tout += res.completion_tokens

                    consistent, drifted = doc.enforce_consistency(res.lines)
                    targets_ln = doc.to_translate()
                    consistent = [clean.restore_trailing_sound(t.text, tr)
                                  for t, tr in zip(targets_ln, consistent)]
                    out_lines = doc.apply(consistent)
                    while out_lines and not out_lines[-1].strip():
                        out_lines.pop()
                    new_text = "\n".join(out_lines)

                    if dry:
                        self._say(f"{head}  {r.need} 行 → {len(out_lines)} 行"
                                  f"  [API×{res.api_calls}] [{res.register}] （未写盘）")
                    else:
                        target = r.path
                        if out_root is not None:
                            target = out_root / r.path.relative_to(root)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(r.path, target)
                        wres = write_lyrics(
                            target, new_text, key=r.key,
                            # 输出到副本时源文件没动，不需要备份
                            backup_dir=None if out_root is not None else backup_dir,
                        )
                        tag = " [缓存]" if res.from_cache else f" [API×{res.api_calls}]"
                        if drifted:
                            tag += f" [纠偏{drifted}]"
                        self._say(f"{head}  {wres.before_lines} → {wres.after_lines} 行"
                                  f"{tag} [{res.register}]")
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    self._say(f"{head}  ✗ {exc}")
                    failed += 1

        self.msg_q.put(("progress", (len(targets), len(targets))))
        summary = f"完成：成功 {ok}，失败 {failed}"
        if calls:
            cost = tin / 1e6 * 0.15 + tout / 1e6 * 0.6
            summary += f"\nAPI {calls} 次，token {tin}+{tout}，约 ${cost:.4f}"
        self._say("")
        self._say(summary)
        self.msg_q.put(("done", summary))

    # ------------------------------------------------------------ 其他操作

    def _check(self) -> None:
        self._bg(self._check_work)

    def _check_work(self) -> None:
        root = Path(self.dir_var.get())
        backup_dir = backup_dir_for(root)
        checked = bad = 0
        for r in self.rows:
            if not r.has_lyrics:
                continue
            data = load_backup(backup_dir, r.key)
            if data is None:
                continue
            text = read_lyrics(r.path)
            if text is None:
                continue
            checked += 1
            base = [ln.strip() for ln in data["original"].splitlines() if ln.strip()]
            cur = [ln.strip() for ln in text.splitlines() if ln.strip()]
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
                    if not clean.RE_HAN.search(src) and not clean.is_vocal_loose(src):
                        gaps.append(src)
            if gaps:
                bad += 1
                self._say(f"  {r.display}：{len(gaps)} 行外语没有译文")
                for g in gaps[:5]:
                    self._say(f"      {g[:70]}")
        self._say("")
        self._say(f"核对 {checked} 首——" +
                  ("没有发现漏译" if bad == 0 else f"{bad} 首需要人工确认"))
        self.msg_q.put(("done", f"核对完成：{checked} 首，{bad} 首待确认"))

    def _restore(self) -> None:
        n = len([r for r in self.rows if load_backup(
            backup_dir_for(Path(self.dir_var.get())), r.key)])
        if n == 0:
            messagebox.showinfo("没有备份", "这个目录下找不到任何备份。")
            return
        if not messagebox.askyesno(
                "确认还原", f"将用备份覆盖 {n} 首的歌词。\n\n原始歌词会回到未被翻译的状态。继续？"):
            return
        self._bg(self._restore_work)

    def _restore_work(self) -> None:
        root = Path(self.dir_var.get())
        backup_dir = backup_dir_for(root)
        n = 0
        for r in self.rows:
            try:
                restore_lyrics(r.path, r.key, backup_dir)
                n += 1
                self._say(f"  还原 {r.display}")
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                self._say(f"  失败 {r.display}：{exc}")
        self._say(f"\n还原 {n} 首")
        self.msg_q.put(("done", f"已还原 {n} 首"))

    def _forget(self) -> None:
        """清除处理记录（备份目录）。

        备份兼着两个作用：还原用 + "我们动过它"的标记。删掉之后两个都没了。
        必须说清后果——尤其是"音频里已有的歌词清不掉"，
        免得用户以为清掉记录就能从头再来。
        """
        root = Path(self.dir_var.get())
        backup_dir = backup_dir_for(root)
        backups = list_backups(backup_dir)
        if not backups:
            messagebox.showinfo("没有处理记录",
                                f"{backup_dir}\n\n（这个目录还没被处理过）")
            return
        if not messagebox.askyesno(
            "清除处理记录",
            f"共 {len(backups)} 份备份。\n\n"
            f"清掉之后：\n"
            f"  · 不能再从备份还原原始歌词\n"
            f"  · 别处翻好的歌会被重新翻译\n"
            f"  · 已经是双语的歌若勾「重做」，会被拒绝（拿不到原始版本）\n"
            f"  · **音频文件里已有的歌词不受影响**——那清不掉\n\n"
            f"确定清掉？",
        ):
            return
        n = clear_backups(backup_dir)
        self._say(f"已清除处理记录：{n} 份备份（{backup_dir}）")
        messagebox.showinfo("已清除", f"删除 {n} 份备份。\n音频文件里的歌词没有动。")
        if self.rows:
            self._scan()

    def _prune(self) -> None:
        """清空缓存。

        这里刻意用 `clear()`（全删）而不是 `prune()`（只删旧版本）——
        按钮叫「清空缓存」，就该做用户理解的那件事。
        早先调的是 prune，缓存全是当前版本时一条都删不掉，
        用户以为工具"记住了翻过什么"，清不掉。
        """
        db = default_db_path()
        if not db.exists():
            messagebox.showinfo("没有缓存", f"找不到 {db.resolve()}\n（说明还没跑过翻译）")
            return

        with Cache(db) as cache:
            before = cache.stats()

        if not messagebox.askyesno(
            "清空缓存",
            f"缓存里有 {before['cached_songs']} 首的译文。\n\n"
            f"清空后，下次翻译会**重新调用 API**（约几分钱）。\n"
            f"已经写进音频文件的歌词不受影响，备份也不受影响。\n\n"
            f"确定清空？",
        ):
            return

        with Cache(db) as cache:
            out = cache.clear()
        self._say(f"清空缓存：删除译文 {out['cleared_cache']} 条、日志 {out['cleared_logs']} 条，"
                  f"{out['bytes_before']:,} → {out['bytes_after']:,} 字节")
        messagebox.showinfo(
            "已清空",
            f"删除译文缓存 {out['cleared_cache']} 条\n"
            f"删除运行日志 {out['cleared_logs']} 条\n"
            f"文件 {out['bytes_before']:,} → {out['bytes_after']:,} 字节\n\n"
            f"音频文件和备份都没有动。")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
