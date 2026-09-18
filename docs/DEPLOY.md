# 部署方案（Windows / Linux，离线内网）

## 一、先回答「换个语言会不会更快」

**不会。** 实测数据（6 万行 × 20 列）：

| 环节 | 耗时 | 谁在执行 |
| --- | --- | --- |
| 读取 + 字段画像 + 主键匹配 + 逐字段比对 + 异常检测 | **18.0 ~ 21.9s** | DuckDB（C++），与调用语言无关 |
| 生成 SQL 文本（Python 侧全部逻辑） | **< 0.2s** | Python |
| 报告渲染（少量差异时） | 0.2s | Python |
| 报告渲染（104 万个差异单元格的极端情况） | 10.3s | Python |

结论：

1. **计算全在 DuckDB 里。** Python 只做一件事——把「归一化规则 + 差异判定」
   翻译成 SQL 字符串，然后交给 DuckDB 执行。这部分不到总耗时的 1%。
   换成 Go / Rust / C# / Java 绑定，调用的还是**同一个 DuckDB C++ 引擎**，
   引擎部分一秒都不会省。
2. **报告渲染是唯一的 Python 热点**，但已经优化过了（详见下一节），
   而且真实场景（差异不多）只要 0.2s。
3. 换语言的真实成本：约 2000 行 SQL 生成与配置逻辑要重写、51 个测试要重建，
   而收益接近 0。**除非有硬性约束（比如公司禁止 Python 上线），否则不建议。**

> 参考：DuckDB 官方提供 C / C++ / Go / Rust / Java / Node / .NET / R 等绑定，
> 它们都是同一份 C++ 核心的封装，性能一致。

### 已经做掉的性能优化

| 优化项 | 效果 |
| --- | --- |
| 归一化表达式两阶段物化（避免同一正则链重复求值十几次） | 差异计算 105s → 3.4s |
| 给 DuckDB `translate()` 加字符预判闸门 | 字段画像 202s → 2.4s |
| CSV 改用 DuckDB `COPY`（C++）导出 | 8.7s → 0.57s |
| Excel 改用 openpyxl 流式写入 + 控制明细行数 | 37.7s → 9.3s |
| 日期解析加「长相闸门」+ 只对疑似日期的列解析 | 含在上面两项里 |

极端场景（20 万行 × 20 列，**每个单元格都不同**）总耗时从 **7 分钟以上**降到 **2 分钟**。

---

## 二、三种部署方案怎么选

| | 方案 A：离线 wheel 包 | 方案 B：免安装可执行程序 | 方案 C：带 Python 的镜像/基线 |
| --- | --- | --- | --- |
| 目标机器要有 Python | 要（3.9+） | **不要** | 要 |
| 包体积 | 默认（3.13 / linux+win / 含源码与开发依赖）102MB，tar.gz 72MB；`--no-source --no-dev` 降到 54MB，只做一个平台再减一半 | 约 130MB，tar.gz 45MB | 视基线而定 |
| 是否需要编译 | 否 | 否，但在目标系统上打 | 否 |
| 启动速度 | 快 | 快（目录模式 0.1s） | 快 |
| 依赖管理 | pip 离线装 | 全静态 | 镜像自带 |
| 适用场景 | 内网有 Python 环境 | 内网干净、不想装 Python | 已有容器/镜像体系 |
| 状态 | **已实测通过** | **已实测通过** | 做法见「九、常见问题」里的 Docker 一节 |

下面重点讲 A 和 B（方案 C 只是一个 Dockerfile，见常见问题）。

---

## 三、方案 A：离线 wheel 包（推荐）

### 步骤 1：在能联网的机器上制作安装包

```bash
cd data-csv-compare
python scripts/build_offline_bundle.py \
    --out dist/offline-bundle \
    --platforms linux-x64,win-x64 \
    --with-extensions \
    --zip
```

> `--python` 默认就是开发环境的 `3.13`；内网是别的版本才需要加（可写多个：`3.12,3.13`）。

