# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.3.3] - 2026-09-18

0.3.2 把 CI 的构建 Python 换成了 3.13，但 **Windows 离线套件里的 wheel 还是 cp312** ——
`build_windows_release.ps1` 给 `build_offline_bundle.py` 写死了 `--python 3.12`，
那个包拿到 3.13 的机器上装不上。本版修掉，并加上出包后的自动校验。

### 修复

- `build_windows_release.ps1`：离线套件的 `--python` 改为**跟着构建解释器走**
  （原来写死 3.12），并在 `-PythonInstaller` 的版本与构建版本不一致时直接报错
- 新增 `scripts/check_windows_kit.py`：校验「CI 的 PYTHON_VERSION / 自带安装器 /
  包里 wheel 的 cp 标签 / MANIFEST 里的目标 Python」四者一致，CI 出包后自动跑，
  不一致就发不出去（这个坑踩了两次，本地 Linux 开发机测不出来）

### 变更

- Windows 产物统一由 Python 3.13 构建（0.3.2 起），自带安装器
  `python-3.13.7-amd64.exe`

> 0.3.2 的 `windows-x64-offline-kit.zip` 请勿使用，用 0.3.3 的。

## [0.3.2] - 2026-09-18

本版修两个会把内网实施卡住的问题：Windows 产物是按 **Python 3.12** 构建的（而开发环境是 3.13，
拿过去根本装不上），以及三个产物名字分不清哪个给谁用。

### 修复

- **Windows 产物改用 Python 3.13 构建**：CI 的 `PYTHON_VERSION` 由 3.12 改为 3.13，
  自带的 Python 安装器换成 `python-3.13.7-amd64.exe`。
  之前 `windows-x64.zip` 与 `offline-win-x64.zip` 里是 cp312 的 wheel，
  在 3.13 的机器上会报 `not a supported wheel on this platform`

### 变更

- **Release 产物改名**，别再让人对着名字猜（旧名混着 offline / windows 两种词序）：
  - `datacompare-<ver>-windows-x64.zip` → `-windows-x64-standalone.zip`（免 Python，解压即用）
  - `datacompare-<ver>-offline-win-x64.zip` → `-windows-x64-offline-kit.zip`（自带安装器 + wheels + 源码）
  - `datacompare-<ver>-offline-bundle.tar.gz` → `-wheels-src-py<版本>-<平台>.tar.gz`
    （自备 Python；名字里的 `py313` / `linux-win` 直接说明兼容性与平台）
  - Release 说明自动附上「三个文件怎么选」表格（`docs/RELEASE-ASSETS.md`）
- 清理死代码（全仓库搜过确认零引用）：`SourceSpec.display_name()`、`is_excel_kind`、
  `Stats._ratio()`、`ColumnResult.diff_rate`、`normalize.normalize_text_py()`、
  `numeric_clean_expr()`；最后一个连带删掉只服务它的 `_FW_TABLE`

### 文档

- 新增 `docs/RELEASE-ASSETS.md`（产物选择表，同时会拼进 Release 说明）
- `README.md`：离线部署的体积改成实测值（默认包 102MB / tar.gz 72MB / 免 Python 包 130MB）、
  制作命令去掉写死的 `--python 3.12`、开发与测试一节换成实际命令
  （`requirements-dev.txt`、pytest、pyflakes）并补上 CI 四个作业的说明
- `docs/DEPLOY.md`：方案对比表补实测体积与「方案 C 见常见问题」的指引；
  Linux 整体包的做法换成已验证的写法 —— 日志走 stderr、只把 tar 流到 stdout，
  避免重定向出「假 tar.gz」（踩过：得到 45MB 文本文件，`tar` 报 not in gzip format）
- `scripts/build_exe.py`：docstring 补 binutils 依赖与 glibc 注意事项

## [0.3.1] - 2026-09-18

本版重点：**修掉离线包「装了也跑不起来」的两类问题**，并按实际部署口径把范围收口 ——
开发环境是 Python 3.13，离线 wheel 包就只服务它；运行环境可能连 Python 都没有，
那条路走方案 B 的免安装整体包（无需 Python）。

### 修复

- **离线包的开发/测试依赖漏了环境标记依赖**：`pip download --python-version` /
  `--platform` 只影响「选哪个 wheel」，**不按目标解释器评估 marker**，
  导致 `colorama`（Windows 上 pytest 要用）没进包 —— Windows 内网
  `install_dev_offline.sh` 直接装不上
