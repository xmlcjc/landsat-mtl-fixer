# -*- coding: utf-8 -*-
"""
Landsat 8/9 MTL 元数据修复工具（ENVI 5.6 兼容）
================================================

用途：
    把 Landsat 8/9（OLI/TIRS，Collection 2 Level-1 / Level-2）的 *_MTL.txt
    打一个最小补丁，使 ENVI 5.6 能识别并以 "GeoTIFF with Metadata" 打开。

默认行为：
    1. 原样备份 MTL 到同目录下的 "_MTL_原始备份" 子目录（文件名带时间戳，绝不覆盖）；
    2. 修复后的内容原地覆盖同名 *_MTL.txt（ENVI 只认这个命名）；
    3. 自动选择补丁模式：Collection 2 / Level-2 用兼容模式（实测可在 ENVI 5.6 打开），
       其余用最小补丁（只把 SPACECRAFT_ID 的 LANDSAT_9 改为 LANDSAT_8）；
    4. --envi56 可选另存为 *_MTL_ENVI56.txt（ENVI 无法识别该命名，仅作留档）。

只使用 Python 3 标准库。

命令行示例：
    python fix_mtl.py "D:\\data\\LC08_L2SP_..._MTL.txt"
    python fix_mtl.py --compat "D:\\data\\*_MTL.txt"
    python fix_mtl.py --envi56 "D:\\data\\..._MTL.txt"
    python fix_mtl.py --restore "D:\\data\\..._MTL.txt"
    python fix_mtl.py --diagnose "D:\\data\\..._MTL.txt"
    python fix_mtl.py --self-check "D:\\data"
    python fix_mtl.py --make-hdr "D:\\data"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Landsat 元数据修复工具"
BACKUP_DIR_NAME = "_MTL_原始备份"
LOG_FILE_NAME = "修复日志.txt"
OUTPUT_SUFFIX = "_MTL_ENVI56.txt"
ENVI_TARGET_NAME = "LANDSAT_8"
SOURCE_NAME = "LANDSAT_9"

# 匹配 "  KEY = value" 形式（ODL 标签）
LABEL_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
GROUP_RE = re.compile(r"^GROUP\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I)
END_GROUP_RE = re.compile(r"^END_GROUP\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I)

# 兼容模式白名单：ENVI 5.6 解析 Landsat 元数据需要的字段
COMPAT_EXACT_KEYS = {
    "SPACECRAFT_ID", "SENSOR_ID", "LANDSAT_PRODUCT_ID", "PRODUCT_ID",
    "PRODUCT_TYPE", "PROCESSING_LEVEL", "PROCESSING_SOFTWARE_VERSION",
    "COLLECTION_NUMBER", "COLLECTION_CATEGORY", "OUTPUT_STRETCH",
    "DATE_ACQUIRED", "SCENE_CENTER_TIME", "WRS_PATH", "WRS_ROW",
    "CLOUD_COVER", "CLOUD_COVER_LAND", "IMAGE_QUALITY",
    "SUN_AZIMUTH", "SUN_ELEVATION", "TIRS_SSM_MODEL", "TIRS_SSM_POSITION_STATUS",
    "MAP_PROJECTION", "DATUM", "ELLIPSOID", "UTM_ZONE", "ZONE_NUMBER",
    "ORIENTATION", "GRID_CELL_SIZE_REFLECTIVE", "GRID_CELL_SIZE_THERMAL",
    "GRID_CELL_SIZE_PANCHROMATIC", "REFLECTIVE_SAMPLES_PER_PIXEL",
    "REFLECTIVE_LINES", "THERMAL_SAMPLES_PER_PIXEL", "THERMAL_LINES",
    "PANCHROMATIC_SAMPLES_PER_PIXEL", "PANCHROMATIC_LINES",
    "REFLECTIVE_QUANTIZE_CAL_MIN_BAND_1", "REFLECTIVE_QUANTIZE_CAL_MAX_BAND_1",
}
COMPAT_PREFIXES = (
    "FILE_NAME_BAND_", "CORNER_",
    "RADIANCE_MULT_BAND_", "RADIANCE_ADD_BAND_",
    "REFLECTANCE_MULT_BAND_", "REFLECTANCE_ADD_BAND_",
    "K1_CONSTANT_BAND_", "K2_CONSTANT_BAND_",
    "QUANTIZE_CAL_MIN_BAND_", "QUANTIZE_CAL_MAX_BAND_",
    "SATURATION_BAND_", "THERMAL_CONSTANT_",
    # L2 地表反射率/温度产品必需
    "REFLECTANCE_", "TEMPERATURE_", "EMISSIVITY_", "SUN_",
)


class MTLRepairError(Exception):
    """修复过程中可预期、可直接展示给用户的错误。"""


@dataclass
class RepairResult:
    source: Path
    backup: Path | None = None
    output: Path | None = None
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    missing_band_files: list[str] = field(default_factory=list)
    already_fixed: bool = False
    success: bool = False
    message: str = ""
    log_path: Path | None = None
    mode_reason: str = ""


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def source_base_name(filename: str) -> str:
    """从 MTL 文件名提取不带 _MTL 后缀的基名。"""
    m = re.match(r"^(.*?)_MTL(?:_ENVI56)?\.(?:txt|mtl)$", filename, re.I)
    if m:
        return m.group(1)
    return Path(filename).stem


def output_name_for(filename: str, envi56: bool = False) -> str:
    """默认保持标准命名 *_MTL.txt（原地替换）；envi56=True 时另存为 *_MTL_ENVI56.txt。"""
    base = source_base_name(filename)
    if envi56:
        return base + OUTPUT_SUFFIX
    ext = ".MTL" if Path(filename).suffix.lower() == ".mtl" else ".txt"
    return base + "_MTL" + ext


def backup_dir_for(source: Path) -> Path:
    return source.parent / BACKUP_DIR_NAME


def read_source(source: Path) -> tuple[str, str]:
    """按原始字节读取文本，返回 (文本, 编码提示)。"""
    raw = source.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "原始文件含 UTF-8 BOM，输出已去除"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "原始文件为 UTF-16，输出统一为 UTF-8"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk"), "原始文件非 UTF-8，按 GBK 读取，输出统一为 UTF-8"
        except UnicodeDecodeError:
            return raw.decode("latin-1"), "原始文件编码未知，按 Latin-1 读取，输出统一为 UTF-8"


def detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# 备份 / 日志 / 还原
# ----------------------------------------------------------------------------

def create_backup(source: Path) -> Path:
    """原样复制到 _MTL_原始备份 目录，文件名唯一且不含 _MTL.txt 形态。"""
    backup_dir = backup_dir_for(source)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MTLRepairError(f"无法创建备份目录 {backup_dir}：{exc}") from exc

    base = source_base_name(source.name)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = backup_dir / f"{base}_MTL_原始备份_{stamp}.txt"
    index = 2
    while candidate.exists():
        candidate = backup_dir / f"{base}_MTL_原始备份_{stamp}_{index}.txt"
        index += 1

    try:
        shutil.copy2(source, candidate)
    except OSError as exc:
        raise MTLRepairError(f"备份失败：{exc}") from exc

    if _file_sha256(source) != _file_sha256(candidate):
        raise MTLRepairError(f"备份校验失败（内容不一致）：{candidate}")
    return candidate


def append_log(backup_dir: Path, text: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    log_path = backup_dir / LOG_FILE_NAME
    with log_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")
    return log_path


def find_backups(target: Path) -> list[Path]:
    """按基名查找某个 MTL 对应的备份，最新在前。"""
    directory = backup_dir_for(target)
    if not directory.is_dir():
        return []
    base = source_base_name(target.name)
    pattern = re.compile(
        r"^" + re.escape(base) + r"_MTL_原始备份_(\d{8}_\d{6})(?:_(\d+))?\.txt$"
    )
    found: list[Path] = []
    for item in directory.iterdir():
        if item.is_file() and pattern.match(item.name):
            found.append(item)
    # 文件名内的时间戳可以直接按字符串排序（后缀序号越大越新）
    found.sort(key=lambda p: p.name, reverse=True)
    return found


def _backup_is_patched(path: Path) -> bool:
    """备份内容是否已是 LANDSAT_8（说明这份备份是“修复后”的文件，而非真正原件）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _spacecraft_state(text) == ENVI_TARGET_NAME


