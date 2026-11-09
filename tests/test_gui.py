# -*- coding: utf-8 -*-
"""窗口版 v2.1 界面测试：布局、明细滚动、整页兜底滚动、settings、DPI 计算。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import gui_mtl  # noqa: E402

SAMPLE = (PROJECT / "测试样例数据" / "LC09_L1TP_123032_20260101_20260101_02_T1"
          / "LC09_L1TP_123032_20260101_20260101_02_T1_MTL.txt")


def labels_in(widget):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            found.append(child.cget("text"))
        found.extend(labels_in(child))
    return found


class GeometryPureTest(unittest.TestCase):
    """不依赖桌面环境：初始尺寸 / settings 纯函数。"""

    def test_01_DPI_初始尺寸计算(self):
        for scale in (1.0, 1.25, 1.5):
            width, height, x, y = gui_mtl.compute_window_geometry(1920, 1080, scale)
            self.assertGreater(width, 600)
            self.assertGreater(height, 500)
            self.assertLessEqual(width, int(1920 * 0.92))
            self.assertLessEqual(height, int(1080 * 0.9))
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLess(x + width, 1920 + 1)
        # 100% / 125% 不被屏幕上限截断，按基准尺寸放大
        self.assertEqual(gui_mtl.compute_window_geometry(1920, 1080, 1.0)[:2], (980, 760))
        self.assertEqual(gui_mtl.compute_window_geometry(2560, 1440, 1.25)[:2], (1225, 950))
        # 150% 在小屏上必须被屏幕高度上限截断
        capped = gui_mtl.compute_window_geometry(1920, 1080, 1.5)
        self.assertLessEqual(capped[1], int(1080 * 0.9))

    def test_02_settings_读写与容错(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertTrue(gui_mtl.save_settings({"geometry": "900x700+100+50"}, path))
            self.assertEqual(gui_mtl.load_settings(path)["geometry"], "900x700+100+50")
            # 坏 JSON / 不存在 → 空 dict，不抛异常
            path.write_text("{oops", encoding="utf-8")
            self.assertEqual(gui_mtl.load_settings(path), {})
            self.assertEqual(gui_mtl.load_settings(Path(tmp) / "none.json"), {})

    def test_03_geometry_解析与可见性(self):
        self.assertEqual(gui_mtl.parse_geometry("900x700+100+50"), (900, 700, 100, 50))
        self.assertIsNone(gui_mtl.parse_geometry("nonsense"))
        self.assertTrue(gui_mtl.geometry_is_visible((900, 700, 100, 50), 1920, 1080))
        self.assertFalse(gui_mtl.geometry_is_visible((900, 700, 5000, 50), 1920, 1080))

    def test_04_像素缩放与内容上限(self):
        self.assertEqual(gui_mtl.scale_px(180, 1.0), 180)
        self.assertEqual(gui_mtl.scale_px(180, 1.5), 270)
        self.assertEqual(gui_mtl.scale_px(0, 1.0), 1)
        # 缩放比越界时被限制在 0.75~2.0
        self.assertEqual(gui_mtl.scale_px(100, 9.9), 200)
        self.assertEqual(gui_mtl.scale_px(100, 0.1), 75)
        self.assertGreater(gui_mtl.CONTENT_MAX_W, 800)


class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        gui_mtl.enable_high_dpi()
        self._tmp = tempfile.TemporaryDirectory()
        self._settings = Path(self._tmp.name) / "settings.json"
        os.environ["LANDSAT_MTL_SETTINGS"] = str(self._settings)
        self.root = gui_mtl.TkinterDnD.Tk() if gui_mtl.HAS_DND else tk.Tk()
        self.app = gui_mtl.MTLApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self.app._on_close()
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass
        os.environ.pop("LANDSAT_MTL_SETTINGS", None)
        self._tmp.cleanup()

    def _wait_idle(self, seconds=12):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.root.update()
            if not self.app._busy:
                break
            time.sleep(0.05)
        self.root.update()

    # ------------------------------------------------------------ 基本布局
    def test_04_启动与控件(self):
        app, root = self.app, self.root
        self.assertEqual(root.title(), gui_mtl.APP_TITLE)
        self.assertTrue(app.drop_canvas.winfo_exists())
        self.assertTrue(app.list_canvas.winfo_exists())
        self.assertTrue(app.start_btn.winfo_exists())
        self.assertEqual(app.mode_var.get(), "auto")
        self.assertEqual(app.out_var.get(), "inplace")
        self.assertEqual(app.count_label.cget("text"), "0 个")
        self.assertEqual(app.summary_var.get(), "尚未开始")
        self.assertEqual(app.start_btn.cget("text"), "开始修复")
        self.assertIn(f"v{gui_mtl.APP_VERSION}", labels_in(app.root))
        # 整页兜底滚动条常驻；空列表滚动条不显示
        self.assertEqual(app.page_scrollbar.winfo_manager(), "pack")
        self.assertFalse(app.list_scrollbar.winfo_manager())
        # 结果卡高度由内容决定，不裁切
        self.assertGreaterEqual(app.result_card.winfo_height(), 112)

    def test_05_加入文件与模式预览(self):
        app, root = self.app, self.root
        app.add_files([SAMPLE])
        root.update()
        self.assertEqual(len(app.files), 1)
        self.assertEqual(app.count_label.cget("text"), "1 个")
        scene, mode, fixed = app._describe_file(SAMPLE)
        self.assertIn("LC09_L1TP_123032", scene)
        self.assertIn(mode, ("兼容模式", "最小补丁"))
        self.assertFalse(fixed)
        self.assertIn("已加入 1 个文件", app.summary_var.get())

        app.mode_var.set("minimal")
        app._refresh_rows()
        root.update()
        self.assertEqual(app._describe_file(SAMPLE)[1], "最小补丁")

        app.mode_var.set("compat")
        app._refresh_rows()
        self.assertEqual(app._describe_file(SAMPLE)[1], "兼容模式")

        app.remove_file(SAMPLE)
        root.update()
        self.assertEqual(len(app.files), 0)
        self.assertEqual(app.count_label.cget("text"), "0 个")

    # ------------------------------------------------------------ 明细滚动
    def test_06_明细展开高度与可见性(self):
        app, root = self.app, self.root
        self.assertFalse(app.details_frame.winfo_manager())
        for index in range(40):
            app._log(f"第 {index + 1} 行日志内容", "信息")
        app.toggle_details()
        root.update()
        self.assertTrue(app._details_open)
        self.assertEqual(app.details_frame.winfo_manager(), "pack")
        self.assertTrue(app.output.winfo_ismapped())
        height = app.output.winfo_height()
        scale = app.ui_scale
        self.assertGreaterEqual(height, gui_mtl.scale_px(80, scale))
        self.assertLessEqual(height, gui_mtl.scale_px(220, scale))
        self.assertIn("收起明细", app.detail_btn.cget("text"))
        # 长文本可滚动
        app.output.yview_moveto(1.0)
        root.update()
        self.assertGreater(app.output.yview()[0], 0.0)
        # 明细展开后结果卡确实装得下（无裁切）
        body = app.result_card.body
        self.assertGreaterEqual(app.result_card.winfo_height(),
                                body.winfo_reqheight() + 2 * app.result_card.padding)

        app.toggle_details()
        root.update()
        self.assertFalse(app.details_frame.winfo_manager())
        self.assertIn("展开明细", app.detail_btn.cget("text"))
        self.assertLess(app.result_card.winfo_height(), gui_mtl.scale_px(200, app.ui_scale))

    def test_07_鼠标滚轮(self):
        app, root = self.app, self.root

        class Wheel:
            num = None

            def __init__(self, delta):
                self.delta = delta

        for index in range(60):
            app._log(f"滚动测试 {index}", "信息")
        app.toggle_details()
        root.update()
        app.output.yview_moveto(0.0)
        root.update()
        before = app.output.yview()[0]
        app._on_output_wheel(Wheel(-120))
        root.update()
        self.assertGreater(app.output.yview()[0], before)

        # 整页滚轮：窗口缩到最小后上滚回到顶部
        min_w, min_h = root.minsize()
        root.geometry(f"{min_w}x{min_h}")
        root.update()
        app.page_canvas.yview_moveto(0.0)
        root.update()
        app._on_page_wheel(Wheel(-120))
        root.update()
        self.assertGreater(app.page_canvas.yview()[0], 0.0)

    # ------------------------------------------------------------ 兜底滚动
    def test_08_最小窗口下所有控件可滚动到达(self):
        app, root = self.app, self.root
        for index in range(30):
            app._log(f"内容 {index}", "信息")
        app.toggle_details()
        root.update()
        min_w, min_h = root.minsize()
        root.geometry(f"{min_w}x{min_h}")
        root.update()

        region = [float(v) for v in app.page_canvas.cget("scrollregion").split()]
        self.assertGreater(region[3], app.page_canvas.winfo_height())
        self.assertGreaterEqual(app.page_scrollbar.winfo_width(), 1,
                                "窄窗口下整页滚动条必须可见")

        # 顶部：标题可见
        app.page_canvas.yview_moveto(0.0)
        root.update()
        header_y = app.subtitle_label.winfo_rooty()
        self.assertLess(header_y, root.winfo_rooty() + root.winfo_height())

        # 底部：结果卡（含明细）可见
        app.page_canvas.yview_moveto(1.0)
        root.update()
        card_bottom = (app.result_card.winfo_rooty()
                       + app.result_card.winfo_height())
        self.assertLessEqual(card_bottom, root.winfo_rooty() + root.winfo_height() + 2)
        self.assertGreaterEqual(app.output.winfo_height(), 80)

    # ------------------------------------------------------------ 功能回归
    def test_09_修复成功状态(self):
        app, root = self.app, self.root
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp) / SAMPLE.stem.replace("_MTL", "")
            scene.mkdir()
            for tif in SAMPLE.parent.glob("*.TIF"):
                shutil.copy2(tif, scene / tif.name)
            mtl = scene / SAMPLE.name
            shutil.copy2(SAMPLE, mtl)
            app.add_files([mtl])
            root.update()
            app.run_repair()
            self._wait_idle()
            self.assertFalse(app._busy)
            self.assertIn("修复完成", app.summary_var.get())
            self.assertEqual(app.start_btn.cget("text"), "开始修复")
            self.assertTrue(app.reveal_btn.winfo_manager())
            self.assertFalse(app.banner.winfo_manager())
            self.assertIn("模式", app.output.get("1.0", "end"))
            self.assertTrue((scene / mtl.name).is_file())
            self.assertTrue((scene / gui_mtl.fix_mtl.BACKUP_DIR_NAME).is_dir())

    def test_10_修复失败横幅与自动展开(self):
        app, root = self.app, self.root
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "BAD_L2SP_000000_20260101_20260101_02_T1_MTL.txt"
            bad.write_text("GROUP = LANDSAT_METADATA_FILE\n  SPACECRAFT_ID = \"LANDSAT_9\"\n",
                           encoding="utf-8")
            app.add_files([bad])
            root.update()
            app.run_repair()
            self._wait_idle(10)
            self.assertFalse(app._busy)
            self.assertEqual(app.banner.winfo_manager(), "pack")
            self.assertIn("失败", app.summary_var.get())
            self.assertTrue(app.details_frame.winfo_manager())
            self.assertIn("ODL", app.output.get("1.0", "end"))

    def test_11_窗口尺寸记忆(self):
        app, root = self.app, self.root
        root.geometry("900x700+80+60")
        root.update()
        app._user_resized = True
        app._save_now()
        saved = gui_mtl.load_settings(self._settings)
        width, height, _x, _y = gui_mtl.parse_geometry(saved.get("geometry"))
        self.assertEqual((width, height), (900, 700))
        self.assertTrue(self._settings.is_file())

    def test_12_技术明细折叠(self):
        app, root = self.app, self.root
        app.toggle_details()
        root.update()
        app._log("出错了", "错误", "Traceback (most recent call last):\n  boom")
        root.update()
        self.assertTrue(app._has_traceback)
        self.assertEqual(app.tb_bar.winfo_manager(), "pack")
        self.assertTrue(int(app.output.tag_cget("traceback", "elide")))
        app.toggle_traceback()
        root.update()
        self.assertFalse(int(app.output.tag_cget("traceback", "elide")))
        self.assertIn("收起技术明细", app.tb_btn.cget("text"))
        # 标题行不折叠，用户始终知道有技术明细
        self.assertIn("技术明细", app.output.get("1.0", "end"))

    # ------------------------------------------- 第二轮 UI 优化（DPI/自适应）
    def test_13_操作条窄窗换行(self):
        app, root = self.app, self.root

        class Evt:
            def __init__(self, width):
                self.width = width

        app._on_actions_configure(Evt(400))
        self.assertTrue(app._actions_narrow)
        self.assertEqual(int(app.start_btn.grid_info()["row"]), 1)
        self.assertEqual(int(app.start_btn.grid_info()["columnspan"]), 2)

        app._on_actions_configure(Evt(4000))
        self.assertFalse(app._actions_narrow)
        self.assertEqual(int(app.start_btn.grid_info()["row"]), 0)

    def test_14_拖拽区内容不越界(self):
        app, root = self.app, self.root
        root.update()
        app._render_drop()
        root.update()
        bbox = app.drop_canvas.bbox("all")
        self.assertIsNotNone(bbox)
        height = int(float(app.drop_canvas.cget("height")))
        self.assertGreaterEqual(bbox[1], 0)
        self.assertLessEqual(bbox[3], height)

    def test_15_banner与摘要宽度随窗口自适应(self):
        app, root = self.app, self.root
        app._show_banner("danger", "测试标题",
                         "这是一段较长的提示文字，用于验证换行宽度是否随窗口变化。" * 2)
        root.update()
        narrow = int(app.banner_text.cget("wraplength"))
        self.assertGreater(narrow, 0)
        self.assertLessEqual(narrow, app.result_card.winfo_width())
        self.assertLessEqual(int(app.summary_label.cget("wraplength")),
                             app.result_card.winfo_width())

        width = gui_mtl.scale_px(1120, app.ui_scale)
        root.geometry(f"{width}x{root.winfo_height()}")
        root.update()
        wide = int(app.banner_text.cget("wraplength"))
        self.assertGreater(wide, narrow)
        self.assertLessEqual(wide, app.result_card.winfo_width())

    def test_16_键盘可达与焦点可见(self):
        from tkinter import ttk

        app, root = self.app, self.root
        self.assertEqual(str(app.drop_canvas.cget("takefocus")), "1")
        self.assertTrue(app.drop_canvas.bind("<space>"))
        self.assertTrue(app.drop_canvas.bind("<Return>"))
        style = ttk.Style(root)
        self.assertNotEqual(style.lookup("TRadiobutton", "focuscolor"), gui_mtl.CARD)

    def test_17_忙碌时Esc不关窗(self):
        app, root = self.app, self.root
        app._busy = True
        app._on_escape_key(None)
        root.update()
        self.assertTrue(root.winfo_exists())
        self.assertIn("正在处理", app.summary_var.get())
        app._busy = False

    def test_18_空列表卡片收缩(self):
        app, root = self.app, self.root
        root.update()
        empty_height = app.body_card.winfo_height()
        app.add_files([SAMPLE])
        root.update()
        self.assertGreater(app.body_card.winfo_height(), empty_height)
        self.assertGreaterEqual(app.list_canvas.winfo_height(), 1)


if __name__ == "__main__":
    unittest.main()