- **同一个包多版本导致内网 pip 解析失败**：包里同时存在多个 setuptools/pytest/pip
  版本时，内网 `pip install -r requirements-dev.txt` 会长时间 backtracking，
  最后报 `ERROR: Package 'setuptools' requires a different Python`。
  现在开发工具链钉死成 `DEV_TOOLCHAIN`，每个包只留一个版本
- `download_wheels()` 里 `req_rel` 优先级高于 `packages`，显式包名被静默忽略
  （0.3.0 声称「不再按构建机平台重复解析运行依赖」，实际没生效）
- 个别「Python × 平台」组合下不到 wheel（如 `3.9 + manylinux_2_28_x86_64`，
  老 Python 在新 glibc 标签下已无 duckdb wheel）不再当致命错误：只警告并记入
  `MANIFEST.txt`；但某个 Python 版本在所有平台下都下不到时会立即报错，
  不会默默产出一个装不上的包
- 开发依赖按平台各下一遍：tomli 2.4 起带 `cpXXX` 原生 wheel，
  只下构建机平台会让 Windows 目标缺 wheel

### 变更

- 离线包默认目标收成 `--python 3.13`（＝当前开发环境），CI 默认值同步；
  内网开发机是别的 Python 版本时用 `--python` 显式指定
- `scripts/build_offline_bundle.py` 不再用 `argparse.BooleanOptionalAction`
  （3.9+ 语法），改成 `--x` / `--no-x` 两个开关

### 测试

- CI 新增 `test-offline` 作业：在 `python:3.13-slim` 容器里**真离线**装一遍
  （`--no-index --find-links`）再跑测试，专治上面两类问题 ——
  本地开发机只有一个解释器，跑 `pip download` 是验证不了「内网装得上」的
- `build` 与 `offline-bundle` 两个发版作业改为依赖 `test` + `test-offline`

### 文档

- `docs/DEPLOY.md` 新增「老 Python / 老 glibc 会装到较低版本的 DuckDB」与
  「为什么离线包里的开发工具链是钉死版本的」
- `requirements-dev.txt` 补上「离线包里是钉死版本」的说明

## [0.3.0] - 2026-09-17

本版重点：**离线交付包从「只能跑」升级为「能改」** —— wheel 与源码一起带进内网，
内网装好就能可编辑安装、跑测试、离线重打 wheel；打包这一步也交给 GitHub Actions 自动完成。

### 新增

- 离线交付包升级为「**wheel + 源码**一起带」，内网可以直接二次开发
  - `scripts/build_offline_bundle.py` 默认新增 `source/`（当前工作区快照，
    含未提交改动；`--with-git` 可连 `.git` 历史一起带走）
  - 新增 `requirements-dev.txt`：`setuptools` / `wheel` / `pip` / `pytest`。
    离线做 `pip install -e .` 时 pip 的 build isolation 会去装 setuptools 和 wheel，
    `--no-index` 下这两个必须来自包内，否则内网装不上
  - 新增 `scripts/install_dev_offline.sh` / `.ps1`：离线建 `venv-dev`、
    可编辑安装 `source/`、装 DuckDB 扩展、跑 `pytest` 自检
  - 新增 `MANIFEST.txt`：项目版本号、源码提交号（含未提交改动会标出）、
    生成时间、目标 Python/平台、完整 wheel 清单
  - 包内 `README.txt` 重写，分「只部署」与「二次开发」两条路径
  - `--no-source` / `--no-dev` 可退回到原来只带 wheel 的精简包（约 40MB）
- `scripts/build_windows_release.ps1` 新增 `-WithSource` 开关
  （发布包默认仍不带源码，保持体积）
- **CI 自动产出跨平台离线包**：`.github/workflows/release.yml` 新增 `offline-bundle` 作业
  - 跑在 `ubuntu-latest`（`pip download --platform` 能在 Linux 上一次备齐 Windows wheel，
    不需要两个 runner）
  - 产出 `datacompare-<ver>-offline-bundle.tar.gz`：wheel + 源码 + 开发依赖
  - push `v*` tag 时自动挂到 GitHub Release；`workflow_dispatch` 可手动触发
    （新增 `python_versions` / `platforms` 两个输入，留空用默认 `3.9-3.13` / `linux-x64,win-x64`）
  - Windows 发布流程改为 `-WithSource`，让 `offline-win-x64.zip` 也带源码