def restore_from_backup(target: Path, backup_path: Path | None = None,
                        report_log: bool = True) -> RepairResult:
    """把备份以原文件名写回 target（原子替换）；写回前把当前文件再存一份。"""
    target = Path(target)
    result = RepairResult(source=target)
    backups = find_backups(target)
    if backup_path is None:
        if not backups:
            raise MTLRepairError(
                f"未找到 {target.name} 的备份。备份目录：{backup_dir_for(target)}"
            )
        # 原地替换模式下，后续备份可能是“已修复”的内容；
        # 优先选最新一份“未打过补丁”的备份，才能还原到真正的原件。
        originals = [b for b in backups if not _backup_is_patched(b)]
        if originals:
            backup_path = originals[0]
            if backup_path != backups[0]:
                result.notes.append("最新备份已是修复版，已自动选用最近一份未打补丁的备份")
        else:
            backup_path = backups[0]
            result.notes.append("未找到未打过补丁的备份，将使用最新备份")
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise MTLRepairError(f"备份文件不存在：{backup_path}")

    backup_dir = backup_dir_for(target)
    backup_dir.mkdir(parents=True, exist_ok=True)

    safety = None
    if target.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = source_base_name(target.name)
        safety = backup_dir / f"{base}_MTL_还原前_{stamp}.txt"
        n = 2
        while safety.exists():
            safety = backup_dir / f"{base}_MTL_还原前_{stamp}_{n}.txt"
            n += 1
        shutil.copy2(target, safety)
        result.notes.append(f"还原前文件已另存：{safety}")

    data = backup_path.read_bytes()
    tmp = target.parent / f".{target.name}.restore-tmp-{os.getpid()}"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    result.backup = backup_path
    result.output = target
    result.success = True
    result.message = "已从备份还原"

    if report_log:
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "",
            "=" * 68,
            f"[{stamp}] 从备份还原",
            f"  目标文件：{target}",
            f"  使用备份：{backup_path}",
        ]
        if safety:
            lines.append(f"  还原前另存：{safety}")
        lines.append("  原文件已被覆盖：是（已按原文件名写回，可用于 ENVI 标准命名识别）")
        result.log_path = append_log(backup_dir, "\n".join(lines))
    return result


# ----------------------------------------------------------------------------
# ODL 结构校验
# ----------------------------------------------------------------------------

def validate_odl(lines: list[str]) -> list[str]:
    """检查 GROUP/END_GROUP 配对、引号闭合、末尾 END。"""
    problems: list[str] = []
    stack: list[str] = []
    for no, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if s.count('"') % 2 != 0:
            problems.append(f"第 {no} 行引号数量不成对：{s[:70]}")
        m = GROUP_RE.match(s)
        if m:
            stack.append(m.group(1).upper())
            continue
        m = END_GROUP_RE.match(s)
        if m:
            name = m.group(1).upper()
            if not stack:
                problems.append(f"第 {no} 行出现多余的 END_GROUP = {name}")
            elif stack[-1] != name:
                problems.append(
                    f"第 {no} 行 END_GROUP = {name} 与 GROUP = {stack[-1]} 不匹配"
                )
                stack.pop()
            else:
                stack.pop()
            continue
        if s.upper() == "END":
            continue
        if not LABEL_RE.match(line):
            problems.append(f"第 {no} 行无法识别为 ODL 标签：{s[:70]}")
    if stack:
        problems.append("GROUP 未闭合：" + "、".join(stack))
    if not any(l.strip().upper() == "END" for l in lines):
        problems.append("文件末尾缺少 END")
    return problems