> 这条命令产出的包**默认同时带源码和开发依赖**，内网既能直接部署，
> 也能在源码上二次开发（见下面「步骤 4」）。只想要最小部署包就加 `--no-source --no-dev`。

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--python` | 目标机器的 Python 版本，可多个：`3.12,3.13`（默认 `3.13`，即开发环境；只有你的开发机/内网机器是别的版本时才要改） |
| `--platforms` | `linux-x64` / `linux-arm64` / `win-x64` / `win-arm64`，也可直接写 pip 平台标签（如 `manylinux_2_28_x86_64`） |
| `--with-extensions` | 顺便下载 DuckDB 的 `excel` / `json` 扩展，离线机器可直接用 |
| `--no-source` | **不带源码**。默认是带的（因为内网经常要二次开发）；只部署就加这个，包体积从 102MB 降到 54MB |
| `--no-dev` | 不带开发/构建依赖（setuptools / wheel / pip / pytest） |
| `--with-git` | 源码里连 `.git` 一起带（保留提交历史，包会变大） |
| `--zip` | 打成 `tar.gz` |

产出结构：

```
offline-bundle/
├── README.txt                  # 双份说明：怎么装、怎么二次开发
├── MANIFEST.txt                # 版本号 / 源码提交号 / 生成时间 / 完整 wheel 清单
├── install_offline.sh          # Linux/macOS 安装（只部署）
├── install_offline.ps1         # Windows 安装（只部署）
├── install_dev_offline.sh      # Linux/macOS 二次开发环境（可编辑安装 + 跑测试）
├── install_dev_offline.ps1     # Windows 二次开发环境
├── wheels/                     # 全部 .whl（多平台混放，pip 自动挑对的那个）
├── source/                     # 完整源码：src/ tests/ examples/ docs/ scripts/
│                               # + pyproject.toml + requirements*.txt
└── duckdb_extensions/          # DuckDB 扩展，目录结构镜像 ~/.duckdb/extensions
    └── v1.5.5/linux_amd64/excel.duckdb_extension
```

> `wheels/` 里可以混放多平台 wheel —— pip 会按当前解释器的标签自动筛选，
> 不会装错。这也是把多平台放一个目录的原因。

### 步骤 1′：让 GitHub 帮你打包（本地不用联网机器）

不想在本地装 Python / 耗流量下载 wheel，就把打包交给 CI ——
`.github/workflows/release.yml` 里的 `offline-bundle` 作业跑在 `ubuntu-latest` 上：

- **打标签自动出包**：push `v*` tag 时，除 Windows 两个 zip 之外，
  额外产出 `datacompare-<ver>-wheels-src-py<版本>-<平台>.tar.gz` 并挂到 GitHub Release；
- **随时手动出包**：Actions → **Build Release** → *Run workflow*，
  在输入框里填目标 Python 版本与平台（留空就用默认值），跑完在
  **Artifacts** 里下载 `datacompare-wheels-src-<ver>`（保留 30 天）。

Release 页面的说明里会附一张「三个文件怎么选」的表（内容来自仓库里的
`docs/RELEASE-ASSETS.md`），不用对着文件名猜。

| 输入 | 默认值 | 说明 |
| --- | --- | --- |
| `version` | 空 = 读 `pyproject.toml` | 手动触发时用来命名产物 |
| `python_versions` | `3.13` | 逗号分隔；只有内网开发机是别的 Python 版本时才需要改 |
| `platforms` | `linux-x64,win-x64` | `linux-x64` / `linux-arm64` / `win-x64` / `win-arm64`，也可写 pip 平台标签 |

为什么能在一个 Linux runner 上备齐 Windows 的 wheel：
`pip download --platform win_amd64` 只影响**挑哪个 wheel**，
不需要真的在 Windows 上跑（PyInstaller 才必须本平台构建）。

> ⚠️ CI 跑的是 `pip download`，**版本号取的是仓库里的源码**（含分支/标签指向的提交）。
> 如果本地有未提交的改动，CI 打出来的包**不会有那些改动** —— 那种情况就本地跑
> `build_offline_bundle.py`（它会连未提交的改动一起拷进 `source/`）。

### 步骤 2：拷进内网并安装

**Linux：**

```bash
# 本地打出来的叫 offline-bundle.tar.gz；CI 产出的名字形如
# datacompare-<ver>-wheels-src-py313-linux-win.tar.gz
tar -xzf offline-bundle.tar.gz
cd offline-bundle
bash install_offline.sh
```

脚本会：找一个 3.9+ 的解释器 → 建 `venv` → 用 `pip --no-index` 从本地 wheels 装 →
可选装 DuckDB 扩展 → 自检。可用环境变量覆盖：

```bash
PYTHON=python3.12 VENV_DIR=/opt/datacompare/venv bash install_offline.sh
```

**Windows（PowerShell）：**

```powershell
Expand-Archive offline-bundle.zip -DestinationPath .
cd offline-bundle
powershell -ExecutionPolicy Bypass -File install_offline.ps1
# 或者指定解释器与安装目录：
powershell -ExecutionPolicy Bypass -File install_offline.ps1 `
    -PythonExe "C:\Python312\python.exe" `
    -VenvDir "C:\datacompare\venv" `
    -InstallExtensions
```

