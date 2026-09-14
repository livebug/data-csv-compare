# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-14

首个正式版本。

### 新增

- **全字段对比**：前后数据集逐字段比对，输出差异清单与对比报告
- **数据源**：CSV / Excel（xlsx）；大文件落 DuckDB，内存友好
- **子命令**：`compare`（对比）、`profile`（单文件字段画像）、`init-config`（生成配置模板）
- **报告格式**：控制台 / CSV / Markdown / HTML / Excel
- **匹配与比较**：主键匹配、字段级差异定位、异常数据检测
- **比较选项**：数值容差（相对/绝对）、忽略大小写/空白、类型自动识别、自动主键
- **编码与分隔符**：自动探测，支持 GBK；前后数据集可用不同分隔符 / 编码 / Sheet
- **打包与分发**
  - PyInstaller 打包脚本 `scripts/build_exe.py`（单文件 / 目录模式，可带 DuckDB 扩展）
  - Windows 一键发布脚本 `scripts/build_windows_release.ps1`
  - Python 离线安装包构建 `scripts/build_offline_bundle.py`（wheels + DuckDB 扩展）
  - 离线安装脚本：`scripts/install_offline.ps1`（Windows）、`scripts/install_offline.sh`（Linux）
  - 离线机无 Python 时可用包内自带的 Python 安装器静默安装
- **CI**：GitHub Actions（`.github/workflows/release.yml`），推送 `v*` tag 自动构建
  Windows exe 包 + 离线包并发布 Release

### 说明

- 运行环境：Python 3.9+
- 运行依赖：`duckdb` / `openpyxl` / `PyYAML`
