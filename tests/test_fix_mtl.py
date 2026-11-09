# -*- coding: utf-8 -*-
"""fix_mtl.py 的自动化测试（仅用标准库 unittest）。

运行：
    python -m unittest discover -s tests -v
"""

import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fix_mtl  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = TOOL_DIR / "测试样例数据" / "LC09_L1TP_123032_20260101_20260101_02_T1"
SAMPLE_NAME = "LC09_L1TP_123032_20260101_20260101_02_T1_MTL.txt"


def make_test_geotiff(path: Path, width=4, height=4) -> None:
    """构造一个最小但合法的 GeoTIFF（16 位、单波段、UTM 50N），用于 hdr 测试。"""
    entries = []  # (tag, type, count, value)
    data_offset_placeholder = 0
    entries = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 1, 16),
        (259, 3, 1, 1),
        (262, 3, 1, 1),
        (273, 4, 1, data_offset_placeholder),
        (277, 3, 1, 1),
        (278, 4, 1, height),
        (279, 4, 1, width * height * 2),
        (284, 3, 1, 1),
        (339, 3, 1, 1),
        (33550, 12, 3, None),
        (33922, 12, 6, None),
        (34735, 3, 12, None),
    ]
    ifd_size = 2 + 12 * len(entries) + 4
    ifd_start = 8
    extra_start = ifd_start + ifd_size
    data_offset = extra_start
    pixel_bytes = width * height * 2
    scale_offset = data_offset + pixel_bytes
    tie_offset = scale_offset + 24
    geokey_offset = tie_offset + 48
    total = geokey_offset + 24

    buf = bytearray(total)
    buf[0:2] = b"II"
    struct.pack_into("<H", buf, 2, 42)
    struct.pack_into("<I", buf, 4, ifd_start)
    struct.pack_into("<H", buf, ifd_start, len(entries))

    for i, (tag, typ, cnt, value) in enumerate(entries):
        base = ifd_start + 2 + i * 12
        struct.pack_into("<HHI", buf, base, tag, typ, cnt)
        if tag == 273:
            struct.pack_into("<I", buf, base + 8, data_offset)
        elif tag == 33550:
            struct.pack_into("<I", buf, base + 8, scale_offset)
        elif tag == 33922:
            struct.pack_into("<I", buf, base + 8, tie_offset)
        elif tag == 34735:
            struct.pack_into("<I", buf, base + 8, geokey_offset)
        else:
            if typ == 3:
                struct.pack_into("<H", buf, base + 8, value)
            else:
                struct.pack_into("<I", buf, base + 8, value)
    # 下一个 IFD 偏移 = 0
    struct.pack_into("<I", buf, ifd_start + 2 + 12 * len(entries), 0)

    # ModelPixelScale = (30, 30, 0)
    struct.pack_into("<3d", buf, scale_offset, 30.0, 30.0, 0.0)
    # ModelTiepoint = (0, 0, 0, 500000, 3500000, 0)
    struct.pack_into("<6d", buf, tie_offset, 0.0, 0.0, 0.0, 500000.0, 3500000.0, 0.0)
    # GeoKeyDirectory: 版本头 + GTModelType(1024)=1 + ProjectedCSType(3072)=32650
    struct.pack_into("<12H", buf, geokey_offset,
                     1, 1, 0, 2,
                     1024, 0, 1, 1,
                     3072, 0, 1, 32650)

    path.write_bytes(bytes(buf))


def make_mtl_text(product_id: str, spacecraft: str, product_type: str,
                  level: str, collection: int, l2_fields: bool = False) -> str:
    """构造一份最小但合法的 MTL 文本，用于模式选择 / L2 字段保留测试。"""
    lines = [
        "GROUP = LANDSAT_METADATA_FILE",
        "  GROUP = PRODUCT_CONTENTS",
        f'    ORIGIN = "Image courtesy of the U.S. Geological Survey"',
        f'    PRODUCT_TYPE = "{product_type}"',
        f'    LANDSAT_PRODUCT_ID = "{product_id}"',
        f'    PROCESSING_LEVEL = "{level}"',
        f"    COLLECTION_NUMBER = {collection}",
        f'    FILE_NAME_BAND_1 = "{product_id}_B1.TIF"',
        f'    FILE_NAME_BAND_10 = "{product_id}_B10.TIF"',
        "  END_GROUP = PRODUCT_CONTENTS",
        "  GROUP = IMAGE_ATTRIBUTES",
        f'    SPACECRAFT_ID = "{spacecraft}"',
        '    SENSOR_ID = "OLI_TIRS"',
        "    SUN_AZIMUTH = 148.1234",
        "    SUN_ELEVATION = 27.5678",
        "  END_GROUP = IMAGE_ATTRIBUTES",
        "  GROUP = PROJECTION_ATTRIBUTES",
        '    MAP_PROJECTION = "UTM"',
        "    UTM_ZONE = 50",
        "  END_GROUP = PROJECTION_ATTRIBUTES",
    ]
    if l2_fields:
        lines += [
            "  GROUP = LEVEL1_SURFACE_REFLECTANCE_PARAMETERS",
            "    REFLECTANCE_MULT_BAND_1 = 1.23450000E-04",
            "    REFLECTANCE_ADD_BAND_1 = -0.1234",
            "  END_GROUP = LEVEL1_SURFACE_REFLECTANCE_PARAMETERS",
        ]
    lines += ["END_GROUP = LANDSAT_METADATA_FILE", "END"]
    return "\n".join(lines) + "\n"