### 步骤 3：验证

```bash
# Linux
./venv/bin/datacompare --version
# Windows
.\venv\Scripts\datacompare.exe --version
```

### 如果内网有 Nexus / Artifactory 之类的私有 PyPI

那就更简单，把 wheel 上传上去，目标机器直接：

```bash
pip install -i https://nexus.内网域名/repository/pypi/simple datacompare
```

### 步骤 4（可选）：内网要在源码上二次开发

打包时**不用加任何参数**——`--source` 和 `--dev` 默认就是开的，
所以标准那条制作命令产出的包已经带齐了下面这些东西：

| 需要什么 | 包里对应什么 |
| --- | --- |
| 源码 | `source/`（整个工作区快照：`src/` `tests/` `scripts/` `docs/` `examples/` + `pyproject.toml`） |
| 改完能直接跑 | `install_dev_offline.sh` / `.ps1` 做**可编辑安装**（`pip install -e`），改代码不用重装 |
| 跑测试 | `pytest` wheel 已在 `wheels/`，`requirements-dev.txt` 里也列了 |
| 离线重新打 wheel | `setuptools` / `wheel` wheel 已在 `wheels/` ——离线做可编辑安装时 pip 的 build isolation 会去装这两个包，**少一个都装不上** |
| 能对上版本 | `MANIFEST.txt` 里写了项目版本号 + 源码提交号（含未提交改动时会标出来） |

内网执行：

```bash
tar -xzf offline-bundle.tar.gz && cd offline-bundle
bash install_dev_offline.sh              # 建 venv-dev → 离线装依赖 → 可编辑装 source/
cd source
../venv-dev/bin/python -m pytest tests -q        # 应全部通过
../venv-dev/bin/datacompare compare -b 旧.csv -a 新.csv -k 单据号 -o out
```

Windows：

```powershell
Expand-Archive offline-bundle.zip -DestinationPath . ; cd offline-bundle
powershell -ExecutionPolicy Bypass -File install_dev_offline.ps1
cd source
..\venv-dev\Scripts\python.exe -m pytest tests -q
```

几个注意点：

1. **源码是「生成那一刻的工作区」**，不是 `git archive`，所以**未提交的改动也会带进去**
   （内网常见场景就是「先把改了一半的版本递进去」）。想要 git 历史就加 `--with-git`。
2. `.venv` / `dist` / `__pycache__` / `*.egg-info` / `compare_out` 这些产物目录不会被带走，
   内网重新建环境即可。
3. 内网加了**新的第三方依赖**时，本机联网重跑一次 `build_offline_bundle.py` 才能补齐 wheel，
   内网是变不出新 wheel 的（如果内网有私有 PyPI，也可以只把新 wheel 传上去）。