- `build_offline_bundle.py` 新增 `--print-version`（CI 取版本号用，不再在 YAML 里写正则）

### 变更

- GitHub Actions 工作流名 `Build Windows Release` → `Build Release`：现在一个 tag
  同时产出 Windows 两个 zip 与跨平台离线包（三个作业：`test` 门禁 → `build` 与
  `offline-bundle` 并行）
- `datacompare-<ver>-offline-win-x64.zip` 也含源码与开发依赖（CI 传 `-WithSource`），
  体积增加约 5MB

### 修复

- `build_offline_bundle.py` 重定向/管道输出时进度日志与 pip 输出顺序错位
- `build_offline_bundle.py` 下载开发依赖时不再按**构建机**平台重复解析运行依赖
  （以前会多带一份用不上的平台 wheel）

### 文档

- `docs/DEPLOY.md` 方案 A 新增「步骤 1′：让 GitHub 帮你打包」与「步骤 4：内网要在源码上二次开发」
- `README.md` 离线内网部署章节补充源码/开发依赖与 CI 打包说明
- 开发依赖单独拆到 `requirements-dev.txt`，生产部署只认 `requirements.txt`

## [0.2.0] - 2026-09-15

本版重点：**加了一个图形界面**，把「41 个命令行参数 / 69 个配置项」的门槛降下来；
同时把 Windows 发布流程做成了自动化。

### 新增

- **本机 Web 图形界面**：`datacompare gui`，三步向导（选数据 → 确认字段与主键 → 执行对比）
  - 刻意只用标准库 `http.server`，不引入 Flask/FastAPI；页面是一个内嵌的单文件 HTML，
    不需要前端构建 —— 离线包里因此**不需要多带任何东西**
  - 点选式目录浏览选择文件，自动识别编码 / 分隔符 / 工作表，可先预览前若干行
  - 字段自动对齐（标出「仅左侧有 / 仅右侧有」的列），推断字段类型与主键并给出依据，可手动修改
  - 实时进度条 + 日志；跑完直接点开 HTML 报告
  - 界面配置可「导出配置」为 YAML，再用 `datacompare compare -c` 完全复现
  - `--host` / `--port`（`0` = 自动选空闲端口）/ `--dir` / `--open-browser`
  - 同一时刻只允许一个任务，避免并发把内存打爆
- `engine.inspect()`：只读前 N 行做字段画像 + 主键建议，秒级返回
  （让用户在真跑之前就能看到字段类型推断依据和主键推断策略）
- `sources.load_source(limit=)`：预览用行数限制，Excel 流式读取时可提前中断
- Windows 一键发布脚本 `scripts/build_windows_release.ps1`，产出两个包：
  - `datacompare-<ver>-windows-x64.zip` —— PyInstaller onedir，解压即用，无需 Python
  - `datacompare-<ver>-offline-win-x64.zip` —— 含 Python 安装器的完整离线包
- GitHub Actions 工作流：推送 `v*` tag 自动构建上述产物并发布 Release
  - 新增 `test` 门禁作业（`ubuntu-latest`）：测试不过就不构建、不发版
  - Release 说明自动从本文件的对应版本段落抽取（`scripts/extract_release_notes.py`），
    不再是一句占位的通用文字
  - 同 tag 重跑时会同步刷新标题与说明，并 `--clobber` 覆盖旧产物
- 添加 `LICENSE`（MIT）

### 变更

- HTTP 接口错误语义修正：文件不存在 / 参数填错 / 文件解析失败返回 **400**，
  服务端异常返回 **500** —— 让前端能和「服务端炸了」区分开

### 修复

- 修复 CI 中中文输出导致的崩溃（`PYTHONIOENCODING=utf-8`）
- 修复 exe 包缺失 DuckDB 扩展的问题
- `scripts/build_windows_release.ps1` 端到端修复

### 文档

- README 新增「图形界面」章节，项目结构补充 `gui.py`，常见问题补充 GUI 相关条目
- `docs/DEPLOY.md` 新增「七、图形界面（可选）」：启动方式、内网共享与**安全提醒**、
  离线环境下的行为、与命令行的等价关系

### 测试

- 新增 `tests/test_gui.py`：17 个端到端用例（真实启动 HTTP 服务并发起请求），
  覆盖目录浏览、防目录穿越、字段预检、导出配置、完整对比流程、并发拒绝

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
