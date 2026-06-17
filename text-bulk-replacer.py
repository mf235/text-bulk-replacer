#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Text Bulk Replacer v2

指定フォルダ配下の対象テキストファイルを、調査または一括置換するGUIツール。
- 通常文字列 / 正規表現
- 再帰検索
- 対象ファイルパターン指定 (*.txt;*.md など)
- 文字コード 自動判別 / UTF-8 / SJIS(CP932) / EUC-JP
- 置換前バックアップ作成オプション
- 置換条件履歴 / 前回設定復元

Optional:
    pip install tkinterdnd2
を入れると、フォルダ欄へのドラッグ＆ドロップが使えます。
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False
    DND_FILES = None  # type: ignore
    TkinterDnD = None  # type: ignore


APP_TITLE = "テキスト一括置換ツール"
MAX_LOG_MATCHES_PER_FILE = 500
PREVIEW_LIMIT = 120
HISTORY_LIMIT = 10
SETTINGS_FILE = Path(__file__).with_name("text-bulk-replacer-settings.json")


@dataclass
class MatchInfo:
    line: int
    column: int
    matched: str
    replacement: str


@dataclass
class FileReport:
    path: Path
    encoding: str
    match_count: int
    logged_matches: list[MatchInfo]
    log_limited: bool = False


ENCODING_MAP = {
    "自動判別": None,
    "UTF-8": "utf-8",
    "UTF-8 BOM": "utf-8-sig",
    "SJIS/CP932": "cp932",
    "EUC-JP": "euc_jp",
}


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # 設定保存に失敗しても、本体の置換処理は止めない。
        pass


def one_line_preview(value: str, limit: int = 42) -> str:
    value = value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(value) > limit:
        value = value[:limit] + "…"
    return value


def normalize_dropped_path(value: str) -> str:
    """tkinterdnd2のドロップ文字列をWindows日本語パス向けに整える。"""
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    # 複数ドロップされた場合は先頭だけ使う。スペース入りパスは {} で保護される想定。
    if "} {" in value:
        value = value.split("} {")[0].lstrip("{").rstrip("}")
    return value


def parse_patterns(pattern_text: str) -> list[str]:
    parts = re.split(r"[;,\n]+", pattern_text)
    patterns = [p.strip() for p in parts if p.strip()]
    return patterns or ["*.txt"]


def iter_target_files(folder: Path, patterns: Iterable[str]) -> list[Path]:
    pattern_list = list(patterns)
    files: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        # このツールが作るバックアップを、うっかり再置換しにくくする。
        if path.name.endswith(".bak"):
            continue
        name = path.name
        if any(fnmatch.fnmatchcase(name.lower(), pat.lower()) for pat in pattern_list):
            files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def score_decoded_text(text: str) -> int:
    """文字化けっぽさをざっくり点数化。低いほど自然。"""
    score = 0
    suspicious_chars = "�"
    mojibake_tokens = ("縺", "繧", "譁", "荳", "蜷", "鬆", "髱", "驥", "窶")

    score += sum(text.count(ch) * 100 for ch in suspicious_chars)
    score += sum(text.count(token) * 5 for token in mojibake_tokens)

    for ch in text:
        code = ord(ch)
        if code < 32 and ch not in "\t\r\n":
            score += 20
        elif 0xE000 <= code <= 0xF8FF:  # Private Use Area
            score += 3
    return score


def detect_encoding(raw: bytes) -> tuple[str, str]:
    """日本語テキスト向けの実用的な自動判別。戻り値は (encoding, decoded_text)。"""
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", raw.decode("utf-8-sig")

    # UTF-8は厳密に判定できるので最優先。
    try:
        return "utf-8", raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # インストールされていれば charset_normalizer を利用。
    # なくても動くように、必須依存にはしない。
    try:
        from charset_normalizer import from_bytes  # type: ignore

        result = from_bytes(raw).best()
        if result and result.encoding:
            guessed = result.encoding.lower().replace("-", "_")
            if guessed in {"cp932", "shift_jis", "shift_jis_2004", "euc_jp"}:
                enc = "cp932" if guessed.startswith("shift_jis") else guessed
                try:
                    return enc, raw.decode(enc)
                except UnicodeDecodeError:
                    pass
    except Exception:
        pass

    candidates = ["cp932", "euc_jp"]
    decoded_candidates: list[tuple[int, str, str]] = []
    for enc in candidates:
        try:
            text = raw.decode(enc)
            decoded_candidates.append((score_decoded_text(text), enc, text))
        except UnicodeDecodeError:
            continue

    if decoded_candidates:
        decoded_candidates.sort(key=lambda item: item[0])
        _, enc, text = decoded_candidates[0]
        return enc, text

    raise UnicodeDecodeError("unknown", raw, 0, 1, "対応している文字コードで読み込めませんでした")