4. 安装脚本全程 `--no-index --find-links wheels`，一次网络请求都不会发；
   如果发现装得慢或者报连接超时，说明某个命令漏了 `--no-index`。

### 老 Python / 老 glibc 会装到较低版本的 DuckDB

这是**正常现象**，不是包做错了：老 glibc 上只剩老 wheel 带对应标签。

| 目标 | 装到的 DuckDB | 原因 |
| --- | --- | --- |
| Python 3.9 | 1.4.5 | DuckDB 从 1.5 起不再发 cp39 wheel |
| Python 3.10 ~ 3.13 | 1.5.5 | 最新版 |
| glibc < 2.28（CentOS 7 / RHEL 7 等） | 1.2.2 | 新 wheel 只带 `manylinux_2_28` 标签 |

都在 `duckdb>=1.1.0` 的允许范围内，且 **1.2.2 上跑过完整测试（51 个用例全绿）**，
所以对比结果一致，只是引擎旧一些。装的时候 pip 会自己挑，不用管。

正因为有这种差异，某些「Python × 平台」组合会**下不到任何 wheel**（例如
`3.9 + manylinux_2_28_x86_64`）—— 构建脚本会打警告并跳过，`MANIFEST.txt` 里记一笔；
但若某个 Python 版本在所有平台下都下不到，就直接报错，不会默默出一个装不上的包。

### 为什么离线包里的开发工具链是钉死版本的

见 `scripts/build_offline_bundle.py` 里的 `DEV_TOOLCHAIN`。两个坑都踩过：

1. `pip download --python-version` / `--platform` 只影响「选哪个 wheel」，
   **不按目标解释器评估环境标记** → `colorama`（Windows 上 pytest 要用）这类依赖
   会被漏掉，内网 `pip install -r requirements-dev.txt` 直接失败；
2. 同一个包在包里放了多个版本时，内网 pip 要 backtracking，实测会以
   `ERROR: Package 'setuptools' requires a different Python` 收场。

所以离线包里每个开发包**只留一个版本**。改这个列表后跑一下 CI 的 `test-offline`
作业（在容器里真离线装一遍再跑测试），本地开发机是验证不了这两类问题的。

> Windows 那个 `datacompare-<ver>-windows-x64-offline-kit.zip`（自带 Python 3.13 安装器、
> 目标机器不用装 Python）也带源码（CI 里传了 `-WithSource`），一样能二次开发，
> 只是它只覆盖 Python 3.13 + Windows；要覆盖多个 Python 版本就用跨平台的
> `datacompare-<ver>-wheels-src-py<版本>-<平台>.tar.gz`。

---

## 四、方案 B：免安装可执行程序（目标机器没有 Python）

### 重要限制

**PyInstaller 不支持交叉编译**：只能在 Windows 上打出 `.exe`，
在 Linux 上打出 Linux 可执行文件。要两个平台就得在两台机器上各打一次
（或者用各自的虚拟机/CI）。

### 打包

```bash
pip install pyinstaller
python scripts/build_exe.py --onedir --clean --with-extensions
```

产物：

| 模式 | 产物 | 说明 |
| --- | --- | --- |
| `--onedir`（推荐） | `dist/datacompare/`（约 130MB） | 整个目录拷走，启动 0.1s |
| 默认 `--onefile` | `dist/datacompare[.exe]` | 单文件，但每次启动要解压到临时目录，慢 1-3 秒 |

用法和 Python 版完全一样：

```bash
./dist/datacompare/datacompare compare -b 旧.csv -a 新.csv -k 单据号 -o out
```

**Linux 上要先装 `binutils`**，否则 PyInstaller 会报
`On Linux, objdump is required`（Debian/Ubuntu：`apt-get install -y binutils`；
RHEL/CentOS：`yum install -y binutils`）。

### Linux 整体包：在老一点的发行版里打（已实测）

