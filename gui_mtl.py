# -*- coding: utf-8 -*-
"""Landsat 元数据修复工具 —— 窗口版（tkinter，UI v2.1）。

界面层：深蓝灰 + 状态色、圆角卡片、整页滚动兜底、可滚动明细、窗口尺寸记忆。
修复逻辑全部复用 fix_mtl.py；有 tkinterdnd2 时支持拖拽，没装时降级为点击选择。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import fix_mtl

APP_TITLE = "Landsat 元数据修复工具"
APP_VERSION = "2.1"
SUBTITLE = "把 Landsat 8/9 的 *_MTL.txt 拖进来，自动备份并生成 ENVI 可用元数据"

# ---------------------------------------------------------------- 视觉规范
BG = "#F3F5F9"
CARD = "#FFFFFF"
BORDER = "#DCE3EC"
PRIMARY = "#22384F"
PRIMARY_ACTIVE = "#2E4C69"
ACCENT = "#2F6FB2"
TEXT = "#1B2733"
MUTED = "#66758A"
SUCCESS = "#1E8E5A"
SUCCESS_BG = "#EAF6EF"
WARN = "#A86A00"
WARN_BG = "#FCF3DE"
DANGER = "#B83227"
DANGER_BG = "#FCEAE8"
DROP_BG = "#EEF4FB"
DROP_BG_HOVER = "#E1EFFC"
ROW_HOVER = "#F5F8FC"
MONO_FONTS = ("Consolas", "Cascadia Mono", "Courier New")

FALLBACK_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                  "PingFang SC", "Noto Sans CJK SC")
BASE_W, BASE_H = 980, 760
DETAILS_HEIGHT = 180


def enable_high_dpi() -> None:
    """Windows 高 DPI：进程声明为 system-aware，避免 125%/150% 下被系统拉伸变糊。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------------------------------------ 纯函数（可测）
def compute_window_geometry(screen_w: int, screen_h: int,
                            dpi_scale: float = 1.0) -> tuple[int, int, int, int]:
    """按屏幕与缩放比计算初始窗口 (宽, 高, x, y)，保证不超出可用区域。"""
    scale = max(0.75, min(dpi_scale, 2.0))
    width = int(round(BASE_W * scale))
    height = int(round(BASE_H * scale))
    max_w = int(screen_w * 0.92)
    max_h = int(screen_h * 0.9)
    min_w = min(int(round(640 * scale)), max_w)
    min_h = min(int(round(520 * scale)), max_h)
    width = max(min(width, max_w), min(min_w, max_w))
    height = max(min(height, max_h), min(min_h, max_h))
    x = max((screen_w - width) // 2, 0)
    y = max((screen_h - height) // 3, 0)
    return width, height, x, y


def default_settings_path() -> Path:
    override = os.environ.get("LANDSAT_MTL_SETTINGS")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "LandsatMTLRepair" / "settings.json"
    return Path.home() / ".landsat_mtl_repair" / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    target = Path(path) if path else default_settings_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict, path: Path | None = None) -> bool:
    target = Path(path) if path else default_settings_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def parse_geometry(text: str) -> tuple[int, int, int, int] | None:
    match = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(text or "").strip())
    if not match:
        return None
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


def geometry_is_visible(geom: tuple[int, int, int, int], screen_w: int,
                        screen_h: int) -> bool:
    _w, _h, x, y = geom
    return -50 <= x < screen_w - 100 and 0 <= y < screen_h - 80


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except Exception:  # pragma: no cover - 取决于本机环境
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False