# ----------------------------------------------------------------------------
# 补丁
# ----------------------------------------------------------------------------

def _label_of(line: str) -> re.Match | None:
    return LABEL_RE.match(line)


def _spacecraft_state(text: str) -> str:
    """返回 SPACECRAFT_ID 的值（去引号大写），找不到返回空串。"""
    m = re.search(r"^[ \t]*SPACECRAFT_ID[ \t]*=[ \t]*(.+)$", text, re.M | re.I)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'").upper()


def _line_ending(line: str) -> str:
    m = re.search(r"(\r\n|\n|\r)$", line)
    return m.group(1) if m else ""


def patch_spacecraft(lines: list[str], changes: list[tuple[str, str, str]],
                     notes: list[str]) -> None:
    found = False
    for i, line in enumerate(lines):
        m = _label_of(line)
        if not m or m.group(2).upper() != "SPACECRAFT_ID":
            continue
        found = True
        raw_value = m.group(4)
        if SOURCE_NAME not in raw_value.upper():
            if ENVI_TARGET_NAME in raw_value.upper():
                notes.append("SPACECRAFT_ID 已是 LANDSAT_8，无需改动")
            else:
                notes.append(f"SPACECRAFT_ID = {raw_value.strip()}，非 LANDSAT_9，未改动")
            continue
        new_raw = re.sub(SOURCE_NAME, ENVI_TARGET_NAME, raw_value, flags=re.I)
        lines[i] = m.group(1) + m.group(2) + m.group(3) + new_raw
        changes.append(("SPACECRAFT_ID", raw_value.strip(), new_raw.strip()))
    if not found:
        notes.append("未找到 SPACECRAFT_ID 标签，可能不是 Landsat MTL 文件")


def patch_product_ids(lines: list[str], changes: list[tuple[str, str, str]]) -> None:
    for i, line in enumerate(lines):
        m = _label_of(line)
        if not m or m.group(2).upper() not in {"LANDSAT_PRODUCT_ID", "PRODUCT_ID"}:
            continue
        raw_value = m.group(4)
        if not re.search(r"LC09_", raw_value, re.I):
            continue
        new_raw = re.sub(r"LC09_", "LC08_", raw_value, count=1, flags=re.I)
        lines[i] = m.group(1) + m.group(2) + m.group(3) + new_raw
        changes.append((m.group(2), raw_value.strip(), new_raw.strip()))


