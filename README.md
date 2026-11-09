# Landsat 元数据修复工具

把 Landsat 8/9 的 `*_MTL.txt` 拖进来，工具先备份原件，再把修复后的内容原地写回同名 `*_MTL.txt` —— ENVI 5.6 只认这个命名。

> 只改元数据文本，不动任何 TIF、不重命名、不移动文件。

## 功能

- **原地覆盖 + 自动备份**：修复前把原件复制到 `_MTL_原始备份/`，失败时保留原件不动
- **两种补丁模式**：兼容模式（Collection 2 / Level-2）、最小补丁（Collection 1 / Level-1），支持自动判定
- **幂等**：重复运行先备份但不重复改写
- **附带能力**：还原备份、元数据诊断、目录自检、生成 `.hdr` 文件

## 使用方式

| 方式 | 操作 |
|---|---|
| 窗口版 | 双击 `Landsat元数据修复工具_窗口版.exe`，把 MTL 文件或数据目录拖进窗口 |
| 命令行版 | 双击 `Landsat元数据修复工具.exe`，或 `修复Landsat元数据.bat <路径>` |
| 源码运行 | `python fix_mtl.py <路径>` / `python gui_mtl.py` |

支持的参数：`--compat`（强制兼容模式）、`--minimal`（强制最小补丁）、`--restore`（还原备份）、`--diagnose`（诊断）、`--self-check`（目录自检）、`--make-hdr`（生成 hdr）。

完整说明见 [使用说明.md](使用说明.md)，构建与验证记录见 [测试记录.md](测试记录.md)。

## 构建

需要 Python 3.10+ 与依赖：

```powershell
python -m pip install pyinstaller tkinterdnd2
python -m PyInstaller --noconfirm --clean --distpath exe "build\Landsat元数据修复工具.spec"
python -m PyInstaller --noconfirm --clean --distpath exe "build\Landsat元数据修复工具_窗口版.spec"
```

产物位于 `exe/`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 已知限制

- 仅支持 Python 3 标准库运行，无需第三方库；窗口版的拖拽功能依赖 `tkinterdnd2`，缺失时自动降级为点击选择
- 已在 ENVI 5.6 上验证 Collection 2 Level-2 数据；其他 ENVI 小版本、真实生产 MTL 建议先用 `--diagnose` 核对
- 仅在 Windows 上验证