可执行文件会绑定打包机的 glibc 版本，**在越老的系统上打，能跑的目标机越多**。
不想专门找一台老机器，就用容器。关键在于**日志走 stderr、只有 tar 流到 stdout**，
否则重定向出来的文件里全是日志（这个坑踩过：得到 45MB 的「tar.gz」，`tar` 报
`not in gzip format`）：

```bash
docker run --rm -v "$PWD":/src:ro python:3.13-slim-bullseye bash -c '
  set -e
  apt-get update -qq >&2 && apt-get install -y -qq --no-install-recommends binutils >&2
  cp -r /src /work && cd /work
  pip install -r requirements.txt pyinstaller >&2
  python scripts/build_exe.py --onedir --clean --with-extensions >&2
  ./dist/datacompare/datacompare --version >&2
  tar -C /work/dist -czf - datacompare
' > datacompare-linux-x64.tar.gz

tar -tzf datacompare-linux-x64.tar.gz | head    # 应该看到 datacompare/...
```

| 打包方式 | 产物要求的最低 glibc | 能跑在 |
| --- | --- | --- |
| `python:3.13-slim-bullseye`（Debian 11，已验证） | 2.31 | Debian 11+ / Ubuntu 20.04+ / RHEL 9+ |
| `python:3.13-slim-bookworm`（Debian 12） | 2.36 | Debian 12+ / Ubuntu 22.04+ |
| GitHub 的 `ubuntu-latest` | 2.39 | Ubuntu 24.04+ |

> **CentOS 7 / RHEL 7（glibc 2.17）跑不了上面任何一种**：DuckDB 新版的 wheel
> 要求 glibc ≥ 2.28，PyInstaller 打的包也一样。这种机器请改用方案 A
> （装个 Python 3.9+ 再离线装 wheel），或者在 `manylinux2014` 容器里
> `pip install "duckdb<1.3"` 后打包 —— 代价是引擎停在 1.2.2
> （已实测 51 个用例全绿，功能一致）。

Windows 那边不需要操心这些：CI 已经会产出
`datacompare-<ver>-windows-x64-standalone.zip`（解压即用，目标机器不用装 Python）。

### 关于 DuckDB 扩展

`excel` / `json` 扩展**不会**被打进可执行文件（它们是运行时下载的）。
离线环境有两种用法：

1. 打包时加 `--with-extensions`，扩展会放在 exe 同级的 `duckdb_extensions/` 下；
2. 或者手动把扩展文件拷到目标机器的扩展目录：

| 系统 | 扩展目录 |
| --- | --- |
| Linux | `~/.duckdb/extensions/<版本>/<平台>/` |
| Windows | `%USERPROFILE%\.duckdb\extensions\<版本>\<平台>\` |

平台目录名：`linux_amd64` / `linux_arm64` / `windows_amd64` / `windows_arm64`。

> **注意**：本工具当前**不依赖**任何 DuckDB 扩展也能完整跑通
> （Excel 报告走 openpyxl）。扩展只是给你事后自己写 SQL 时多几个函数可用。

---

## 五、Windows 部署补充

### 路径与编码

- 中文路径可以正常处理，但建议输出目录不要放在 `C:\Program Files\` 下（无写权限）
- 读取 GBK/GB18030 的老 CSV：`--encoding-before gbk`
- 输出的 CSV 带 UTF-8 BOM，Excel 双击直接打开不乱码

### 定时任务（示例：每天凌晨 2 点跑一次）

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\datacompare\venv\Scripts\datacompare.exe" `
    -Argument 'compare -c C:\datacompare\config.yaml --fail-on-diff' `
    -WorkingDirectory "C:\datacompare"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask -TaskName "datacompare-daily" -Action $action -Trigger $trigger -RunLevel Highest
```

`--fail-on-diff` 让退出码在发现实质差异时为 `2`，方便上层流程判断。

### 杀毒软件误报

PyInstaller 打的包偶尔会被误报。加白名单，或改用方案 A（纯 wheel + Python）。

---

## 六、Linux 部署补充

### 定时任务（cron）