def collect_labels(lines: list[str]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in lines:
        m = _label_of(line)
        if m:
            items.append((m.group(2), m.group(4).strip()))
    return items


def _compat_keep(key: str) -> bool:
    k = key.upper()
    if k in COMPAT_EXACT_KEYS:
        return True
    return any(k.startswith(p) for p in COMPAT_PREFIXES)


def build_compat_lines(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """扁平化成 Collection 1 风格结构。返回 (新行, 删除字段, 保留字段)。"""
    kept: list[tuple[str, str]] = []
    kept_keys: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for key, value in collect_labels(lines):
        k_up = key.upper()
        if k_up in {"GROUP", "END_GROUP"}:
            continue
        if not _compat_keep(key):
            dropped.append(key)
            continue
        if k_up in seen:
            dropped.append(key + "（重复）")
            continue
        seen.add(k_up)
        kept.append((key, value))
        kept_keys.append(key)

    if not kept_keys:
        raise MTLRepairError("兼容模式未识别到任何 Landsat 必需字段，已终止（未写出文件）")

    new_lines = ["GROUP = L1_METADATA_FILE"]
    for key, value in kept:
        new_lines.append(f"  {key} = {value}")
    new_lines.append("END_GROUP = L1_METADATA_FILE")
    new_lines.append("END")
    return new_lines, dropped, kept_keys


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def check_band_files(source: Path, labels: list[tuple[str, str]]) -> list[str]:
    missing: list[str] = []
    for key, value in labels:
        if not key.upper().startswith("FILE_NAME_BAND_"):
            continue
        name = value.strip().strip('"').strip("'")
        if not name:
            continue
        candidate = (source.parent / name)
        if not candidate.is_file():
            missing.append(name)
    return missing


def _collection_number(labels: list[tuple[str, str]]) -> int | None:
    raw = _first_value(labels, "COLLECTION_NUMBER")
    if raw:
        digits = re.sub(r"\D", "", raw)
        if digits:
            return int(digits)
    product_id = (_first_value(labels, "LANDSAT_PRODUCT_ID")
                  or _first_value(labels, "PRODUCT_ID"))
    m = re.search(r"_(\d{2})_T\d", product_id)
    if m:
        return int(m.group(1))
    return None


def decide_compat(labels: list[tuple[str, str]]) -> tuple[bool, str]:
    """按实测结论选择模式：Collection 2 或 Level-2 默认兼容模式，其余最小补丁。"""
    product_id = (_first_value(labels, "LANDSAT_PRODUCT_ID")
                  or _first_value(labels, "PRODUCT_ID"))
    level = (_first_value(labels, "PROCESSING_LEVEL")
             or _first_value(labels, "PRODUCT_TYPE"))
    collection = _collection_number(labels)
    is_l2 = bool(re.search(r"_L2[^_]*_", product_id, re.I)) or level.upper().startswith("L2")

    if collection == 2:
        return True, "检测到 Collection 2（COLLECTION_NUMBER=02），按实测结论默认使用兼容模式"
    if is_l2:
        return True, f"检测到 Level-2 产品（{level or 'L2SP/L2SR'}），按实测结论默认使用兼容模式"
    if collection == 1 and level.upper().startswith("L1"):
        return False, "Collection 1 + Level-1，使用最小补丁（只改 SPACECRAFT_ID）"
    return False, "未能判定集合/级别，保守使用最小补丁；若 ENVI 报错请改用 --compat"


def repair_mtl(source: str | Path, compat: bool | None = None,
               change_product_id: bool = False,
               envi56: bool = False) -> RepairResult:
    """修复单个 MTL 文件。

    默认在备份后原地覆盖同名的 *_MTL.txt，保证 ENVI 仍按标准命名识别。
    compat=None 时按产品类型自动选择；True/False 为强制模式。
    envi56=True 时另存为 *_MTL_ENVI56.txt（ENVI 无法识别此命名，仅作留档）。
    失败抛 MTLRepairError，绝不写出半成品。
    """
    src = Path(source).expanduser()
    if not src.is_file():
        raise MTLRepairError(f"找不到文件：{src}")
    if src.suffix.lower() not in {".txt", ".mtl"}:
        raise MTLRepairError(f"不是 MTL 文本文件：{src}")

    result = RepairResult(source=src)

    text, enc_note = read_source(src)
    if enc_note:
        result.notes.append(enc_note)
    newline = detect_newline(text)
    lines = text.splitlines()
    if not lines:
        raise MTLRepairError(f"文件为空：{src}")

    problems = validate_odl(lines)
    if problems:
        raise MTLRepairError(
            "输入文件不是合法/完整的 ODL 结构，已终止且未生成任何输出。\n  - "
            + "\n  - ".join(problems[:8])
        )

    labels_before = collect_labels(lines)
    out_name = output_name_for(src.name, envi56)
    out_path = src.parent / out_name

    # ENVI 只认 *_MTL.txt；非该命名的原地输出直接拒写（避免又生成一个 ENVI 打不开的文件）
    if not envi56 and not out_name.lower().endswith("_mtl.txt"):
        raise MTLRepairError(
            f"输出文件名“{out_name}”不是 ENVI 要求的标准命名 *_MTL.txt，已拒绝写入。\n"
            "  请先把原文件改名为 *_MTL.txt 后重试；或使用 --envi56 仅作留档（ENVI 无法识别该命名）。"
        )
    if envi56:
        result.warnings.append(
            "此命名 *_MTL_ENVI56.txt 不被 ENVI 识别（会报 Unrecognized File Type），"
            "仅作留档/比对用；在 ENVI 中必须使用原名 *_MTL.txt"
        )

    if compat is None:
        compat, reason = decide_compat(labels_before)
        result.mode_reason = reason + "（自动选择）"
    else:
        result.mode_reason = "强制兼容模式（--compat）" if compat else "强制最小补丁（--minimal）"
    result.notes.append("模式选择：" + result.mode_reason)

    if _spacecraft_state(text) == ENVI_TARGET_NAME:
        result.already_fixed = True
        result.notes.append("看起来已是修复版（SPACECRAFT_ID 已是 LANDSAT_8），仍按要求先备份原文件")
    elif envi56 and out_path.is_file():
        try:
            existing_text = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing_text = ""
        if _spacecraft_state(existing_text) == ENVI_TARGET_NAME:
            result.already_fixed = True
            result.notes.append(
                f"已存在修复版输出 {out_name}，本次按原文件重新生成（结果保持一致），仍先备份原件"
            )

    # 1) 备份（任何写入之前）
    result.backup = create_backup(src)

    # 2) 波段文件存在性检查（只警告，不擅自改名）
    result.missing_band_files = check_band_files(src, labels_before)
    if result.missing_band_files:
        listing = "\n    ".join(result.missing_band_files)
        result.warnings.append(
            "以下 FILE_NAME_BAND_* 指向的文件不存在（ENVI 打开时可能报错，请核对）：\n    " + listing
        )
    if not any(k.upper().startswith("FILE_NAME_BAND_") for k, _ in labels_before):
        result.warnings.append("未找到任何 FILE_NAME_BAND_* 字段，可能不是 Landsat MTL")

    # 3) 打补丁
    patch_spacecraft(lines, result.changes, result.notes)
    if change_product_id:
        patch_product_ids(lines, result.changes)

    # 4) 兼容模式（可选）
    if compat:
        lines, dropped, kept = build_compat_lines(lines)
        result.removed = dropped
        result.notes.append(f"兼容模式：保留 {len(kept)} 个字段，删除/合并 {len(dropped)} 个字段")

    output_text = newline.join(lines) + newline
    out_lines = output_text.splitlines()
    out_problems = validate_odl(out_lines)
    if out_problems:
        raise MTLRepairError(
            "生成的 ODL 结构校验未通过，已终止且未写出文件。\n  - "
            + "\n  - ".join(out_problems[:8])
        )

    # 5) 原子写入（原地替换时先写临时文件，失败则原文件保持不动）
    tmp = src.parent / f".{out_name}.tmp-{os.getpid()}"
    try:
        tmp.write_bytes(output_text.encode("utf-8"))
        os.replace(tmp, out_path)
    except OSError as exc:
        raise MTLRepairError(f"写出文件失败（原文件未改动）：{exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    result.output = out_path
    if envi56:
        result.notes.append(f"已另存为 {out_name}，原 MTL 未改动")
    else:
        result.notes.append("已原地覆盖原 *_MTL.txt（备份可用于还原）")

    # 6) 日志
    backup_dir = backup_dir_for(src)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_lines = [
        "",
        "=" * 68,
        f"[{stamp}] 修复 MTL",
        f"  原文件：{src}",
        f"  备份  ：{result.backup}",
        f"  输出  ：{out_path}",
        f"  输出方式：{'另存为 ENVI56（不覆盖原件）' if envi56 else '原地覆盖原 *_MTL.txt'}",
        f"  原文件已被覆盖：{'否' if envi56 else '是'}",
        f"  模式  ：{'兼容模式（Collection 1 风格）' if compat else '默认最小补丁'}"
        f"（{result.mode_reason}）",
        f"  产品号：{'已同时改为 LC08 前缀' if change_product_id else '未改动'}",
        f"  改动  ：{len(result.changes)} 项" if result.changes else "  改动  ：无",
    ]
    for key, old, new in result.changes:
        log_lines.append(f"    - {key}: {old} -> {new}")
    if compat:
        log_lines.append(f"  兼容模式删除/合并字段 {len(result.removed)} 个：")
        log_lines.append("    " + "，".join(result.removed))
    if result.missing_band_files:
        log_lines.append("  缺失波段文件：")
        log_lines.append("    " + "，".join(result.missing_band_files))
    for w in result.warnings:
        log_lines.append("  警告：" + w.replace("\n", " "))
    for note in result.notes:
        log_lines.append("  说明：" + note)
    log_lines.append("  结果：成功")
    result.log_path = append_log(backup_dir, "\n".join(log_lines))

    result.success = True
    result.message = "完成"
    return result


# ----------------------------------------------------------------------------
# 目录自检
# ----------------------------------------------------------------------------

def self_check_dir(target: str | Path) -> str:
    """检查目录：是否同时存在原名 MTL 与 ENVI56 版本，帮助用户避免选错文件。"""
    d = Path(target).expanduser()
    if d.is_file():
        d = d.parent
    if not d.is_dir():
        raise MTLRepairError(f"不是目录：{d}")

    originals = sorted(p for p in d.iterdir()
                       if p.is_file() and p.name.lower().endswith(("_mtl.txt", "_mtl.mtl")))
    envies = sorted(p for p in d.iterdir()
                    if p.is_file() and p.name.lower().endswith("_mtl_envi56.txt"))
    out = [f"【自检】{d}"]
    out.append(f"  原名 MTL（ENVI 可识别）：{len(originals)} 个")
    for p in originals:
        state = _spacecraft_state(read_source(p)[0]) or "未找到 SPACECRAFT_ID"
        missing = check_band_files(p, collect_labels(read_source(p)[0].splitlines()))
        out.append(f"    - {p.name}  [SPACECRAFT_ID={state}] 缺失TIF {len(missing)} 个")
    out.append(f"  ENVI56 版本（ENVI 无法识别）：{len(envies)} 个")
    for p in envies:
        out.append(f"    - {p.name}")

    warnings: list[str] = []
    for env in envies:
        base = source_base_name(env.name)
        matches = [o for o in originals if source_base_name(o.name) == base]
        if matches:
            warnings.append(
                f"同时存在 {env.name} 与 {matches[0].name}：在 ENVI 中请选择原名 {matches[0].name}"
            )
        else:
            warnings.append(
                f"只有 {env.name} 而没有同名 *_MTL.txt：ENVI 无法识别，请用 --restore 还原，"
                "或把修复后的内容另存为 *_MTL.txt"
            )
    if not originals and envies:
        warnings.append("目录中没有标准 *_MTL.txt，只有 ENVI56 版本，ENVI 打开必然失败")
    if not originals and not envies:
        warnings.append("目录中未找到任何 *_MTL.txt")
    if warnings:
        out.append("  警告：")
        out += [f"    - {w}" for w in warnings]
    else:
        out.append("  结论：命名状态正常，可直接在 ENVI 中选择 *_MTL.txt")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# 诊断模式
# ----------------------------------------------------------------------------

def _first_value(labels: list[tuple[str, str]], key: str) -> str:
    for k, v in labels:
        if k.upper() == key.upper():
            return v.strip().strip('"').strip("'")
    return ""


def diagnose_mtl(source: str | Path) -> str:
    """输出一份可直接贴给他人排查的景信息报告（不修改任何文件）。"""
    src = Path(source).expanduser()
    if not src.is_file():
        raise MTLRepairError(f"找不到文件：{src}")

    text, enc_note = read_source(src)
    newline = detect_newline(text)
    raw = src.read_bytes()
    lines = text.splitlines()
    labels = collect_labels(lines)
    problems = validate_odl(lines)

    out: list[str] = [
        f"【诊断】{src}",
        f"  文件大小：{len(raw)} 字节",
        f"  编码：{'含 BOM' if raw[:1] == b'\xef' else '无 BOM'}，"
        f"行尾：{'CRLF' if newline == chr(13) + chr(10) else ('LF' if newline == chr(10) else 'CR')}",
    ]
    if enc_note:
        out.append(f"  编码说明：{enc_note}")

    spacecraft = _first_value(labels, "SPACECRAFT_ID")
    out.append(f"  SPACECRAFT_ID：{spacecraft or '（缺失）'}")
    out.append(f"  SENSOR_ID：{_first_value(labels, 'SENSOR_ID') or '（缺失）'}")
    product_id = _first_value(labels, "LANDSAT_PRODUCT_ID") or _first_value(labels, "PRODUCT_ID")
    out.append(f"  产品号：{product_id or '（缺失）'}")
    out.append(f"  PRODUCT_TYPE / LEVEL：{_first_value(labels, 'PRODUCT_TYPE') or '（缺失）'} / "
               f"{_first_value(labels, 'PROCESSING_LEVEL') or '（缺失）'}")
    out.append(f"  集合：Collection {_first_value(labels, 'COLLECTION_NUMBER') or '?'} "
               f"{_first_value(labels, 'COLLECTION_CATEGORY')}")
    out.append(f"  日期：{_first_value(labels, 'DATE_ACQUIRED')} {_first_value(labels, 'SCENE_CENTER_TIME')}")
    out.append(f"  路径/行：{_first_value(labels, 'WRS_PATH')}/{_first_value(labels, 'WRS_ROW')}")
    out.append(f"  ODL 结构：{'通过' if not problems else '存在问题 → ' + '；'.join(problems[:5])}")
    if spacecraft.upper() == ENVI_TARGET_NAME:
        out.append("  状态：SPACECRAFT_ID 已是 LANDSAT_8（看起来已修复过）")

    out.append("  波段文件（FILE_NAME_BAND_*）：")
    band_items = [(k, v) for k, v in labels if k.upper().startswith("FILE_NAME_BAND_")]
    if not band_items:
        out.append("    （未找到任何 FILE_NAME_BAND_* 字段）")
    for key, value in band_items:
        name = value.strip().strip('"').strip("'")
        exists = (src.parent / name).is_file()
        out.append(f"    {'[存在]' if exists else '[缺失]'} {key} = {name}")

    tifs = sorted(p.name for p in src.parent.iterdir()
                  if p.is_file() and p.suffix.lower() in {".tif", ".tiff"})
    out.append(f"  目录中实际 TIF（{len(tifs)} 个）：")
    for name in tifs:
        out.append(f"    - {name}")
    out += ["    （以上无对应关系时，请勿手工改名，可先用 --make-hdr 兜底）"]

    out.append("  分组清单：")
    depth = 0
    for line in lines:
        s = line.strip()
        m = GROUP_RE.match(s)
        if m:
            out.append("    " + "  " * depth + f"GROUP = {m.group(1)}")
            depth += 1
            continue
        m = END_GROUP_RE.match(s)
        if m:
            depth = max(depth - 1, 0)
            out.append("    " + "  " * depth + f"END_GROUP = {m.group(1)}")
    out.append("  （诊断完成，以上文本可直接复制反馈）")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# 兜底：从 TIF 生成 ENVI 头文件（不依赖 MTL）
# ----------------------------------------------------------------------------

_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_TIFF_TYPE_FMT = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d",
                  16: "Q", 17: "q"}
_ENVI_DATA_TYPE = {
    (8, 1): 1, (16, 1): 12, (16, 2): 2,
    (32, 1): 13, (32, 2): 3, (32, 3): 4, (64, 3): 5,
}
_BAND_WAVELENGTH = {
    1: 0.46, 2: 0.48, 3: 0.56, 4: 0.65, 5: 0.86, 6: 1.61,
    7: 2.20, 8: 0.59, 9: 1.37, 10: 10.90, 11: 12.00,
}


def _decode_tiff_values(payload: bytes, typ: int, count: int, order: str):
    prefix = "<" if order == "little" else ">"
    if typ == 2:
        return payload.decode("latin-1").replace("\x00", "")
    if typ in (5, 10):
        nums = struct.unpack(prefix + ("I" if typ == 5 else "i") * 2 * count, payload)
        return [nums[i] / nums[i + 1] if nums[i + 1] else 0.0
                for i in range(0, len(nums), 2)]
    if typ not in _TIFF_TYPE_FMT:
        return payload
    fmt = prefix + _TIFF_TYPE_FMT[typ] * count
    values = list(struct.unpack(fmt, payload))
    return values[0] if count == 1 else values


def read_tiff_tags(path: Path) -> tuple[dict, str]:
    """读取 GeoTIFF 的常用标签，纯标准库实现。"""
    tags: dict = {}
    with path.open("rb") as f:
        head = f.read(8)
        if not head or head[:2] not in (b"II", b"MM"):
            raise MTLRepairError(f"不是有效的 TIFF（缺少 II/MM 标识）：{path.name}")
        order = "little" if head[:2] == b"II" else "big"
        if int.from_bytes(head[2:4], order) != 42:
            raise MTLRepairError(f"不是经典 TIFF（魔数不是 42）：{path.name}")
        ifd_offset = int.from_bytes(head[4:8], order)
        f.seek(ifd_offset)
        raw_count = f.read(2)
        if not raw_count:
            raise MTLRepairError(f"TIFF 目录为空：{path.name}")
        count = int.from_bytes(raw_count, order)
        entries = []
        for _ in range(count):
            entry = f.read(12)
            if len(entry) < 12:
                break
            entries.append((
                int.from_bytes(entry[0:2], order),
                int.from_bytes(entry[2:4], order),
                int.from_bytes(entry[4:8], order),
                entry[8:12],
            ))
        for tag, typ, cnt, raw in entries:
            size = _TIFF_TYPE_SIZE.get(typ)
            if size is None:
                continue
            total = size * cnt
            if total <= 4:
                payload = raw[:total]
            else:
                value_offset = int.from_bytes(raw, order)
                pos = f.tell()
                f.seek(value_offset)
                payload = f.read(total)
                f.seek(pos)
            try:
                tags[tag] = _decode_tiff_values(payload, typ, cnt, order)
            except struct.error:
                continue
    return tags, order


def _parse_geokeys(values) -> dict:
    keys: dict = {}
    if not values or len(values) < 4:
        return keys
    count = values[3]
    for i in range(count):
        base = 4 + i * 4
        if base + 3 >= len(values):
            break
        key_id, location, _, value = values[base:base + 4]
        if location == 0:
            keys[key_id] = value
    return keys


def _build_hdr_text(tif: Path, tags: dict, order: str) -> tuple[str, str]:
    """返回 (ENVI hdr 文本, 备注)。备注说明精度/投影是否缺失。"""
    width = int(tags.get(256) or 0)
    height = int(tags.get(257) or 0)
    if not width or not height:
        raise MTLRepairError(f"TIFF 缺少宽高标签：{tif.name}")

    bands = int(tags.get(277) or 1)
    bits = tags.get(258)
    bits = bits[0] if isinstance(bits, list) else bits
    bits = int(bits or 8)
    sample_format = tags.get(339)
    sample_format = sample_format[0] if isinstance(sample_format, list) else sample_format
    sample_format = int(sample_format or 1)
    data_type = _ENVI_DATA_TYPE.get((bits, sample_format))
    if data_type is None:
        raise MTLRepairError(f"暂不支持的 TIFF 数据类型：{bits} 位 / 样本格式 {sample_format}")

    notes = "hdr 由纯 Python 解析 GeoTIFF 标签生成，未使用 MTL"
    if tags.get(259):
        notes += "；该 TIFF 使用了压缩存储，ENVI 读取头文件时不支持压缩，建议直接用 File > Open 打开 TIF"

    description = "ENVI header generated by fix_mtl.py (no MTL used)"
    lines = [
        "ENVI",
        f"description = {{{description}}}",
        f"samples = {width}",
        f"lines = {height}",
        f"bands = {bands}",
        "header offset = 0",
        "file type = TIFF",
        f"data type = {data_type}",
        "interleave = bsq",
        f"byte order = {0 if order == 'little' else 1}",
    ]

    m = re.search(r"_B(\d+)\.(?:tif|tiff)$", tif.name, re.I)
    band_no = int(m.group(1)) if m else None
    if band_no:
        lines.append(f"band names = {{B{band_no}}}")
        if band_no in _BAND_WAVELENGTH:
            lines.append("wavelength units = Micrometers")
            lines.append(f"wavelength = {{{_BAND_WAVELENGTH[band_no]:.2f}}}")

    scale = tags.get(33550)
    tiepoint = tags.get(33922)
    geokeys = _parse_geokeys(tags.get(34735))
    epsg = geokeys.get(3072)
    if scale and tiepoint and epsg and len(tiepoint) >= 6:
        sx, sy = float(scale[0]), float(scale[1])
        i, j = float(tiepoint[0]), float(tiepoint[1])
        x, y = float(tiepoint[3]), float(tiepoint[4])
        ulx = x - i * sx
        uly = y + j * sy
        zone = hemi = datum = None
        if 32601 <= epsg <= 32660:
            zone, hemi, datum = epsg - 32600, "North", "WGS-84"
        elif 32701 <= epsg <= 32760:
            zone, hemi, datum = epsg - 32700, "South", "WGS-84"
        elif 26901 <= epsg <= 26923:
            zone, hemi, datum = epsg - 26900, "North", "NAD83"
        elif 26701 <= epsg <= 26722:
            zone, hemi, datum = epsg - 26700, "North", "NAD27"
        if zone:
            lines.append(
                f"map info = {{UTM, 1.0000, 1.0000, {ulx + sx / 2:.6f}, {uly - sy / 2:.6f}, "
                f"{sx:.6f}, {sy:.6f}, {zone}, {hemi}, {datum}, units=Meters}}"
            )
        else:
            notes += f"；识别到 EPSG:{epsg}，但不是常见 UTM 代码，未写 map info，请在 ENVI 中手工指定投影"
    else:
        notes += "；未解析到 GeoTIFF 投影标签（GeoKeyDirectory/ModelTiepoint/PixelScale），未写 map info"
    return "\n".join(lines) + "\n", notes


def _collect_band_tifs(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir()
                      if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}
                      and re.search(r"_B\d+$", p.stem, re.I))
    if target.is_file() and target.suffix.lower() in {".tif", ".tiff"}:
        return [target]
    return []


def make_envi_headers(target: str | Path, overwrite: bool = False) -> list[str]:
    """为每个波段 TIF 生成 ENVI .hdr；返回逐条中文说明（不依赖 MTL）。"""
    tgt = Path(target).expanduser()
    if not tgt.exists():
        raise MTLRepairError(f"路径不存在：{tgt}")
    tifs = _collect_band_tifs(tgt)
    if not tifs:
        raise MTLRepairError(f"未找到波段 TIF（*_B1.TIF 这类命名）：{tgt}")

    reports = [f"【ENVI 头文件】共 {len(tifs)} 个 TIF，目录：{tifs[0].parent}"]
    for tif in tifs:
        hdr = tif.with_suffix(".hdr")
        if hdr.exists() and not overwrite:
            reports.append(f"  跳过 {hdr.name}（已存在，加 --hdr-overwrite 可覆盖）")
            continue
        tags, order = read_tiff_tags(tif)
        text, note = _build_hdr_text(tif, tags, order)
        tmp = hdr.with_name(hdr.name + f".tmp-{os.getpid()}")
        try:
            tmp.write_text(text, encoding="ascii")
            os.replace(tmp, hdr)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        reports.append(f"  生成 {hdr.name}：{note}")
    reports.append(
        "  用法：ENVI 中用 File > Open 打开各 TIF（会自动读取同名 .hdr），"
        "再用 Layer Stacking 按 B1→B11 顺序建栈；单波段定标请在 ENVI 中设置。"
    )
    return reports


# ----------------------------------------------------------------------------
# 命令行输出
# ----------------------------------------------------------------------------

def format_result(result: RepairResult) -> str:
    out = [f"【文件】{result.source}"]
    if result.backup:
        out.append(f"  原始备份 -> {result.backup}")
    if result.output:
        out.append(f"  输出文件 -> {result.output}")
    if result.already_fixed:
        out.append("  提示：看起来已是修复版，本次仍已备份。")
    if result.changes:
        items = "；".join(f'{k}: {o} -> {n}' for k, o, n in result.changes)
        out.append(f"  改动 {len(result.changes)} 项：{items}")
    else:
        out.append("  改动：无（未找到需要修改的标签）")
    if result.removed:
        out.append(f"  兼容模式删除/合并字段 {len(result.removed)} 个（详见日志）")
    for note in result.notes:
        out.append(f"  说明：{note}")
    for warning in result.warnings:
        out.append("  警告：" + warning.replace("\n", "\n          "))
    out.append(f"  日志：{result.log_path}")
    return "\n".join(out)


def _maybe_pause() -> None:
    """打包成 exe 后被拖拽时，保留窗口显示结果。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键关闭窗口…")
    except (EOFError, OSError):
        pass


def print_usage_hint() -> None:
    print(f"{APP_NAME}\n")
    print("用法：")
    print("  python fix_mtl.py <MTL 文件...>            # 默认：备份后原地覆盖 *_MTL.txt")
    print("  python fix_mtl.py --envi56 <文件...>       # 另存为 *_MTL_ENVI56.txt（不动原件）")
    print("  python fix_mtl.py --compat <文件...>       # 强制兼容模式")
    print("  python fix_mtl.py --minimal <文件...>      # 强制最小补丁（只改 SPACECRAFT_ID）")
    print("  python fix_mtl.py --change-product-id ... # 同时把 LC09 产品号改为 LC08")
    print("  python fix_mtl.py --restore <文件...>      # 从备份还原到原文件名")
    print("  python fix_mtl.py --diagnose <文件...>     # 只诊断，不改文件")
    print("  python fix_mtl.py --self-check <目录>      # 检查是否选错 ENVI56 文件")
    print("  python fix_mtl.py --make-hdr <目录或TIF>   # 不依赖 MTL，从 TIF 生成 .hdr")
    print("\n也可以把 MTL 文件直接拖到 修复Landsat元数据.bat 上运行。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fix_mtl.py",
        description=f"{APP_NAME}（ENVI 5.6 兼容补丁，纯标准库实现）",
        add_help=True,
    )
    parser.add_argument("files", nargs="*", help="一个或多个 *_MTL.txt / *_MTL.MTL / TIF 目录路径")
    parser.add_argument("--compat", action="store_true",
                        help="强制兼容模式（默认自动：Collection 2 / Level-2 会选兼容模式）")
    parser.add_argument("--minimal", action="store_true",
                        help="强制最小补丁（只改 SPACECRAFT_ID，不做兼容模式扁平化）")
    parser.add_argument("--change-product-id", action="store_true",
                        help="同时把 LANDSAT_PRODUCT_ID / PRODUCT_ID 中的 LC09 改为 LC08（默认不改）")
    parser.add_argument("--envi56", action="store_true",
                        help="另存为 *_MTL_ENVI56.txt，不覆盖原件（默认原地覆盖 *_MTL.txt）")
    parser.add_argument("--overwrite", action="store_true",
                        help="已过时：现在默认就是原地覆盖，保留该参数仅为兼容旧命令")
    parser.add_argument("--restore", action="store_true",
                        help="从备份还原到原文件名（自动避开已打补丁的备份）")
    parser.add_argument("--backup", metavar="备份文件", default=None,
                        help="配合 --restore 指定要还原的具体备份文件")
    parser.add_argument("--diagnose", action="store_true",
                        help="只输出诊断信息（SPACECRAFT_ID、产品号、波段文件是否存在、分组清单）")
    parser.add_argument("--self-check", action="store_true",
                        help="检查目录里是否同时存在原名 MTL 与 ENVI56 版本（判断是否选错文件）")
    parser.add_argument("--make-hdr", action="store_true",
                        help="兜底方案：不依赖 MTL，为每个波段 TIF 生成 ENVI .hdr")
    parser.add_argument("--hdr-overwrite", action="store_true",
                        help="配合 --make-hdr 覆盖已存在的 .hdr")
    args = parser.parse_args(argv)

    if not args.files:
        print_usage_hint()
        _maybe_pause()
        return 1
    if args.compat and args.minimal:
        print("【失败】--compat 与 --minimal 不能同时使用。")
        _maybe_pause()
        return 1
    compat_flag: bool | None = True if args.compat else (False if args.minimal else None)

    exit_code = 0
    for raw in args.files:
        path = Path(raw).expanduser()
        try:
            if args.diagnose:
                print(diagnose_mtl(path))
            elif args.self_check:
                print(self_check_dir(path))
            elif args.make_hdr:
                for line in make_envi_headers(path, overwrite=args.hdr_overwrite):
                    print(line)
            elif args.restore:
                result = restore_from_backup(path, args.backup)
                print(f"【已还原】{result.output}")
                for note in result.notes:
                    print(f"  说明：{note}")
                print(f"  使用的备份：{result.backup}")
            else:
                result = repair_mtl(
                    path,
                    compat=compat_flag,
                    change_product_id=args.change_product_id,
                    envi56=args.envi56,
                )
                print(format_result(result))
            print()
        except MTLRepairError as exc:
            exit_code = 1
            print(f"【失败】{path}\n  {exc}\n")
        except Exception as exc:  # 兜底：不吞掉细节，方便用户反馈
            exit_code = 1
            print(f"【意外错误】{path}\n  {type(exc).__name__}: {exc}\n")

    if exit_code == 0:
        print("全部处理完成。")
    else:
        print("存在处理失败的文件，请查看上面的错误信息。")
    _maybe_pause()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