def decode_file(path: Path, encoding_choice: str) -> tuple[str, str, bytes]:
    raw = path.read_bytes()
    selected = ENCODING_MAP.get(encoding_choice)
    if selected is None:
        enc, text = detect_encoding(raw)
        return enc, text, raw

    try:
        return selected, raw.decode(selected), raw
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(
            exc.encoding,
            exc.object,
            exc.start,
            exc.end,
            f"{encoding_choice} として読み込めませんでした: {exc.reason}",
        ) from exc


def preview_text(text: str, limit: int = PREVIEW_LIMIT) -> str:
    value = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(value) > limit:
        value = value[:limit] + "…"
    return value


def line_col_from_pos(text: str, pos: int) -> tuple[int, int]:
    # 巨大ファイルで大量一致すると重いが、ログ用途として分かりやすさ優先。
    line = text.count("\n", 0, pos) + 1
    last_newline = text.rfind("\n", 0, pos)
    column = pos + 1 if last_newline < 0 else pos - last_newline
    return line, column


def compile_pattern(search_text: str, use_regex: bool) -> re.Pattern[str]:
    pattern = search_text if use_regex else re.escape(search_text)
    return re.compile(pattern, re.MULTILINE)


def analyze_content(
    content: str,
    search_text: str,
    replace_text: str,
    use_regex: bool,
) -> tuple[int, list[MatchInfo], bool]:
    regex = compile_pattern(search_text, use_regex)
    matches: list[MatchInfo] = []
    count = 0
    limited = False

    for match in regex.finditer(content):
        count += 1
        if len(matches) >= MAX_LOG_MATCHES_PER_FILE:
            limited = True
            continue

        line, column = line_col_from_pos(content, match.start())
        if use_regex:
            try:
                replacement_preview = match.expand(replace_text)
            except re.error:
                # 無効な後方参照などは、置換時に正式にエラーにする。
                replacement_preview = replace_text
        else:
            replacement_preview = replace_text

        matches.append(
            MatchInfo(
                line=line,
                column=column,
                matched=match.group(0),
                replacement=replacement_preview,
            )
        )

    return count, matches, limited


def replace_content(
    content: str,
    search_text: str,
    replace_text: str,
    use_regex: bool,
) -> tuple[str, int]:
    regex = compile_pattern(search_text, use_regex)
    return regex.subn(replace_text, content)


def make_backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        return backup

    for i in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak{i}")
        if not candidate.exists():
            shutil.copy2(path, candidate)
            return candidate

    raise RuntimeError("バックアップファイル名を決められませんでした")


class TextBulkReplacerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x790")
        self.root.minsize(880, 650)

        self.settings = load_settings()
        self.history: list[dict[str, object]] = self._sanitize_history(self.settings.get("history", []))

        self.folder_var = tk.StringVar(value=str(self.settings.get("folder", "")))
        self.pattern_var = tk.StringVar(value=str(self.settings.get("patterns", "*.txt")))
        self.regex_var = tk.BooleanVar(value=bool(self.settings.get("use_regex", False)))
        self.backup_var = tk.BooleanVar(value=bool(self.settings.get("make_backup", True)))
        encoding_value = str(self.settings.get("encoding", "自動判別"))
        if encoding_value not in ENCODING_MAP:
            encoding_value = "自動判別"
        self.encoding_var = tk.StringVar(value=encoding_value)
        self.status_var = tk.StringVar(value="準備OK")
        self.history_var = tk.StringVar()

        self._build_ui()
        self._load_last_replace_texts()
        self.refresh_history_combo()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        top = ttk.Frame(root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="対象フォルダ").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        folder_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="フォルダ指定...", command=self.choose_folder).grid(
            row=0, column=2, sticky="ew", padx=(8, 0), pady=4
        )

        if DND_AVAILABLE:
            folder_entry.drop_target_register(DND_FILES)  # type: ignore[arg-type]
            folder_entry.dnd_bind("<<Drop>>", self.on_drop_folder)  # type: ignore[attr-defined]

        ttk.Label(top, text="対象ファイル").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.pattern_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(top, text="例: *.txt;*.md;*.csv").grid(row=1, column=2, sticky="w", padx=(8, 0), pady=4)

        options = ttk.Frame(top)
        options.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Checkbutton(options, text="正規表現を使う", variable=self.regex_var).pack(side="left")
        ttk.Checkbutton(options, text="置換前に .bak を作成", variable=self.backup_var).pack(side="left", padx=(18, 0))
        ttk.Label(options, text="文字コード").pack(side="left", padx=(24, 6))
        ttk.Combobox(
            options,
            textvariable=self.encoding_var,
            values=list(ENCODING_MAP.keys()),
            width=14,
            state="readonly",
        ).pack(side="left")

        replace_frame = ttk.LabelFrame(root, text="置換条件", padding=10)
        replace_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        replace_frame.columnconfigure(0, weight=1)
        replace_frame.columnconfigure(1, weight=1)

        ttk.Label(replace_frame, text="置換前").grid(row=0, column=0, sticky="w")
        ttk.Label(replace_frame, text="置換後").grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.search_text = tk.Text(replace_frame, height=4, wrap="word", undo=True)
        self.search_text.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.replace_text = tk.Text(replace_frame, height=4, wrap="word", undo=True)
        self.replace_text.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))

        history_frame = ttk.Frame(replace_frame)
        history_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        history_frame.columnconfigure(1, weight=1)
        ttk.Label(history_frame, text="履歴").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.history_combo = ttk.Combobox(
            history_frame,
            textvariable=self.history_var,
            values=[],
            state="readonly",
        )
        self.history_combo.grid(row=0, column=1, sticky="ew")
        self.history_combo.bind("<<ComboboxSelected>>", self.on_history_selected)
        ttk.Button(history_frame, text="履歴を適用", command=self.apply_selected_history).grid(
            row=0, column=2, padx=(8, 0)
        )

        log_frame = ttk.LabelFrame(root, text="ログ", padding=10)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap="none", undo=False)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(font=("Consolas", 10))

        buttons = ttk.Frame(root, padding=(10, 0, 10, 10))
        buttons.grid(row=3, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)

        ttk.Label(buttons, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="ログをクリア", command=self.clear_log).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(buttons, text="調査", command=self.run_survey).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(buttons, text="置換", command=lambda: self.run(replace=True)).grid(row=0, column=3, padx=(8, 0))

        self.log(
            "使い方: フォルダ・対象ファイル・置換条件を指定して、まずは [調査]。問題なければ [置換]。\n"
            "対象ファイルは ; 区切りで複数指定できます。例: *.txt;*.md\n"
            "SJISはWindows実用上 CP932 として扱います。\n"
        )
        if DND_AVAILABLE:
            self.log("フォルダ欄へのドラッグ＆ドロップが使えます。\n")
        else:
            self.log("D&Dを使う場合は `pip install tkinterdnd2` を入れてください。未導入でも通常操作はできます。\n")

    def _sanitize_history(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, object]] = []
        seen: set[tuple[str, str, bool]] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            search = str(item.get("search", ""))
            replace = str(item.get("replace", ""))
            use_regex = bool(item.get("use_regex", False))
            if not search:
                continue
            key = (search, replace, use_regex)
            if key in seen:
                continue
            seen.add(key)
            result.append({"search": search, "replace": replace, "use_regex": use_regex})
            if len(result) >= HISTORY_LIMIT:
                break
        return result

    def _load_last_replace_texts(self) -> None:
        search = str(self.settings.get("last_search", ""))
        replace = str(self.settings.get("last_replace", ""))
        if search:
            self.search_text.insert("1.0", search)
        if replace:
            self.replace_text.insert("1.0", replace)

    def make_history_label(self, item: dict[str, object]) -> str:
        prefix = "正規" if bool(item.get("use_regex", False)) else "通常"
        search = one_line_preview(str(item.get("search", "")), 48)
        replace = one_line_preview(str(item.get("replace", "")), 48)
        return f"[{prefix}] {search}  =>  {replace}"

    def refresh_history_combo(self) -> None:
        values = [self.make_history_label(item) for item in self.history]
        self.history_combo.configure(values=values)
        if values:
            self.history_var.set(values[0])
        else:
            self.history_var.set("")

    def on_history_selected(self, _event=None) -> None:  # type: ignore[no-untyped-def]
        self.apply_selected_history()

    def apply_selected_history(self) -> None:
        index = self.history_combo.current()
        if index < 0 or index >= len(self.history):
            return
        item = self.history[index]
        self.search_text.delete("1.0", "end")
        self.search_text.insert("1.0", str(item.get("search", "")))
        self.replace_text.delete("1.0", "end")
        self.replace_text.insert("1.0", str(item.get("replace", "")))
        self.regex_var.set(bool(item.get("use_regex", False)))
        self.status_var.set("履歴を適用しました")

    def add_history(self, search_text: str, replace_text: str, use_regex: bool) -> None:
        if search_text == "":
            return
        key = (search_text, replace_text, use_regex)
        if self.history and (
            str(self.history[0].get("search", "")),
            str(self.history[0].get("replace", "")),
            bool(self.history[0].get("use_regex", False)),
        ) == key:
            return

        # 同じ内容は履歴に重複登録しない。既存にあれば先頭へ移動。
        new_history: list[dict[str, object]] = []
        for item in self.history:
            item_key = (
                str(item.get("search", "")),
                str(item.get("replace", "")),
                bool(item.get("use_regex", False)),
            )
            if item_key != key:
                new_history.append(item)
        self.history = [{"search": search_text, "replace": replace_text, "use_regex": use_regex}] + new_history
        self.history = self.history[:HISTORY_LIMIT]
        self.refresh_history_combo()

    def collect_settings(self) -> dict:
        return {
            "folder": self.folder_var.get().strip(),
            "patterns": self.pattern_var.get().strip(),
            "use_regex": self.regex_var.get(),
            "make_backup": self.backup_var.get(),
            "encoding": self.encoding_var.get(),
            "last_search": self.get_text_value(self.search_text),
            "last_replace": self.get_text_value(self.replace_text),
            "history": self.history,
        }

    def save_current_settings(self) -> None:
        save_settings(self.collect_settings())

    def on_close(self) -> None:
        self.save_current_settings()
        self.root.destroy()

    def run_survey(self) -> None:
        self.clear_log()
        self.run(replace=False)

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="対象フォルダを選択")
        if folder:
            self.folder_var.set(folder)

    def on_drop_folder(self, event) -> None:  # type: ignore[no-untyped-def]
        path_text = normalize_dropped_path(event.data)
        path = Path(path_text)
        if path.is_file():
            path = path.parent
        self.folder_var.set(str(path))

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")

    def log(self, message: str) -> None:
        self.log_text.insert("end", message)
        if not message.endswith("\n"):
            self.log_text.insert("end", "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def get_text_value(self, widget: tk.Text) -> str:
        # Text末尾の自動改行だけ落とす。ユーザーが入力した途中の改行は維持。
        value = widget.get("1.0", "end-1c")
        return value

    def validate_inputs(self) -> Optional[tuple[Path, list[str], str, str, bool, str]]:
        folder = Path(self.folder_var.get().strip())
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("エラー", "対象フォルダを指定してください。")
            return None

        patterns = parse_patterns(self.pattern_var.get())
        search_text = self.get_text_value(self.search_text)
        replace_text = self.get_text_value(self.replace_text)
        use_regex = self.regex_var.get()
        encoding_choice = self.encoding_var.get()

        if search_text == "":
            messagebox.showerror("エラー", "置換前の文字列を入力してください。")
            return None

        try:
            compile_pattern(search_text, use_regex)
        except re.error as exc:
            messagebox.showerror("正規表現エラー", f"正規表現が不正です。\n\n{exc}")
            return None

        if use_regex:
            try:
                re.compile(search_text).sub(replace_text, "")
            except re.error as exc:
                messagebox.showerror("置換後文字列エラー", f"置換後の指定が不正です。\n\n{exc}")
                return None

        return folder, patterns, search_text, replace_text, use_regex, encoding_choice

    def run(self, replace: bool) -> None:
        params = self.validate_inputs()
        if params is None:
            return

        folder, patterns, search_text, replace_text, use_regex, encoding_choice = params
        self.add_history(search_text, replace_text, use_regex)
        self.save_current_settings()
        mode_name = "置換" if replace else "調査"
        self.status_var.set(f"{mode_name}中...")
        self.root.update_idletasks()

        self.log("\n" + "=" * 80)
        self.log(f"[{mode_name}開始]")
        self.log(f"対象フォルダ: {folder}")
        self.log(f"対象ファイル: {';'.join(patterns)}")
        self.log(f"正規表現: {'ON' if use_regex else 'OFF'}")
        self.log(f"文字コード: {encoding_choice}")
        if replace:
            self.log(f"バックアップ: {'ON' if self.backup_var.get() else 'OFF'}")

        files = iter_target_files(folder, patterns)
        self.log(f"検出ファイル数: {len(files)}")

        if not files:
            self.log("対象ファイルが見つかりませんでした。")
            self.status_var.set("対象ファイルなし")
            return

        matched_files = 0
        total_matches = 0
        replaced_files = 0
        errors = 0

        for index, path in enumerate(files, start=1):
            rel = path.relative_to(folder)
            try:
                enc, content, _raw = decode_file(path, encoding_choice)
                match_count, matches, limited = analyze_content(content, search_text, replace_text, use_regex)

                if match_count <= 0:
                    continue

                matched_files += 1
                total_matches += match_count
                self.log(f"\n[{rel}] encoding={enc} matches={match_count}")
                for info in matches:
                    self.log(
                        f"  L{info.line}:C{info.column}  "
                        f"{preview_text(info.matched)}  =>  {preview_text(info.replacement)}"
                    )
                if limited:
                    self.log(f"  ...ログ表示は {MAX_LOG_MATCHES_PER_FILE} 件で省略。実際の一致数: {match_count}")

                if replace:
                    new_content, replaced_count = replace_content(content, search_text, replace_text, use_regex)
                    if replaced_count != match_count:
                        self.log(f"  注意: 調査一致数 {match_count} / 置換数 {replaced_count}")

                    if new_content != content:
                        if self.backup_var.get():
                            backup_path = make_backup(path)
                            self.log(f"  backup: {backup_path.name}")
                        path.write_bytes(new_content.encode(enc))
                        replaced_files += 1
                        self.log("  replaced: OK")

            except Exception as exc:
                errors += 1
                self.log(f"\n[ERROR] {rel}")
                self.log(f"  {type(exc).__name__}: {exc}")
                # 予期しないエラーだけ詳細を出す。文字コード系は本文だけで十分。
                if not isinstance(exc, UnicodeDecodeError):
                    self.log("  " + traceback.format_exc().replace("\n", "\n  ").rstrip())

            if index % 25 == 0:
                self.status_var.set(f"{mode_name}中... {index}/{len(files)}")
                self.root.update_idletasks()

        self.log("\n" + "-" * 80)
        self.log(f"[{mode_name}完了]")
        self.log(f"走査ファイル数: {len(files)}")
        self.log(f"一致ファイル数: {matched_files}")
        self.log(f"一致箇所数: {total_matches}")
        if replace:
            self.log(f"置換ファイル数: {replaced_files}")
        self.log(f"エラー数: {errors}")

        self.status_var.set(
            f"{mode_name}完了: 一致ファイル {matched_files}, 一致箇所 {total_matches}, エラー {errors}"
        )

        if replace:
            messagebox.showinfo(
                "完了",
                f"置換が完了しました。\n\n一致ファイル: {matched_files}\n一致箇所: {total_matches}\n置換ファイル: {replaced_files}\nエラー: {errors}",
            )


def main() -> None:
    if DND_AVAILABLE and TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    # Windowsで日本語フォントが崩れにくいように少しだけ調整。
    try:
        default_font = ("Yu Gothic UI", 10)
        root.option_add("*Font", default_font)
    except Exception:
        pass

    TextBulkReplacerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