```cron
0 2 * * * cd /opt/datacompare && /opt/datacompare/venv/bin/datacompare compare \
    -c /opt/datacompare/config.yaml --fail-on-diff >> /var/log/datacompare.log 2>&1
```

### systemd timer

```ini
# /etc/systemd/system/datacompare.service
[Unit]
Description=datacompare 数据集对比

[Service]
Type=oneshot
WorkingDirectory=/opt/datacompare
ExecStart=/opt/datacompare/venv/bin/datacompare compare -c /opt/datacompare/config.yaml
User=datacompare
```

```ini
# /etc/systemd/system/datacompare.timer
[Unit]
Description=每天跑一次数据集对比

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now datacompare.timer
```

### glibc 版本

`manylinux_2_28` 的 wheel 需要 glibc ≥ 2.28（CentOS 8+ / RHEL 8+ / Ubuntu 18.04+）。
老系统（CentOS 7，glibc 2.17）请用 `--platforms manylinux2014_x86_64` 制作安装包，
它会挑到兼容的 wheel 版本（注意此时装到的 DuckDB 版本可能偏低，
DuckDB 扩展的版本号要对得上）。

查看目标机器 glibc：

```bash
ldd --version | head -1
```

打**免安装可执行文件**时同样的道理（而且更严格：产物直接绑定打包机的 glibc），
见「四、方案 B」里的 glibc 对照表。

---

## 七、图形界面（可选）

命令行有 41 个参数、配置文件有 69 项，对不熟悉的人门槛太高。
所以内置了一个本机网页界面，**不引入任何额外依赖**——
用的是 Python 标准库的 `http.server`，页面是一个内嵌的单文件 HTML，
所以离线包里**不需要多带任何东西**，装了 wheel 包就能用。

### 启动

```bash
# 内网机器上（仅本机访问，最安全）
datacompare gui

# 指定端口；0 = 自动挑空闲端口
datacompare gui --port 8800

# 不自动弹浏览器（没有桌面环境、或走 SSH 时用）
datacompare gui --no-open-browser
```

启动后会在控制台打印访问地址，默认 `http://127.0.0.1:8765/`。

### 让内网同事也能用

```bash
datacompare gui --host 0.0.0.0 --port 8765
```

> ⚠️ **这个界面没有任何鉴权和加密，能访问到端口的人就能读服务器上的任何文件**
> （选文件的浏览接口和报告下载接口都是直接读本地路径的）。
> 所以只适合**可信内网**，绝对不要暴露到公网，也不要在有无关人员共用网络的环境里开 `0.0.0.0`。
> 需要多人共用就换成 `--host 127.0.0.1` 加 SSH 端口转发：
>
> ```bash
> # 同事在自己机器上执行，把远端 8765 映射到本地 8765
> ssh -L 8765:127.0.0.1:8765 user@内网服务器
> ```

### 在离线整包里会怎么表现

| 场景 | 行为 |
| --- | --- |
| wheel 包安装（方案 A） | 直接用，`datacompare gui` 即可 |
| PyInstaller 可执行程序（方案 B） | 同样支持，带 `--host` / `--port` / `--no-open-browser` |
| 无图形界面的服务器 | 用 `--no-open-browser`，自己去浏览器输地址 |
| 无浏览器（纯终端） | 别用 GUI，直接用 `compare` 子命令 |
| Windows 防火墙弹窗 | 只监听 `127.0.0.1` 时一般不会弹；改 `0.0.0.0` 会弹，选允许即可 |

### 和命令行的关系

界面上填的每一项都对应一个命令行参数／配置项：

- 点「导出配置」存成 YAML，然后 `datacompare compare -c 导出的.yaml` 就是完全一样的跑法；
- 调用的引擎、生成的报告格式、异常检测规则都**完全一致**，GUI 只是在外面套了一层表单；
- 输出目录里的文件（HTML / Excel / Markdown / CSV）和命令行跑出来的一模一样。

同一个界面**一次只允许跑一个任务**（避免多人同时点把内存打爆），
任务在后台线程跑，进度条和日志实时输出。关掉终端（Ctrl+C）服务就停了，不会常驻。

