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
3. 换语言的真实成本：约 2000 行 SQL 生成与配置逻辑要重写、34 个测试要重建，
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
| 包体积 | ~40MB（单平台） | ~130MB | 视基线而定 |
| 是否需要编译 | 否 | 否，但在目标系统上打 | 否 |
| 启动速度 | 快 | 快（目录模式 0.1s） | 快 |
| 依赖管理 | pip 离线装 | 全静态 | 镜像自带 |
| 适用场景 | 内网有 Python 环境 | 内网干净、不想装 Python | 已有容器/镜像体系 |
| 状态 | **已实测通过** | **已实测通过** | 视环境而定 |

下面重点讲 A 和 B。

---

## 三、方案 A：离线 wheel 包（推荐）

### 步骤 1：在能联网的机器上制作安装包

```bash
cd data-csv-compare
python scripts/build_offline_bundle.py \
    --out dist/offline-bundle \
    --python 3.12 \
    --platforms linux-x64,win-x64 \
    --with-extensions \
    --zip
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--python` | 目标机器的 Python 版本，可多个：`3.11,3.12` |
| `--platforms` | `linux-x64` / `linux-arm64` / `win-x64` / `win-arm64`，也可直接写 pip 平台标签（如 `manylinux_2_28_x86_64`） |
| `--with-extensions` | 顺便下载 DuckDB 的 `excel` / `json` 扩展，离线机器可直接用 |
| `--zip` | 打成 `tar.gz` |

产出结构：

```
offline-bundle/
├── README.txt
├── install_offline.sh          # Linux/macOS 安装
├── install_offline.ps1         # Windows 安装
├── wheels/                     # 全部 .whl（多平台混放，pip 自动挑对的那个）
└── duckdb_extensions/          # DuckDB 扩展，目录结构镜像 ~/.duckdb/extensions
    └── v1.5.5/linux_amd64/excel.duckdb_extension
```

> `wheels/` 里可以混放多平台 wheel —— pip 会按当前解释器的标签自动筛选，
> 不会装错。这也是把多平台放一个目录的原因。

### 步骤 2：拷进内网并安装

**Linux：**

```bash
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

---

## 七、大文件场景的部署建议

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

## 八、常见问题

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