def write_scene(directory: Path, product_id: str, spacecraft: str, product_type: str,
                level: str, collection: int, l2_fields: bool = False) -> Path:
    """在目录里写一份 MTL 和两个占位波段文件，返回 MTL 路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    mtl = directory / f"{product_id}_MTL.txt"
    mtl.write_text(make_mtl_text(product_id, spacecraft, product_type,
                                level, collection, l2_fields), encoding="utf-8")
    for band in ("B1", "B10"):
        (directory / f"{product_id}_{band}.TIF").write_bytes(b"placeholder")
    return mtl


class MTLTestCase(unittest.TestCase):
    def setUp(self):
        # 目录名故意带空格和中文，顺带验证路径处理
        self.tmp = Path(tempfile.mkdtemp(prefix="mtl_test_"))
        self.data = self.tmp / "影像 数据"
        shutil.copytree(SAMPLE_DIR, self.data)
        self.mtl = self.data / SAMPLE_NAME
        self.orig_bytes = self.mtl.read_bytes()
        self.orig_lines = self.mtl.read_text(encoding="utf-8").splitlines()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_01_默认原地覆盖并备份(self):
        result = fix_mtl.repair_mtl(self.mtl)

        self.assertTrue(result.success)
        self.assertEqual(result.output, self.mtl, "默认应原地覆盖同名 *_MTL.txt")
        self.assertRegex(result.backup.name,
                         r"^LC09_L1TP_123032_20260101_20260101_02_T1_MTL_原始备份_\d{8}_\d{6}(?:_\d+)?\.txt$")
        self.assertEqual(result.backup.read_bytes(), self.orig_bytes)

        text = self.mtl.read_text(encoding="utf-8")
        self.assertIn('SPACECRAFT_ID = "LANDSAT_8"', text)
        self.assertNotIn("LANDSAT_9", text)
        self.assertEqual(fix_mtl.validate_odl(text.splitlines()), [])
        self.assertIn("LC09_L1TP_123032", text)          # 产品号默认不改
        self.assertFalse((self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_MTL_ENVI56.txt").exists())

    def test_02_只改动必要的一行(self):
        fix_mtl.repair_mtl(self.mtl, compat=False)
        new_lines = self.mtl.read_text(encoding="utf-8").splitlines()
        diffs = [(old, new) for old, new in zip(self.orig_lines, new_lines) if old != new]
        self.assertEqual(len(diffs), 1)
        self.assertIn("SPACECRAFT_ID", diffs[0][0])
        self.assertIn("LANDSAT_9", diffs[0][0])
        self.assertIn("LANDSAT_8", diffs[0][1])
        self.assertEqual(len(self.orig_lines), len(new_lines))

    def test_03_缺TIF时给出警告且不猜名(self):
        (self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_B10.TIF").unlink()
        (self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_B11.TIF").unlink()
        result = fix_mtl.repair_mtl(self.mtl)

        self.assertTrue(result.success)
        self.assertEqual(len(result.missing_band_files), 2)
        joined = " ".join(result.warnings)
        self.assertIn("_B10.TIF", joined)
        self.assertIn("_B11.TIF", joined)
        self.assertFalse((self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_B10.TIF").exists())
        log = (self.data / fix_mtl.BACKUP_DIR_NAME / fix_mtl.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("_B10.TIF", log)

    def test_04_重复运行幂等且提示已是修复版(self):
        first = fix_mtl.repair_mtl(self.mtl)
        first_bytes = self.mtl.read_bytes()
        second = fix_mtl.repair_mtl(self.mtl)

        self.assertTrue(second.already_fixed)
        self.assertEqual(self.mtl.read_bytes(), first_bytes, "第二次运行不得叠改内容")
        backups = list((self.data / fix_mtl.BACKUP_DIR_NAME).glob("*_MTL_原始备份_*.txt"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(first.backup.read_bytes(), self.orig_bytes)
        # 第二份备份记录的是“已修复”的现状（符合“仍先备份”的要求）
        self.assertIn(b'SPACECRAFT_ID = "LANDSAT_8"', second.backup.read_bytes())

    def test_05_可选ENVI56另存模式(self):
        result = fix_mtl.repair_mtl(self.mtl, envi56=True)
        self.assertEqual(self.mtl.read_bytes(), self.orig_bytes, "ENVI56 模式不得改动原件")
        out = self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_MTL_ENVI56.txt"
        self.assertTrue(out.is_file())
        self.assertEqual(result.output, out)
        self.assertIn('SPACECRAFT_ID = "LANDSAT_8"', out.read_text(encoding="utf-8"))
        joined = " ".join(result.warnings)
        self.assertIn("ENVI", joined)
        self.assertIn("Unrecognized File Type", joined)
        self.assertIn("仅作留档", joined)

    def test_06_从备份还原到原文件名(self):
        fix_mtl.repair_mtl(self.mtl)
        self.assertIn(b"LANDSAT_8", self.mtl.read_bytes())

        result = fix_mtl.restore_from_backup(self.mtl)
        self.assertTrue(result.success)
        self.assertEqual(result.output, self.mtl)
        self.assertEqual(self.mtl.read_bytes(), self.orig_bytes)
        log = (self.data / fix_mtl.BACKUP_DIR_NAME / fix_mtl.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("原文件已被覆盖：是", log)
        safety = [p for p in (self.data / fix_mtl.BACKUP_DIR_NAME).iterdir()
                  if "_MTL_还原前_" in p.name]
        self.assertTrue(safety)

    def test_07_多次运行后还原仍取真正原件(self):
        fix_mtl.repair_mtl(self.mtl)
        fix_mtl.repair_mtl(self.mtl)   # 第二次备份已是修复版
        result = fix_mtl.restore_from_backup(self.mtl)
        self.assertEqual(self.mtl.read_bytes(), self.orig_bytes, "应避开已打补丁的备份")
        backups = fix_mtl.find_backups(self.mtl)
        self.assertTrue(any(fix_mtl._backup_is_patched(b) for b in backups))

    def test_08_BOM与CRLF输入(self):
        text = self.mtl.read_text(encoding="utf-8").replace("\n", "\r\n")
        self.mtl.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

        result = fix_mtl.repair_mtl(self.mtl)
        raw = self.mtl.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "行尾应统一为 CRLF")
        self.assertEqual(fix_mtl.validate_odl(raw.decode("utf-8").splitlines()), [])
        self.assertTrue(result.backup.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_09_LF行尾保持与兼容模式(self):
        result = fix_mtl.repair_mtl(self.mtl, compat=True)
        raw = self.mtl.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(fix_mtl.validate_odl(lines), [])
        self.assertEqual(lines[0], "GROUP = L1_METADATA_FILE")
        self.assertEqual(lines[-2], "END_GROUP = L1_METADATA_FILE")
        self.assertIn('SPACECRAFT_ID = "LANDSAT_8"', [l.strip() for l in lines])
        self.assertIn("ORIGIN", result.removed)
        log = (self.data / fix_mtl.BACKUP_DIR_NAME / fix_mtl.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("兼容模式删除", log)
        self.assertIn("ORIGIN", log)

    def test_10_可选改产品号(self):
        result = fix_mtl.repair_mtl(self.mtl, change_product_id=True)
        text = self.mtl.read_text(encoding="utf-8")
        self.assertIn('LANDSAT_PRODUCT_ID = "LC08_L1TP_123032_20260101_20260101_02_T1"', text)
        self.assertEqual(len(result.changes), 2)

    def test_11_非法ODL不写出任何文件(self):
        bad_dir = self.tmp / "坏文件"
        bad_dir.mkdir()
        bad = bad_dir / "BAD_MTL.txt"
        bad.write_text('GROUP = LANDSAT_METADATA_FILE\n  SPACECRAFT_ID = "LANDSAT_9"\n',
                       encoding="utf-8")

        with self.assertRaises(fix_mtl.MTLRepairError):
            fix_mtl.repair_mtl(bad)
        self.assertFalse((bad_dir / fix_mtl.BACKUP_DIR_NAME).exists())
        self.assertEqual([p.name for p in bad_dir.iterdir()], ["BAD_MTL.txt"])

    def test_12_大写MTL扩展名拒绝原地写入(self):
        # ENVI 只认 *_MTL.txt；*.MTL 原地输出必须拒写，且不产生备份/新文件
        upper = self.data / "CASE_MTL.MTL"
        upper.write_bytes(self.orig_bytes)
        with self.assertRaises(fix_mtl.MTLRepairError) as ctx:
            fix_mtl.repair_mtl(upper)
        self.assertIn("_MTL.txt", str(ctx.exception))
        self.assertEqual(upper.read_bytes(), self.orig_bytes)
        self.assertFalse((self.data / fix_mtl.BACKUP_DIR_NAME).exists())

    def test_13_不修改TIF等其他文件(self):
        tif = self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_B1.TIF"
        before = tif.read_bytes()
        fix_mtl.repair_mtl(self.mtl)
        self.assertEqual(tif.read_bytes(), before)
        self.assertEqual(len(list(self.data.glob("*.TIF"))), 12)

    def test_14_日志内容(self):
        fix_mtl.repair_mtl(self.mtl)
        log = (self.data / fix_mtl.BACKUP_DIR_NAME / fix_mtl.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("修复 MTL", log)
        self.assertIn("SPACECRAFT_ID", log)
        self.assertIn("LANDSAT_9", log)
        self.assertIn("LANDSAT_8", log)
        self.assertIn("原文件已被覆盖：是", log)
        self.assertIn("结果：成功", log)

    def test_15_同名备份不覆盖(self):
        r1 = fix_mtl.repair_mtl(self.mtl)
        r2 = fix_mtl.repair_mtl(self.mtl)
        self.assertNotEqual(r1.backup, r2.backup)
        self.assertTrue(r1.backup.exists())
        self.assertTrue(r2.backup.exists())
        self.assertEqual(r1.backup.read_bytes(), self.orig_bytes)

    def test_16_诊断模式(self):
        report = fix_mtl.diagnose_mtl(self.mtl)
        self.assertIn("SPACECRAFT_ID：LANDSAT_9", report)
        self.assertIn("LC09_L1TP_123032", report)
        self.assertIn("ODL 结构：通过", report)
        self.assertEqual(report.count("[存在]"), 11)
        self.assertIn("分组清单", report)
        self.assertIn("GROUP = PRODUCT_CONTENTS", report)
        self.assertIn("目录中实际 TIF（12 个）", report)
        # 诊断不得修改文件
        self.assertEqual(self.mtl.read_bytes(), self.orig_bytes)

    def test_17_纯Python生成ENVI头文件(self):
        tif = self.data / "DEMO_B1.TIF"
        make_test_geotiff(tif)
        reports = fix_mtl.make_envi_headers(tif)
        hdr = tif.with_suffix(".hdr")
        self.assertTrue(hdr.is_file())
        text = hdr.read_text(encoding="ascii")
        self.assertIn("samples = 4", text)
        self.assertIn("lines = 4", text)
        self.assertIn("data type = 12", text)
        self.assertIn("file type = TIFF", text)
        self.assertIn("map info = {UTM, 1.0000, 1.0000, 500015.000000, 3499985.000000, "
                      "30.000000, 30.000000, 50, North, WGS-84, units=Meters}", text)
        self.assertIn("wavelength = {0.46}", text)
        # 已存在时不覆盖
        before = hdr.read_text(encoding="ascii")
        fix_mtl.make_envi_headers(tif)
        self.assertEqual(hdr.read_text(encoding="ascii"), before)
        # 占位 TIF 不是有效 TIFF，应给出明确错误而不是写出坏头文件
        placeholder = self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_B1.TIF"
        with self.assertRaises(fix_mtl.MTLRepairError):
            fix_mtl.make_envi_headers(placeholder)
        self.assertFalse(placeholder.with_suffix(".hdr").exists())


    def test_18_四种组合的模式选择(self):
        cases = [
            ("LC08_L1TP_129043_20150104_20200910_01_T1", "LANDSAT_8", "L1TP", "L1TP", 1, False),
            ("LC08_L2SP_129043_20150104_20200910_02_T1", "LANDSAT_8", "L2SP", "L2SP", 2, True),
            ("LC09_L1TP_129043_20200910_20200910_02_T1", "LANDSAT_9", "L1TP", "L1TP", 2, True),
            ("LC09_L2SP_129043_20200910_20200910_02_T1", "LANDSAT_9", "L2SP", "L2SP", 2, True),
        ]
        for pid, spacecraft, product_type, level, collection, expect in cases:
            labels = fix_mtl.collect_labels(
                make_mtl_text(pid, spacecraft, product_type, level, collection, expect).splitlines()
            )
            got, reason = fix_mtl.decide_compat(labels)
            self.assertEqual(got, expect, f"{pid} 的模式选择不正确，原因：{reason}")
            self.assertTrue(reason)

        # 强制模式不受自动判定影响
        scene = self.tmp / "强制最小"
        mtl = write_scene(scene, cases[1][0], "LANDSAT_8", "L2SP", "L2SP", 2, True)
        forced = fix_mtl.repair_mtl(mtl, compat=False)
        self.assertIn("强制最小补丁", forced.mode_reason)

        # 实际修复：L8 / L9 的 L2 都走兼容模式，并写入日志原因
        for idx, (pid, spacecraft, _pt, level, collection, expect) in enumerate(cases):
            if not expect:
                continue
            scene = self.tmp / f"场景{idx}"
            mtl = write_scene(scene, pid, spacecraft, "L2SP", level, collection, True)
            result = fix_mtl.repair_mtl(mtl)
            self.assertIn("兼容模式", result.mode_reason)
            self.assertEqual(result.output, mtl)
            log = (scene / fix_mtl.BACKUP_DIR_NAME / fix_mtl.LOG_FILE_NAME).read_text(encoding="utf-8")
            self.assertIn("模式", log)
            self.assertIn("兼容", log)

    def test_19_兼容模式保留L2必需字段且数值不变(self):
        pid = "LC08_L2SP_129043_20150104_20200910_02_T1"
        scene = self.tmp / "L2 场景"
        mtl = write_scene(scene, pid, "LANDSAT_8", "L2SP", "L2SP", 2, l2_fields=True)
        result = fix_mtl.repair_mtl(mtl)   # 自动 → 兼容模式

        text = mtl.read_text(encoding="utf-8")
        lines = [l.strip() for l in text.splitlines()]
        self.assertIn("GROUP = L1_METADATA_FILE", lines)
        # 关键字段存在且数值未变
        for expected in (
            "SPACECRAFT_ID = \"LANDSAT_8\"",
            "SENSOR_ID = \"OLI_TIRS\"",
            "PRODUCT_TYPE = \"L2SP\"",
            "PROCESSING_LEVEL = \"L2SP\"",
            "COLLECTION_NUMBER = 2",
            "SUN_AZIMUTH = 148.1234",
            "SUN_ELEVATION = 27.5678",
            "REFLECTANCE_MULT_BAND_1 = 1.23450000E-04",
            "REFLECTANCE_ADD_BAND_1 = -0.1234",
            "MAP_PROJECTION = \"UTM\"",
            "UTM_ZONE = 50",
        ):
            self.assertIn(expected, lines)
        # SR 定标系数不得被删除/改写
        self.assertEqual(sum("REFLECTANCE_MULT_BAND_" in l for l in lines), 1)
        self.assertEqual(sum("REFLECTANCE_ADD_BAND_" in l for l in lines), 1)
        self.assertEqual(fix_mtl.validate_odl(text.splitlines()), [])

    def test_20_自检同时存在两类文件(self):
        clean = fix_mtl.self_check_dir(self.data)
        self.assertIn("原名 MTL（ENVI 可识别）：1 个", clean)
        self.assertIn("命名状态正常", clean)

        out = self.data / "LC09_L1TP_123032_20260101_20260101_02_T1_MTL_ENVI56.txt"
        out.write_text(self.mtl.read_text(encoding="utf-8"), encoding="utf-8")
        mixed = fix_mtl.self_check_dir(self.data)
        self.assertIn("同时存在", mixed)
        self.assertIn("请选择原名", mixed)

        only_env = self.tmp / "只有ENVI56"
        only_env.mkdir()
        (only_env / "X_MTL_ENVI56.txt").write_text(self.mtl.read_text(encoding="utf-8"),
                                                  encoding="utf-8")
        report = fix_mtl.self_check_dir(only_env)
        self.assertIn("只有 X_MTL_ENVI56.txt", report)
        self.assertIn("ENVI 打开必然失败", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