---

## 八、大文件场景的部署建议

1. **把 `temp_dir` 指到大容量真实磁盘**

   ```yaml
   temp_dir: /data/datacompare-tmp     # 别用 /tmp，它常常是内存盘
   memory_limit: 4GB
   work_db: /data/datacompare/work.duckdb
   ```

2. **只出需要的报告格式**：`--formats html` 比全出快得多

3. **明确指定主键** `-k 单据号`，省掉自动推断的一次全表扫描

4. **保留工作库** `--db work.duckdb`，之后可以随时用 DuckDB CLI 复查：

   ```sql
   SELECT * FROM v_diff WHERE status = 'VALUE_DIFF' LIMIT 100;
   SELECT column_name, count(*) FROM v_diff
   WHERE status IN ('VALUE_DIFF','NULL_MISMATCH','TYPE_MISMATCH')
   GROUP BY 1 ORDER BY 2 DESC;
   ```

5. 数据量特别大时，先按业务键拆成多批分别跑，比一次性全量更可控

---

## 九、常见问题

**Q：DuckDB 需要单独安装吗？**
A：**不需要。** 它就是一个 Python 包，跟着 `pip install` 一起进来，并且自带完整引擎。
本工具也**不依赖** DuckDB 命令行程序和任何 DuckDB 扩展。

实测确认（Linux）：

```
$ ldd .venv/lib/python3.13/site-packages/_duckdb.cpython-313-x86_64-linux-gnu.so
    libdl.so.2 / libpthread.so.0 / libstdc++.so.6 / libm.so.6 / libgcc_s.so.1 / libc.so.6
```

只有系统基础库，**没有 libduckdb 这样的外部依赖**；58MB 的引擎已经静态编译在
这个 `.so` 里。仓库里也搜不到 `subprocess` / `INSTALL` 调用，所以运行时既不会
调外部命令，也不会联网下载扩展。

三个概念的区别：

| | 要不要 | 说明 |
| --- | --- | --- |
| Python 包 `duckdb` | **要** | 在 `requirements.txt` 里，离线包里也有对应 wheel |
| DuckDB CLI | 不要 | 只有你自己想手工查库时才需要 |
| DuckDB 扩展 | 不要 | 本工具用不到；`--with-extensions` 只是给你留个方便 |

**Q：pip 报 `not a supported wheel on this platform`**
A：wheel 与「Python 版本 + 操作系统 + CPU 架构」三者绑定。
确认 `--python` 和 `--platforms` 与目标机器一致，重新制作安装包。

**Q：Windows 上提示 `python 不是内部或外部命令`**
A：方案 A 需要 Python。装一个 python.org 的官方版本并勾选 “Add to PATH”，
或者改用方案 B（免安装可执行程序）。

**Q：内网机器完全没网，`datacompare` 会不会偷偷联网？**
A：不会。运行时只依赖本地文件系统。唯一会联网的操作是 DuckDB 的
`INSTALL <扩展>`，本项目**不调用**它。如果你自己写了调用扩展的 SQL，
离线环境请用 `LOAD '/绝对路径/xxx.duckdb_extension'` 指定本地文件。

**Q：能不能做成 Docker 镜像带进内网？**
A：可以。外网机器上：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY wheels/ /wheels/
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt
COPY src/ /app/src/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "datacompare"]
```

然后 `docker save datacompare:latest -o datacompare.tar`，
内网 `docker load -i datacompare.tar`。

**Q：真的不考虑换成 Go / C# 吗？**
A：如果硬性要求「不能有 Python 运行时」，用方案 B 打包成原生可执行程序，
比换语言重写划算得多。只有在需要**把对比能力嵌进现有 Java/.NET 服务**时，
才值得考虑用对应语言的 DuckDB 绑定重写一层——那时复用的是同一套 SQL 逻辑，
这一层可以直接翻译 `src/datacompare/normalize.py` 里的表达式生成规则。