def gather_mtl_paths(raw_paths) -> list[Path]:
    """把拖入/选中的路径整理成 MTL 文件列表；目录则向下查找。"""
    result: list[Path] = []
    for raw in raw_paths:
        path = Path(str(raw).strip().strip("{}"))
        if path.is_dir():
            for pattern in ("*_MTL.txt", "*_MTL.MTL", "*_MTL.mtl"):
                result.extend(sorted(path.glob(pattern)))
        elif path.is_file() and path.suffix.lower() in {".txt", ".mtl"}:
            result.append(path)
    seen = set()
    unique: list[Path] = []
    for item in result:
        key = str(item.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


class RoundedCard(tk.Canvas):
    """圆角卡片：Canvas 画底 + 内嵌 Frame 承载实际控件。"""

    def __init__(self, master, *, bg=CARD, radius=12, padding=16, **kwargs):
        super().__init__(master, bg=master["bg"], highlightthickness=0, bd=0, **kwargs)
        self.card_bg = bg
        self.radius = radius
        self.padding = padding
        self.body = tk.Frame(self, bg=bg)
        self._window = self.create_window(padding, padding, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        self.delete("card_shape")
        self._round_rect(2, 2, event.width - 2, event.height - 2,
                         self.radius, fill=self.card_bg, outline=BORDER, tags="card_shape")
        self.itemconfigure(self._window, width=max(event.width - 2 * self.padding, 1),
                           height=max(event.height - 2 * self.padding, 1))
        self.tag_lower("card_shape")

    def _round_rect(self, x1, y1, x2, y2, r, **kw) -> None:
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self.create_polygon(pts, smooth=True, **kw)


class MTLApp:
    def __init__(self, root) -> None:
        self.root = root
        self.files: list[Path] = []
        self.mode_var = tk.StringVar(value="auto")
        self.product_var = tk.BooleanVar(value=False)
        self.out_var = tk.StringVar(value="inplace")
        self.summary_var = tk.StringVar(value="尚未开始")
        self.current_var = tk.StringVar(value="")
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._details_open = False
        self._traceback_visible = False
        self._has_traceback = False
        self._interactive: list = []
        self._last_outputs: list[Path] = []
        self._user_resized = False
        self._programmatic = False
        self._ready = False
        self._save_job = None
        self._pre_expand_height = 0
        self._name_labels: list[tk.Label] = []
        self._scrollers: set = set()
        self._fitting_options = False
        self._fitting_result = False

        self.font_family = self._pick_font(root)
        self.font_mono = self._pick_mono(root)
        root.title(APP_TITLE)
        self._setup_theme()
        self._build_ui()
        self._setup_dnd()
        self._refresh_rows()
        self._apply_initial_geometry()
        self._bind_global_events()

    # ------------------------------------------------------------- 基础样式
    def _pick_font(self, root) -> str:
        try:
            available = set(tkfont.families(root))
        except Exception:
            available = set()
        for name in FALLBACK_FONTS:
            if name in available:
                return name
        return "TkDefaultFont"

    def _pick_mono(self, root) -> str:
        try:
            available = set(tkfont.families(root))
        except Exception:
            available = set()
        for name in MONO_FONTS:
            if name in available:
                return name
        return "Courier New"

    def _setup_theme(self) -> None:
        fam = self.font_family
        self.font_title = (fam, 18, "bold")
        self.font_subtitle = (fam, 10)
        self.font_section = (fam, 11, "bold")
        self.font_body = (fam, 10)
        self.font_small = (fam, 9)
        self.font_badge = (fam, 9, "bold")
        self.font_button = (fam, 10)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=self.font_body, background=CARD, foreground=TEXT)
        style.configure("TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=self.font_small)
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=self.font_section)
        style.configure("TRadiobutton", background=CARD, foreground=TEXT,
                        font=self.font_body, focuscolor=CARD)
        style.map("TRadiobutton", background=[("active", CARD)])
        style.configure("TCheckbutton", background=CARD, foreground=TEXT, font=self.font_small)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TButton", font=self.font_button, padding=(12, 7),
                        background="#E8EEF6", foreground=TEXT, borderwidth=0)
        style.map("TButton",
                  background=[("disabled", "#EDF0F4"), ("active", "#D9E4F0")],
                  foreground=[("disabled", "#9AA7B8")])
        style.configure("Primary.TButton", font=(fam, 10, "bold"), padding=(18, 7),
                        background=PRIMARY, foreground="#FFFFFF", borderwidth=0)
        style.map("Primary.TButton",
                  background=[("disabled", "#9FAEBD"), ("active", PRIMARY_ACTIVE)],
                  foreground=[("disabled", "#F2F4F6")])
        style.configure("Horizontal.TProgressbar", troughcolor="#E9EEF5",
                        background=ACCENT, bordercolor=CARD,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

    # ---------------------------------------------------------------- 布局
    def _build_ui(self) -> None:
        self.root.configure(bg=BG)
        self.page_canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.page_scrollbar = ttk.Scrollbar(self.root, orient="vertical",
                                            command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        # 先放滚动条再放画布，确保窗口变窄时画布不会把滚动条挤成 0 宽
        self.page_scrollbar.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)

        self.page_frame = tk.Frame(self.page_canvas, bg=BG)
        self._page_window = self.page_canvas.create_window((0, 0), window=self.page_frame,
                                                          anchor="nw")
        self.page_frame.bind("<Configure>", self._on_page_frame_configure)
        self.page_canvas.bind("<Configure>", self._on_page_canvas_configure)

        self._build_header()
        self._build_body()
        self._build_options()
        self._build_actions()
        self._build_result()
        self._bind_wheel_tree(self.page_frame, self._on_page_wheel)
        self._scrollers.update({self.page_canvas, self.list_canvas, self.output})

    def _build_header(self) -> None:
        header = tk.Frame(self.page_frame, bg=BG)
        header.pack(fill="x", padx=20, pady=(18, 10))
        header.grid_columnconfigure(0, weight=1)

        left = tk.Frame(header, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=self.font_title).pack(anchor="w")
        self.subtitle_label = tk.Label(left, text=SUBTITLE, bg=BG, fg=MUTED,
                                       font=self.font_subtitle, justify="left", anchor="w")
        self.subtitle_label.pack(anchor="w", pady=(4, 0))

        tk.Label(header, text=f"v{APP_VERSION}", bg=BG, fg="#8A98AA",
                 font=(self.font_family, 9)).grid(row=0, column=1, sticky="ne")

    def _build_body(self) -> None:
        self.body_card = RoundedCard(self.page_frame, height=330)
        self.body_card.pack(fill="x", padx=20)
        body = self.body_card.body

        self.drop_canvas = tk.Canvas(body, bg=CARD, highlightthickness=0,
                                     height=112, cursor="hand2")
        self.drop_canvas.pack(fill="x")
        self.drop_canvas.bind("<Configure>", lambda _e: self._render_drop())
        self.drop_canvas.bind("<Button-1>", lambda _e: self.choose_files())
        self.drop_canvas.bind("<Enter>", lambda _e: self._set_drop_hover(True))
        self.drop_canvas.bind("<Leave>", lambda _e: self._set_drop_hover(False))
        self._drop_hover = False
        self._drop_drag = False
        self._render_drop()

        list_head = tk.Frame(body, bg=CARD)
        list_head.pack(fill="x", pady=(10, 4))
        tk.Label(list_head, text="待处理文件", bg=CARD, fg=TEXT,
                 font=self.font_section).pack(side="left")
        self.count_label = tk.Label(list_head, text="0 个", bg=CARD, fg=MUTED,
                                    font=self.font_small)
        self.count_label.pack(side="left", padx=(8, 0))
        self.clear_btn = tk.Button(list_head, text="清空", command=self.clear_files,
                                   bg=CARD, fg=ACCENT, activebackground=CARD,
                                   activeforeground=PRIMARY, relief="flat", bd=0,
                                   cursor="hand2", font=self.font_small)
        self.clear_btn.pack(side="right")
        self._interactive.append(self.clear_btn)

        list_wrap = tk.Frame(body, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(list_wrap, bg=CARD, highlightthickness=0)
        self.list_scrollbar = ttk.Scrollbar(list_wrap, orient="vertical",
                                            command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=self.list_scrollbar.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)

        self.rows_frame = tk.Frame(self.list_canvas, bg=CARD)
        self._rows_window = self.list_canvas.create_window((0, 0), window=self.rows_frame,
                                                          anchor="nw")
        self.rows_frame.bind("<Configure>", lambda _e: self._sync_list_scroll())
        self.list_canvas.bind("<Configure>", self._on_list_configure)

    def _build_options(self) -> None:
        self.options_card = RoundedCard(self.page_frame, height=166)
        self.options_card.pack(fill="x", padx=20, pady=(12, 0))
        body = self.options_card.body
        body.grid_columnconfigure(0, minsize=76)
        body.grid_columnconfigure(1, weight=1)

        tk.Label(body, text="补丁模式", bg=CARD, fg=TEXT,
                 font=self.font_section).grid(row=0, column=0, sticky="w")
        modes = tk.Frame(body, bg=CARD)
        modes.grid(row=0, column=1, sticky="w", padx=(16, 0))
        for text, value in (("自动（推荐）", "auto"),
                            ("兼容模式", "compat"),
                            ("最小补丁", "minimal")):
            rb = ttk.Radiobutton(modes, text=text, value=value, variable=self.mode_var,
                                 command=self._refresh_rows)
            rb.pack(side="left", padx=(0, 14))
            self._interactive.append(rb)

        self.auto_hint = tk.Label(
            body, text="自动判定：Collection 2 或 Level-2 → 兼容模式；Collection 1 + Level-1 → 最小补丁。",
            bg=CARD, fg=MUTED, font=self.font_small, justify="left", anchor="w")
        self.auto_hint.grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(0, 8))

        tk.Label(body, text="输出策略", bg=CARD, fg=TEXT,
                 font=self.font_section).grid(row=2, column=0, sticky="w")
        outs = tk.Frame(body, bg=CARD)
        outs.grid(row=2, column=1, sticky="w", padx=(16, 0))
        for text, value in (("原地覆盖（推荐）", "inplace"),
                            ("另存留档（ENVI 无法识别）", "envi56")):
            rb = ttk.Radiobutton(outs, text=text, value=value, variable=self.out_var)
            rb.pack(side="left", padx=(0, 14))
            self._interactive.append(rb)

        self.out_hint = tk.Label(body, text="", bg=CARD, fg=MUTED, font=self.font_small,
                                 justify="left", anchor="w")
        self.out_hint.grid(row=3, column=1, sticky="w", padx=(16, 0), pady=(0, 8))

        self.product_check = ttk.Checkbutton(
            body, text="同时把产品号 LC09 改为 LC08", variable=self.product_var)
        self.product_check.grid(row=4, column=1, sticky="w", padx=(16, 0), pady=(2, 0))
        self._interactive.append(self.product_check)

        self.options_card.bind("<Configure>", self._on_options_configure, add="+")

    def _build_actions(self) -> None:
        bar = tk.Frame(self.page_frame, bg=BG)
        bar.pack(fill="x", padx=20, pady=(12, 0))
        bar.grid_columnconfigure(0, weight=1)

        left = tk.Frame(bar, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        for text, command in (("还原备份", self.restore),
                              ("诊断", self.diagnose),
                              ("目录自检", self.self_check),
                              ("打开备份目录", self.open_backup_dir)):
            btn = ttk.Button(left, text=text, command=command, width=12)
            btn.pack(side="left", padx=(0, 8))
            self._interactive.append(btn)

        self.start_btn = ttk.Button(bar, text="开始修复", style="Primary.TButton",
                                    width=12, command=self.run_repair)
        self.start_btn.grid(row=0, column=1, sticky="e")
        self._interactive.append(self.start_btn)

    def _build_result(self) -> None:
        self.result_card = RoundedCard(self.page_frame, height=112)
        self.result_card.pack(fill="x", padx=20, pady=(12, 20))
        body = self.result_card.body

        self.progress_frame = tk.Frame(body, bg=CARD)
        self.progress = ttk.Progressbar(self.progress_frame, mode="indeterminate",
                                        style="Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        tk.Label(self.progress_frame, textvariable=self.current_var, bg=CARD, fg=MUTED,
                 font=self.font_small, anchor="w", justify="left").pack(fill="x", pady=(2, 6))

        self.banner = tk.Frame(body, bg=DANGER_BG)
        self.banner_title = tk.Label(self.banner, text="", bg=DANGER_BG, fg=DANGER,
                                     font=(self.font_family, 10, "bold"), anchor="w",
                                     justify="left")
        self.banner_title.pack(fill="x", padx=12, pady=(8, 0))
        self.banner_text = tk.Label(self.banner, text="", bg=DANGER_BG, fg="#7B3A33",
                                    font=self.font_small, anchor="w", justify="left",
                                    wraplength=820)
        self.banner_text.pack(fill="x", padx=12, pady=(2, 8))

        self.summary_row = tk.Frame(body, bg=CARD)
        self.summary_row.pack(fill="x")
        self.status_dot = tk.Label(self.summary_row, text="●", bg=CARD, fg=MUTED,
                                   font=(self.font_family, 13, "bold"))
        self.status_dot.pack(side="left")
        tk.Label(self.summary_row, textvariable=self.summary_var, bg=CARD, fg=TEXT,
                 font=(self.font_family, 10, "bold"), anchor="w").pack(side="left", padx=(6, 0))
        self.detail_btn = tk.Button(self.summary_row, text="展开明细 ▾", command=self.toggle_details,
                                    bg=CARD, fg=ACCENT, activebackground=CARD,
                                    activeforeground=PRIMARY, relief="flat", bd=0,
                                    cursor="hand2", font=self.font_small)
        self.detail_btn.pack(side="right")
        self.reveal_btn = tk.Button(self.summary_row, text="在资源管理器中显示", command=self.reveal,
                                    bg=CARD, fg=ACCENT, activebackground=CARD,
                                    activeforeground=PRIMARY, relief="flat", bd=0,
                                    cursor="hand2", font=self.font_small)

        self.details_frame = tk.Frame(body, bg=CARD, height=DETAILS_HEIGHT)
        self.details_frame.pack_propagate(False)
        self.tb_bar = tk.Frame(self.details_frame, bg=CARD)
        self.tb_label = tk.Label(self.tb_bar, text="技术明细已折叠", bg=CARD, fg=MUTED,
                                 font=self.font_small, anchor="w")
        self.tb_label.pack(side="left")
        self.tb_btn = tk.Button(self.tb_bar, text="展开技术明细", command=self.toggle_traceback,
                                bg=CARD, fg=ACCENT, activebackground=CARD,
                                activeforeground=PRIMARY, relief="flat", bd=0,
                                cursor="hand2", font=self.font_small)
        self.tb_btn.pack(side="right")

        self.text_wrap = tk.Frame(self.details_frame, bg=CARD)
        self.text_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.output = tk.Text(self.text_wrap, height=9, wrap="word", state="disabled",
                              background="#FAFBFD", relief="flat", bd=0,
                              font=(self.font_mono, 9), foreground="#33414F",
                              spacing1=1, spacing3=2,
                              highlightthickness=1, highlightbackground=BORDER)
        self.output_scroll = ttk.Scrollbar(self.text_wrap, orient="vertical",
                                           command=self.output.yview)
        self.output.configure(yscrollcommand=self.output_scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        self.output_scroll.pack(side="right", fill="y")
        self.output.tag_configure("info", foreground="#33414F")
        self.output.tag_configure("warning", foreground="#8A5A00")
        self.output.tag_configure("error", foreground="#A93226")
        self.output.tag_configure("traceback", foreground="#75828F", elide=True)
        self.output.bind("<MouseWheel>", self._on_output_wheel)

        self.result_card.bind("<Configure>", self._on_result_configure, add="+")

    # ------------------------------------------------------------ 整页滚动
    def _on_page_frame_configure(self, _event) -> None:
        self._update_page_scrollregion()

    def _on_page_canvas_configure(self, event) -> None:
        self.page_canvas.itemconfigure(self._page_window, width=event.width)
        narrow = event.width < 900
        target_drop = 96 if narrow else 112
        if int(float(self.drop_canvas.cget("height"))) != target_drop:
            self.drop_canvas.configure(height=target_drop)
            self._render_drop()
        self.subtitle_label.configure(wraplength=max(event.width - 170, 320))
        self._update_page_scrollregion()

    def _sync_page_scrollbar(self) -> None:
        """整页滚动条常驻（兜底滚动）：窗口再小也能滚到每一块。"""
        if not self.page_scrollbar.winfo_manager():
            self.page_scrollbar.pack(side="right", fill="y", before=self.page_canvas)
            self.page_canvas.pack_configure(side="left", fill="both", expand=True)

    def _on_page_wheel(self, event) -> str:
        self._scroll_widget(self.page_canvas, event)
        return "break"

    def _on_list_wheel(self, event) -> str:
        self._scroll_widget(self.list_canvas, event)
        return "break"

    def _on_output_wheel(self, event) -> str:
        self._scroll_widget(self.output, event)
        return "break"

    @staticmethod
    def _scroll_widget(widget, event) -> None:
        delta = getattr(event, "delta", 0)
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif delta:
            step = -1 if delta > 0 else 1
        else:
            return
        try:
            widget.yview_scroll(step, "units")
        except Exception:
            pass

    def _bind_wheel_tree(self, widget, handler) -> None:
        if widget in getattr(self, "_scrollers", set()):
            return
        if widget is getattr(self, "list_canvas", None) or widget is getattr(self, "output", None):
            return
        try:
            widget.bind("<MouseWheel>", handler)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_wheel_tree(child, handler)

    # ------------------------------------------------------------ 拖拽绘制
    def _render_drop(self) -> None:
        c = self.drop_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        height = int(c.cget("height"))
        bottom = height - 8
        bg = DROP_BG_HOVER if (self._drop_hover or self._drop_drag) else DROP_BG
        outline = ACCENT if (self._drop_hover or self._drop_drag) else "#8FAFCC"
        c.create_rectangle(4, 4, w - 4, bottom, dash=(6, 4), width=2,
                           outline=outline, fill=bg, tags="drop")
        cy = 22 + (height - 112) / 2
        c.create_oval(w / 2 - 17, cy, w / 2 + 17, cy + 34, outline=ACCENT,
                      width=2, fill=bg, tags="drop")
        c.create_text(w / 2, cy + 17, text="＋", fill=ACCENT,
                      font=(self.font_family, 15, "bold"), tags="drop")
        title = "松开即可加入" if self._drop_drag else "把 *_MTL.txt 或影像文件夹拖到这里"
        c.create_text(w / 2, cy + 50, text=title, fill=TEXT,
                      font=(self.font_family, 11, "bold"), tags="drop")
        sub = ("支持多文件；也可以点击此处选择" if HAS_DND
               else "未安装拖拽组件，点击此处选择文件")
        c.create_text(w / 2, cy + 70, text=sub, fill=MUTED,
                      font=self.font_small, tags="drop")

    def _set_drop_hover(self, hover: bool) -> None:
        self._drop_hover = hover
        self._render_drop()

    def _setup_dnd(self) -> None:
        if not HAS_DND:
            return
        for widget in (self.root, self.drop_canvas, self.list_canvas):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            except Exception:
                pass

    def _on_drop(self, event) -> None:
        self._drop_drag = False
        self._render_drop()
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.add_files(paths)

    def _on_drag_enter(self, _event) -> None:
        self._drop_drag = True
        self._render_drop()

    def _on_drag_leave(self, _event) -> None:
        self._drop_drag = False
        self._render_drop()

    # ------------------------------------------------------------- 文件列表
    def add_files(self, paths) -> None:
        found = gather_mtl_paths(paths)
        if not found:
            self._show_banner("warning", "没有可加入的文件",
                              "请拖入 *_MTL.txt 或包含它的影像文件夹。")
            self._set_summary("warning", "等待加入文件")
            return
        for path in found:
            if path not in self.files:
                self.files.append(path)
        self._refresh_rows()
        self._set_summary("neutral", f"已加入 {len(self.files)} 个文件，可以开始修复。")

    def choose_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="选择 Landsat MTL 文件",
            filetypes=[("Landsat MTL 元数据", "*_MTL.txt *_MTL.MTL *_MTL_ENVI56.txt"),
                       ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if chosen:
            self.add_files(chosen)

    def clear_files(self) -> None:
        if self._busy:
            return
        self.files.clear()
        self._refresh_rows()
        self._set_summary("neutral", "尚未开始")

    def remove_file(self, path: Path) -> None:
        if self._busy or path not in self.files:
            return
        self.files.remove(path)
        self._refresh_rows()
        self._set_summary("neutral", f"已移除 1 个文件，剩余 {len(self.files)} 个。")

    def _refresh_rows(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._name_labels = []
        self.count_label.configure(text=f"{len(self.files)} 个")
        if not self.files:
            empty = tk.Label(
                self.rows_frame,
                text="还没有文件。把 *_MTL.txt 或影像文件夹拖到上方，或点击选择。",
                bg=CARD, fg=MUTED, font=self.font_small, anchor="w", justify="left",
                wraplength=760)
            empty.pack(fill="x", padx=4, pady=10)
            self._name_labels.append(empty)
            self.rows_frame.update_idletasks()
            self._sync_list_scroll()
            self._update_row_wrap()
            return

        for path in self.files:
            scene, mode_text, fixed = self._describe_file(path)
            row = tk.Frame(self.rows_frame, bg=CARD, padx=4, pady=6)
            row.pack(fill="x", padx=2, pady=(0, 1))
            row.grid_columnconfigure(1, weight=1)

            dot = tk.Label(row, text="●", bg=CARD, fg=ACCENT, font=(self.font_family, 9))
            dot.grid(row=0, column=0, sticky="w", padx=(2, 8))

            text_box = tk.Frame(row, bg=CARD)
            text_box.grid(row=0, column=1, sticky="ew")
            name = tk.Label(text_box, text=path.name, bg=CARD, fg=TEXT,
                            font=(self.font_family, 10, "bold"), anchor="w", justify="left",
                            wraplength=640)
            name.pack(fill="x")
            meta = scene + ("　·　已是修复版" if fixed else "")
            meta_label = tk.Label(text_box, text=meta, bg=CARD, fg=MUTED,
                                  font=self.font_small, anchor="w", justify="left",
                                  wraplength=640)
            meta_label.pack(fill="x")
            self._name_labels.extend([name, meta_label])

            tone = {"兼容模式": (ACCENT, "#E8F0FA"),
                    "最小补丁": (SUCCESS, SUCCESS_BG),
                    "待检查": (WARN, WARN_BG)}.get(mode_text, (MUTED, "#F0F3F7"))
            badge = tk.Label(row, text=mode_text, bg=tone[1], fg=tone[0],
                             font=self.font_badge, padx=8, pady=2)
            badge.grid(row=0, column=2, padx=(8, 8))

            remove = tk.Button(row, text="✕", command=lambda p=path: self.remove_file(p),
                               bg=CARD, fg=MUTED, activebackground="#F3E4E2",
                               activeforeground=DANGER, relief="flat", bd=0,
                               cursor="hand2", font=self.font_small, width=2)
            remove.grid(row=0, column=3, sticky="e")

            hover_widgets = (row, dot, text_box, name, meta_label, badge, remove)
            for widget in hover_widgets:
                widget.bind("<Enter>", lambda _e, r=row: self._hover_row(r, True))
                widget.bind("<Leave>", lambda _e, r=row: self._hover_row(r, False))
                widget.bind("<MouseWheel>", self._on_list_wheel)
            self._bind_wheel_tree(row, self._on_list_wheel)

        self.rows_frame.update_idletasks()
        self._sync_list_scroll()
        self._update_row_wrap()

    def _update_row_wrap(self) -> None:
        width = max(self.list_canvas.winfo_width() - 150, 260)
        for label in self._name_labels:
            if label.winfo_exists():
                label.configure(wraplength=width)

    def _on_list_configure(self, event) -> None:
        self.list_canvas.itemconfigure(self._rows_window, width=event.width)
        self._update_row_wrap()
        self._sync_list_scroll()

    def _sync_list_scroll(self) -> None:
        self.list_canvas.update_idletasks()
        bbox = self.list_canvas.bbox("all")
        need = bool(bbox) and bbox[3] > self.list_canvas.winfo_height() + 2
        packed = bool(self.list_scrollbar.winfo_manager())
        if need and not packed:
            self.list_scrollbar.pack(side="right", fill="y")
            self.list_canvas.pack_configure(side="left", fill="both", expand=True)
        elif not need and packed:
            self.list_scrollbar.pack_forget()

    def _hover_row(self, row: tk.Frame, on: bool) -> None:
        tone = ROW_HOVER if on else CARD
        row.configure(bg=tone)
        for child in row.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(bg=tone)
            elif isinstance(child, tk.Frame):
                child.configure(bg=tone)
                for sub in child.winfo_children():
                    if isinstance(sub, tk.Label):
                        sub.configure(bg=tone)

    def _describe_file(self, path: Path) -> tuple[str, str, bool]:
        """返回 (景号, 模式文案, 是否已是修复版)。"""
        try:
            text, _ = fix_mtl.read_source(path)
            labels = fix_mtl.collect_labels(text.splitlines())
            scene = (fix_mtl._first_value(labels, "LANDSAT_PRODUCT_ID")
                     or fix_mtl._first_value(labels, "PRODUCT_ID")
                     or re.sub(r"_MTL$", "", path.stem))
            fixed = fix_mtl._spacecraft_state(text) == fix_mtl.ENVI_TARGET_NAME
            if self.mode_var.get() == "compat":
                mode = "兼容模式"
            elif self.mode_var.get() == "minimal":
                mode = "最小补丁"
            else:
                compat, _reason = fix_mtl.decide_compat(labels)
                mode = "兼容模式" if compat else "最小补丁"
        except Exception:
            scene = re.sub(r"_MTL$", "", path.stem)
            mode = "待检查"
            fixed = False
        return scene, mode, fixed

    # ------------------------------------------------------------- 状态提示
    def _set_summary(self, kind: str, text: str) -> None:
        colors = {"success": SUCCESS, "warning": WARN, "danger": DANGER, "neutral": MUTED}
        self.status_dot.configure(fg=colors.get(kind, MUTED))
        self.summary_var.set(text)

    def _show_banner(self, kind: str, title: str, suggestion: str) -> None:
        palette = {"danger": (DANGER_BG, DANGER, "#7B3A33"),
                   "warning": (WARN_BG, WARN, "#6B4A0A"),
                   "success": (SUCCESS_BG, SUCCESS, "#215C40")}.get(kind, (WARN_BG, WARN, "#6B4A0A"))
        self.banner.configure(bg=palette[0])
        self.banner_title.configure(bg=palette[0], fg=palette[1], text=title)
        self.banner_text.configure(bg=palette[0], fg=palette[2], text=suggestion)
        if not self.banner.winfo_manager():
            self.banner.pack(fill="x", before=self.summary_row)
        self._update_banner_wrap()
        self._fit_result_card()

    def _hide_banner(self) -> None:
        if self.banner.winfo_manager():
            self.banner.pack_forget()
        self._fit_result_card()

    def _update_banner_wrap(self) -> None:
        width = max(self.result_card.winfo_width() - 48, 280)
        self.banner_text.configure(wraplength=width)
        self.banner_title.configure(wraplength=width)

    def _on_result_configure(self, event) -> None:
        self._update_banner_wrap()
        self._fit_result_card()

    def _on_options_configure(self, event) -> None:
        wrap = max(event.width - 2 * self.options_card.padding - 120, 280)
        self.auto_hint.configure(wraplength=wrap)
        self.out_hint.configure(wraplength=wrap)
        self._fit_options_card()

    def _fit_card(self, card, minimum: int) -> bool:
        """按卡片内容实际需要设置高度，避免固定常量导致内容被裁切。"""
        card.body.update_idletasks()
        need = max(card.body.winfo_reqheight() + 2 * card.padding, int(minimum))
        if int(float(card["height"])) != need:
            card.configure(height=need)
            return True
        return False

    def _fit_options_card(self) -> None:
        if self._fitting_options:
            return
        self._fitting_options = True
        try:
            self._fit_card(self.options_card, 140)
            self._update_page_scrollregion()
        finally:
            self._fitting_options = False

    def _fit_result_card(self) -> None:
        if self._fitting_result:
            return
        self._fitting_result = True
        try:
            self._fit_card(self.result_card, 112)
            self._update_page_scrollregion()
        finally:
            self._fitting_result = False

    def _update_page_scrollregion(self) -> None:
        self.page_frame.update_idletasks()
        height = self.page_frame.winfo_reqheight()
        width = self.page_canvas.winfo_width() or self.page_frame.winfo_reqwidth()
        self.page_canvas.configure(scrollregion=(0, 0, width, max(height, 1)))
        self._sync_page_scrollbar()

    def _on_out_change(self) -> None:
        if self.out_var.get() == "envi56":
            self.out_hint.configure(
                fg=WARN,
                text="留档模式生成 *_MTL_ENVI56.txt；此命名 ENVI 无法识别，仅用于比对。")
        else:
            self.out_hint.configure(
                fg=MUTED,
                text="覆盖前会先备份原件到 _MTL_原始备份；ENVI 中请选择原名 *_MTL.txt。")

    def toggle_details(self) -> None:
        self._set_details_visible(not self._details_open)

    def _set_details_visible(self, open_: bool) -> None:
        if open_ == self._details_open:
            return
        self._details_open = open_
        if open_:
            self._pre_expand_height = self.root.winfo_height()
            self.details_frame.pack(fill="x", pady=(8, 0))
            if self._has_traceback and not self.tb_bar.winfo_manager():
                self.tb_bar.pack(fill="x", pady=(0, 2), before=self.text_wrap)
            self.detail_btn.configure(text="收起明细 ▴")
        else:
            self.details_frame.pack_forget()
            self.detail_btn.configure(text="展开明细 ▾")
        self._fit_result_card()
        self.root.update_idletasks()
        if not self._user_resized:
            if open_:
                self._set_window_height(self._natural_window_height())
            elif self._pre_expand_height:
                self._set_window_height(self._pre_expand_height)
        self._update_page_scrollregion()
        if open_:
            self.page_canvas.yview_moveto(1.0)

    def _natural_window_height(self) -> int:
        """展开明细时按内容需要增高窗口，上限为屏幕可用高度的 90%。"""
        self._update_page_scrollregion()
        need = self.page_frame.winfo_reqheight() + 4
        screen_h = self.root.winfo_screenheight()
        _min_w, min_h = self.root.minsize()
        return max(min(need, int(screen_h * 0.9)), min_h)

    def _set_window_height(self, height: int) -> None:
        width = self.root.winfo_width()
        if height <= 0:
            return
        self._programmatic = True
        self.root.geometry(f"{width}x{height}")
        self._last_size = (width, height)

    def _set_details(self, open_: bool) -> None:
        self._set_details_visible(open_)

    def toggle_traceback(self) -> None:
        self._traceback_visible = not self._traceback_visible
        self.output.tag_configure("traceback", elide=not self._traceback_visible)
        self.tb_btn.configure(text="收起技术明细" if self._traceback_visible else "展开技术明细")
        self.tb_label.configure(
            text="技术明细已展开" if self._traceback_visible else "技术明细已折叠")

    # --------------------------------------------------------------- 日志
    def _log(self, text: str, level: str = "信息", traceback_text: str = "") -> None:
        self.output.configure(state="normal")
        prefix = f"[{level}] "
        color = {"信息": "info", "警告": "warning", "错误": "error"}.get(level, "info")
        for index, line in enumerate(text.rstrip("\n").splitlines() or [""]):
            self.output.insert("end", prefix if index == 0 else " " * len(prefix), color)
            self.output.insert("end", line + "\n", color)
        if traceback_text:
            if not self._has_traceback:
                self._has_traceback = True
                self.output.insert("end", "[错误] 技术明细（默认折叠，可展开）\n", "warning")
            self.output.insert("end", traceback_text.rstrip("\n") + "\n", "traceback")
            if self._details_open and not self.tb_bar.winfo_manager():
                self.tb_bar.pack(fill="x", pady=(0, 2), before=self.text_wrap)
        self.output.see("end")
        self.output.configure(state="disabled")

    # --------------------------------------------------------------- 修复
    def run_repair(self) -> None:
        if self._busy:
            return
        if not self.files:
            self._show_banner("warning", "请先加入文件", "把 MTL 拖到上方区域，或点击拖拽区选择。")
            self._set_summary("warning", "等待加入文件")
            return
        mode = self.mode_var.get()
        compat = None if mode == "auto" else (mode == "compat")
        envi56 = self.out_var.get() == "envi56"
        files = list(self.files)
        product = self.product_var.get()

        self._set_busy(True)
        self._hide_banner()
        self.reveal_btn.pack_forget()
        self._last_outputs = []
        self._has_traceback = False
        self._traceback_visible = False
        self.tb_bar.pack_forget()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self._set_summary("neutral", f"正在处理 {len(files)} 个文件…")
        self.current_var.set("准备中…")
        self.progress_frame.pack(fill="x", before=self.summary_row)
        self._fit_result_card()
        self.progress.start(12)

        threading.Thread(target=self._repair_worker,
                         args=(files, compat, envi56, product), daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _repair_worker(self, files, compat, envi56, product) -> None:
        ok = failed = warned = 0
        for path in files:
            self._queue.put(("start", path.name))
            try:
                result = fix_mtl.repair_mtl(path, compat=compat,
                                            change_product_id=product, envi56=envi56)
                if result.warnings:
                    warned += 1
                if result.output:
                    self._last_outputs.append(result.output)
                ok += 1
                level = "警告" if result.warnings else "信息"
                self._queue.put(("log", fix_mtl.format_result(result), level))
                self._queue.put(("ok", result))
            except fix_mtl.MTLRepairError as exc:
                failed += 1
                self._queue.put(("log", f"{path}\n{exc}", "错误", ""))
                self._queue.put(("err", str(path), str(exc), ""))
            except Exception:
                failed += 1
                detail = traceback.format_exc()
                self._queue.put(("log", f"{path}", "错误", detail))
                self._queue.put(("err", str(path),
                                 "处理时发生意外错误，请展开明细查看。", detail))
        self._queue.put(("done", ok, failed, warned))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "start":
                    self.current_var.set(f"正在处理：{item[1]}")
                elif kind == "log":
                    self._log(item[1], item[2], item[3] if len(item) > 3 else "")
                elif kind == "ok":
                    pass
                elif kind == "err":
                    self._show_banner("danger", "有文件处理失败",
                                      "展开明细查看原因，或先运行“诊断”。")
                    self._set_details(True)
                elif kind == "done":
                    self._finish_repair(*item[1:])
                    return
                elif kind in {"task_log", "task_done"}:
                    self._handle_task_message(item)
                    if kind == "task_done":
                        return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _finish_repair(self, ok: int, failed: int, warned: int) -> None:
        self.progress.stop()
        self.progress_frame.pack_forget()
        self.current_var.set("")
        self._set_busy(False)
        if failed:
            self._set_summary("danger", f"失败 {failed} 个，成功 {ok} 个。")
            self._show_banner("danger", "修复未完成", "展开明细查看规则与下一步建议。")
            self._set_details_visible(True)
        elif warned:
            self._set_summary("warning", f"{warned} 个有需注意的项（共 {ok} 个）。")
            self._show_banner("warning", "已完成，但有需要注意的项",
                              "部分 TIF 缺失，展开明细查看清单。")
        else:
            self._set_summary("success", f"修复完成：{ok} 个文件。")
        if self._last_outputs:
            self.reveal_btn.pack(side="right", padx=(0, 12))
        self._fit_result_card()

    # --------------------------------------------------------------- 诊断
    def diagnose(self) -> None:
        if self._busy:
            return
        if not self.files:
            self._show_banner("warning", "请先加入文件", "加入 MTL 后再运行诊断。")
            return
        self._start_task("diagnose", list(self.files))

    def self_check(self) -> None:
        if self._busy:
            return
        if not self.files:
            self._show_banner("warning", "请先加入文件", "加入 MTL 后会自动检查其所在目录。")
            return
        dirs = []
        for path in self.files:
            if path.parent not in dirs:
                dirs.append(path.parent)
        self._start_task("selfcheck", dirs)

    def _start_task(self, kind: str, targets: list) -> None:
        self._set_busy(True)
        self._hide_banner()
        self.reveal_btn.pack_forget()
        self._has_traceback = False
        self._traceback_visible = False
        self.tb_bar.pack_forget()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self.current_var.set("正在诊断…" if kind == "diagnose" else "正在检查目录…")
        self._set_summary("neutral", "正在检查，请稍候…")
        self.progress_frame.pack(fill="x", before=self.summary_row)
        self._fit_result_card()
        self.progress.start(12)
        threading.Thread(target=self._task_worker, args=(kind, targets), daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _task_worker(self, kind: str, targets: list) -> None:
        failed = 0
        for index, target in enumerate(targets, 1):
            self._queue.put(("start", f"{index}/{len(targets)}  {Path(target).name}"))
            try:
                text = (fix_mtl.diagnose_mtl(target) if kind == "diagnose"
                        else fix_mtl.self_check_dir(target))
                self._queue.put(("task_log", text + "\n"))
            except fix_mtl.MTLRepairError as exc:
                failed += 1
                self._queue.put(("task_log", f"{target}\n  {exc}\n", "错误"))
            except Exception:
                failed += 1
                self._queue.put(("task_log", f"{target}\n{traceback.format_exc()}\n",
                                 "错误"))
        self._queue.put(("task_done", kind, len(targets) - failed, failed))

    def _handle_task_message(self, item) -> None:
        if item[0] == "task_log":
            self._log(item[1], item[2] if len(item) > 2 else "信息")
        elif item[0] == "task_done":
            kind, ok, failed = item[1], item[2], item[3]
            self.progress.stop()
            self.progress_frame.pack_forget()
            self.current_var.set("")
            self._set_busy(False)
            self._set_details(True)
            label = "诊断完成" if kind == "diagnose" else "目录自检完成"
            if failed:
                self._set_summary("danger", f"{label}：成功 {ok} 个，失败 {failed} 个。")
                self._show_banner("danger", "部分检查未完成", "展开明细查看失败原因。")
            else:
                self._set_summary("success", f"{label}：{ok} 个。")
            self._fit_result_card()

    # --------------------------------------------------------------- 还原
    def restore(self) -> None:
        if self._busy:
            return
        target = self.files[0] if self.files else None
        if target is None:
            chosen = filedialog.askopenfilename(
                title="选择要还原的 MTL 文件（会使用它的备份）",
                filetypes=[("Landsat MTL 元数据", "*_MTL.txt *_MTL.MTL"), ("所有文件", "*.*")],
            )
            if not chosen:
                return
            target = Path(chosen)
        backups = fix_mtl.find_backups(target)
        if not backups:
            self._show_banner("warning", "没有找到备份",
                              f"未在 {fix_mtl.backup_dir_for(target)} 找到 {target.name} 的备份。")
            return
        latest = backups[0]
        if not messagebox.askyesno(
                "确认还原",
                f"将用备份覆盖：\n{target.name}\n\n备份：{latest.name}\n\n"
                "覆盖前会先把当前文件另存到备份目录，操作可回退。"):
            return
        try:
            result = fix_mtl.restore_from_backup(target, latest)
        except fix_mtl.MTLRepairError as exc:
            self._show_banner("danger", "还原失败", str(exc))
            return
        self._log(f"【已还原】{target}\n使用的备份：{latest}", "信息")
        for note in result.notes:
            self._log(f"说明：{note}", "信息")
        self._set_summary("success", f"已还原：{target.name}")
        self._show_banner("success", "还原完成", "文件名与内容已回到备份状态。")
        self._set_details(True)
        self._last_outputs = [target]
        self.reveal_btn.pack(side="right", padx=(0, 12))
        self._refresh_rows()
        self._fit_result_card()

    # ------------------------------------------------------------ 打开目录
    def open_backup_dir(self) -> None:
        if not self.files:
            self._show_banner("warning", "请先加入文件", "加入 MTL 后才知道对应的备份目录。")
            return
        directory = fix_mtl.backup_dir_for(self.files[0])
        if not directory.is_dir():
            self._show_banner("warning", "备份目录还不存在",
                              "先执行一次修复才会创建备份目录。")
            return
        try:
            os.startfile(str(directory))  # type: ignore[attr-defined]
        except Exception as exc:
            self._show_banner("danger", "无法打开备份目录", str(exc))

    def reveal(self) -> None:
        if not self._last_outputs:
            return
        target = self._last_outputs[0]
        try:
            subprocess.Popen(["explorer", "/select,", str(target)])
        except Exception as exc:
            self._show_banner("danger", "无法打开资源管理器", str(exc))

    # --------------------------------------------------------------- 状态
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in self._interactive:
            try:
                widget.configure(state=state)
            except Exception:
                pass
        self.start_btn.configure(text="处理中…" if busy else "开始修复")

    # ------------------------------------------------------- 窗口尺寸/位置
    def _apply_initial_geometry(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        try:
            scale = self.root.winfo_fpixels("1i") / 96.0
        except Exception:
            scale = 1.0
        default = compute_window_geometry(screen_w, screen_h, scale)
        saved = load_settings().get("geometry")
        geom = parse_geometry(saved) if isinstance(saved, str) else None
        if geom and geometry_is_visible(geom, screen_w, screen_h):
            width, height, x, y = geom
        else:
            width, height, x, y = default
        self.root.minsize(int(round(660 * scale)), int(round(540 * scale)))
        self._programmatic = True
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.update_idletasks()
        self._sync_page_scrollbar()
        self._fit_options_card()
        self._fit_result_card()
        self._ready = True
        self._last_size = (self.root.winfo_width(), self.root.winfo_height())
        self._ready_job = self.root.after(200, self._mark_ready)

    def _mark_ready(self) -> None:
        self._programmatic = False
        self._ready = True

    def _bind_global_events(self) -> None:
        self.root.bind("<Return>", lambda _e: self.run_repair())
        self.root.bind("<Escape>", lambda _e: self._on_close())
        self.root.bind("<Configure>", self._on_root_configure)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.out_var.trace_add("write", lambda *_: self._on_out_change())
        self._on_out_change()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        if self._programmatic:
            self._programmatic = False
            self._last_size = (event.width, event.height)
            return
        if not self._ready:
            return
        if (event.width, event.height) != getattr(self, "_last_size", (event.width, event.height)):
            self._last_size = (event.width, event.height)
            self._user_resized = True
            self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(500, self._save_now)

    def _save_now(self) -> None:
        self._save_job = None
        try:
            if self.root.state() == "zoomed":
                return
            geom = (f"{self.root.winfo_width()}x{self.root.winfo_height()}"
                    f"+{self.root.winfo_rootx()}+{self.root.winfo_rooty()}")
        except Exception:
            return
        save_settings({"geometry": geom})

    def _on_close(self) -> None:
        for job in (self._save_job, getattr(self, "_ready_job", None)):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._save_job = None
        self._save_now()
        self.root.destroy()


def main() -> None:
    enable_high_dpi()
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    MTLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
