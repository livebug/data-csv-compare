# datacompare

前后数据集**全字段对比**工具：出一份对比报告，并把异常数据挑出来。

所有数据都落 **DuckDB**，所以大文件也能跑（走磁盘流式处理，不塞进内存）。

---

## 一、它能识别哪些「看上去不一样、其实一样」的情况

这是这个工具最核心的能力。它把单元格差异分成 5 类，**「仅格式差异」不算数据错误**：

| 状态 | 含义 | 例子 |
| --- | --- | --- |
| `EQUAL` 一致 | 原始文本完全一样 | `120.00` = `120.00` |
| `FORMAT_ONLY` 仅格式差异 | 写法不同、语义一致 | `1.50` vs `1.5`、`1,234.00` vs `1234`、`¥1 200` vs `1200`、`(300.5)` vs `-300.5`、`12%` vs `12.00%`、`2024-01-05` vs `2024/01/05`、`ＡＢＣ－１２３` vs `ABC-123`、` 苹果 ` vs `苹果` |
| `VALUE_DIFF` 值不同 | 真的变了 | `100` vs `120` |
| `NULL_MISMATCH` 空值不一致 | 一侧有值、一侧为空 | `加急` vs 空 |
| `TYPE_MISMATCH` 类型异常 | 数值/日期列混进了脏数据 | `待确认` vs `300`、`N/A` vs `100` |

数值归一化具体做了这些事：

- 去掉首尾和内部所有空白（含 `&nbsp;`、全角空格）
- 全角字符转半角
- 去掉货币符号 `¥ ￥ $ € £ ₹`
- 千分位分隔符（`1,234` → `1234`，可切换欧洲写法 `1.234,56`）
- 会计负数写法（`(300.50)` → `-300.50`）
- 各种 unicode 减号统一（`−` `–` `—`）
- 去百分号（`12%` → `12`），可配置成按比例 `12% → 0.12`
- 最后转成 DOUBLE 比较，**小数位数差异天然被吸收**

数值比较用「绝对容差 + 相对容差」，默认相对容差 `1e-9` 只用来吸收浮点噪声；
业务容差（比如金额允许 0.01）可以整体配，也可以一个字段一个字段配。

日期支持这些写法互认：`2024-01-05`、`2024/01/05`、`2024.01.05`、`20240105`、
`2024年1月5日`、`2024-01-05 08:30:00`、ISO 带 T 的写法等；
两侧都是纯日期时自动按「天」比较，避免 `00:00:00` 造成假差异。

**反过来的保护**：`001` 这类带前导零的值、以及整数位超过 15 位的长数字
（身份证号、银行卡号），会自动按**文本**比较，不会出现 `001 == 1`、
`1234567890123456789 ≈ 1234567890123456790` 这种误判。

---

## 二、安装

```bash
git clone <repo> && cd data-csv-compare
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # 运行依赖，只有 3 个
.venv/bin/pip install -r requirements-dev.txt      # 要改代码 / 跑测试再装这个
```

依赖只有三个：`duckdb`、`openpyxl`、`PyYAML`。开发环境用的是 Python 3.13。

也可以装成命令行工具：

```bash
.venv/bin/pip install -e .
datacompare --help
```

---

## 三、快速开始

```bash
# 最简：自动推断主键、自动判断字段类型、出全套报告
python -m datacompare compare -b 旧数据.csv -a 新数据.csv -o compare_out

# 指定主键（推荐，结果最确定）
python -m datacompare compare -b old.csv -a new.csv -k 单据号 -o out

# 复合主键
python -m datacompare compare -b old.csv -a new.csv -k "门店编码,业务日期" -o out

# 用配置文件
python -m datacompare compare -c config.example.yaml

# 生成配置模板
python -m datacompare init-config -o my.yaml

# 看看某个文件的字段画像（决定该怎么配）
python -m datacompare profile -f 旧数据.csv
```

需要 `PYTHONPATH=src` 时（未 `pip install -e .`）：

```bash
PYTHONPATH=src python -m datacompare compare -b a.csv -a b.csv -o out
```

### 图形界面（不想记参数就用这个）

```bash
python -m datacompare gui
# 或安装后：datacompare gui
```

会自动打开浏览器（默认 `http://127.0.0.1:8765/`），三步走完：

1. **选数据**：点「浏览」挑文件，自动认编码 / 分隔符 / 工作表，可先预览前几十行；
2. **确认字段与主键**：把两个文件的字段自动对齐，标出「仅左侧有 / 仅右侧有」的列，
   推断每个字段的类型（数值 / 日期 / 文本）并给主键建议，可手动改；
3. **跑对比**：实时进度条 + 日志，跑完直接点开 HTML 报告，报告就写在输出目录里。

```bash
# 端口被占用时换一个；0 = 自动挑空闲端口
datacompare gui --port 8800

# 让同一个内网里的同事也能访问（注意：无鉴权，只在可信内网用）
datacompare gui --host 0.0.0.0 --port 8765

# 不自动弹浏览器
datacompare gui --no-open-browser
```

界面上填的东西可以点「导出配置」存成 YAML，下次直接用 `-c` 跑，等价于命令行。
同一个界面一次只跑一个任务；关掉终端（Ctrl+C）服务就停了，不会常驻。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `-b/--before` `-a/--after` | 前（旧）/ 后（新）数据集 |
| `-k/--keys` | 主键列，逗号分隔 |
| `-c/--config` | 配置文件（YAML/JSON） |
| `-o/--out` | 报告输出目录 |
| `--db` | DuckDB 工作库路径（**指定后保留**，可事后写 SQL 复查） |
| `--formats` | `console,html,excel,markdown,csv` |
| `--delimiter` / `--delimiter-after` | 自定义分隔符（支持 `\t`） |
| `--encoding-before` / `--encoding-after` | 编码，如 `gbk`、`gb18030` |
| `--sheet-before` / `--sheet-after` | Excel 工作表 |
| `--no-header` | 首行不是表头 |
| `--tolerance` / `--abs-tolerance` | 相对 / 绝对容差 |
| `--case-insensitive` | 文本比较忽略大小写 |
| `--row-index` | 没有主键时按行号对比 |
| `--fail-on-diff` | 有实质差异时退出码 2（CI 卡口用） |
| `--memory-limit` / `--threads` | DuckDB 资源控制 |
| `--quiet` | 不打印控制台报告 |

---

## 四、输出

```
compare_out/
├── comparison_report.html    # 单文件网页报告：可筛选、可搜索、逐字符高亮差异
├── comparison_report.xlsx    # 多 Sheet：概览 / 字段汇总 / 差异明细 / 异常数据 / 单边数据 / 字段增减
├── comparison_report.md      # Markdown 版
└── diff_detail.csv           # 差异明细，方便丢给别的工具
```

控制台报告包含：总体结论 → 差异最多的字段 → 仅格式差异字段 → 异常数据 → 差异示例。

HTML 报告支持：状态筛选、字段筛选、全文搜索、任意字段排序、字符串级差异高亮。

---

## 五、行匹配是怎么做的

1. 主键来自 `keys` 配置；留空则自动推断（先找名字像主键且唯一的列，再找任意唯一列，最后试组合）。
2. 如果自动推断失败，可以 `--row-index` 退回按行号对齐，否则会明确报错让你指定主键。
3. 匹配规则是 **主键 + 出现序号**：同一主键出现多次时，第 1 条对第 1 条、第 2 条对第 2 条，
   多出来的算「单边行」——这样重复主键不会让结果集爆炸。
4. 主键重复会作为**严重异常**单独报出来，因为它意味着匹配结果不那么可靠。

---

## 六、异常数据检测清单

| 类别 | 说明 |
| --- | --- |
| 字段增减 | 后数据集多了/少了哪些列 |
| 行数变化 | 总行数增减及比例 |
| 主键重复 | 哪一侧有重复主键、重复了多少组、举例 |
| 主键无法匹配 | 只在前 / 只在后存在的行数及举例 |
| 空值率突变 | 某个字段空值率涨了/跌了超过阈值 |
| 数值列脏数据 | 数值列里有解析不了的值（`待确认`、`N/A` 等） |
| 日期列脏数据 | 日期列里有解析不了的值 |
| 数值离群点 | IQR 方法找极端值，给出正常区间和实际范围 |
| 分布漂移 | 均值/中位数大幅漂移 |
| 取值域变化 | 新增/消失的枚举值 |
| 字段退化为常量 | 原来有多个取值，后数据集只剩一个 |
| 仅格式差异汇总 | 多少单元格只是写法不同，并细分「数值补零/千分位」「大小写」「空白」 |

---

## 七、DuckDB：大文件与自查

不指定 `--db` 时，工具用临时库，跑完自动清理。
指定 `--db out/compare.duckdb` 后库会保留，可以用任何 DuckDB 客户端接着查：

```sql
-- 差异明细
SELECT * FROM v_diff WHERE status = 'VALUE_DIFF' LIMIT 100;

-- 只看实质差异最多的字段
SELECT column_name, count(*) FROM v_diff
WHERE status IN ('VALUE_DIFF','NULL_MISMATCH','TYPE_MISMATCH')
GROUP BY 1 ORDER BY 2 DESC;

-- 行级结论
SELECT * FROM v_row WHERE row_status <> 'SAME';

-- 自己对比某个字段的分布
SELECT 金额 FROM before_ranked ORDER BY 金额 DESC LIMIT 20;
```

保留的表 / 视图：

| 名称 | 内容 |
| --- | --- |
| `dc_before` / `dc_after` | 原始数据（全 VARCHAR，带 `__row_id`） |
| `before_ranked` / `after_ranked` | 带 `__key`、`__rank` 的匹配准备表 |
| `pairs` | 行配对关系 |
| `paired_norm` | 配对后每个单元格的归一化结果（原始值 + 归一化值 + 空值标记） |
| `diff_detail` | 逐字段差异明细（只存有差异的格子） |
| `row_status` | 行级结论：SAME / CHANGED / ONLY_IN_BEFORE / ONLY_IN_AFTER |
| `v_row` / `v_diff` | 带主键值的视图，日常查这两个就够 |

大文件相关的行为：CSV 由 DuckDB 直接流式读取，不经过 Python 内存；
超出 `memory_limit` 会溢写到 `temp_dir`；`SET preserve_insertion_order=false` 降低内存占用。

### 性能参考（实测）

6 万行 × 20 列，**每个单元格都不同**的极端场景：

| 环节 | 耗时 | 执行者 |
| --- | --- | --- |
| 读取 + 字段画像 + 主键匹配 + 逐字段比对 + 异常检测 | 21.9s | DuckDB（C++） |
| 生成全部 SQL 文本（Python 侧全部逻辑） | < 0.2s | Python |
| HTML 报告 | 0.36s | Python |
| CSV 明细（104 万行） | 0.57s | DuckDB（C++ COPY） |
| Excel 报告（5 万行明细） | 9.3s | Python（openpyxl 流式） |
| **合计** | **约 32s** | |

真实场景（6 万行里只有 30 处差异）：引擎 18.0s + 报告 0.2s = **18.2s**。

关键结论：**计算量 99% 在 DuckDB 里**，Python 只负责把规则翻译成 SQL
和渲染报告。所以换编程语言不会让计算变快（各语言的 DuckDB 绑定是同一个
C++ 引擎）。详见 `docs/DEPLOY.md`。

几个调优点：

- 大文件把 `temp_dir` 指到大容量**真实磁盘**（`/tmp` 常常是内存盘）
- 只出需要的报告格式（`--formats html`）
- 明确指定 `keys` 可以省掉主键自动推断的开销
- 表很宽且确定字段类型时，用 `compare.detect_types: false` 省掉字段画像
- 差异量极大时调小 `report.excel_max_rows` / `report.html_max_rows`

---

## 八、支持的输入

| 类型 | 说明 |
| --- | --- |
| CSV / 文本 | 自动嗅探分隔符，也支持显式指定；支持 `\r\n`、BOM |
| 自定义编码 | `gbk` / `gb18030` / `utf-16` 等先转码成 UTF-8 再读 |
| Excel | `.xlsx` / `.xlsm`，可选工作表；数字/日期按单元格实际值还原成文本 |
| Parquet | 支持通配符匹配多个文件 |
| JSON | 支持 `.json` / `.jsonl`，嵌套结构转成文本 |

> 老版 `.xls` 不支持，请另存为 `.xlsx` 或导出 CSV。
> 两侧可以是不同格式（比如前是 CSV、后是 Excel），只要字段名对得上。

---

## 九、离线内网部署

内网没有外网访问是常态，这里准备了两条路（都已在干净环境实测通过）：

| | 方案 A：离线 wheel 包 | 方案 B：免安装可执行程序 |
| --- | --- | --- |
| 目标机器要有 Python | 要（3.9+） | **不要** |
| 包体积 | 默认（3.13 / linux+win / 含源码与开发依赖）102MB，tar.gz 72MB；加 `--no-source --no-dev` 降到 54MB，只做一个平台再减一半 | 约 130MB，tar.gz 45MB |
| 制作命令 | `python scripts/build_offline_bundle.py --with-extensions --zip` | `python scripts/build_exe.py --onedir --clean --with-extensions` |

**方案 A**：在能联网的机器上把 wheel 全部下载下来，整包拷进内网，
一条命令装好（`pip --no-index`，全程不联网，支持 Windows 和 Linux）：

```bash
python scripts/build_offline_bundle.py --out dist/offline \
    --platforms win-x64,linux-x64 --with-extensions --zip   # --python 默认 3.13
# 内网机器上：
bash install_offline.sh            # Linux：装上就能跑
powershell -File install_offline.ps1   # Windows：同上
```

**内网还要二次开发**（改代码、加参数、跑测试）就再多走一步：
上面这条命令**默认就把源码和开发依赖一起带上了**（`source/` +
`requirements-dev.txt` 里的 setuptools/wheel/pip/pytest），内网里执行

```bash
bash install_dev_offline.sh        # 可编辑安装：改 source/ 下的代码立刻生效
cd source && ../venv-dev/bin/python -m pytest tests -q
```

包内还带 `MANIFEST.txt`（版本号、源码提交号、wheel 清单、生成时间），
内网收到包先看一眼就知道是哪一版。只要 wheel 不要源码就加 `--no-source --no-dev`。

**不想本地打包就让 GitHub 打**：Actions → **Build Release** → *Run workflow*，
填目标 Python 版本（默认 `3.13`，即开发环境）和平台（默认 `linux-x64,win-x64`），
跑完在 Artifacts 里下载 `datacompare-offline-bundle-<ver>`；
打 `v*` tag 发版时会自动把同一个包挂到 GitHub Release 上。
跨平台 wheel 全部在 Linux runner 上就能备齐，不需要两台机器。

**方案 B**：打包成原生可执行程序，目标机器可以完全没有 Python。
注意 PyInstaller 不支持交叉编译，要在目标系统上各打一次；Windows 的包 CI 会直接产出，
Linux 上在一个 glibc 较老的容器里打最稳（已实测的命令见 `docs/DEPLOY.md` 方案 B）。

完整步骤、Docker 镜像做法（不想用 wheel 也不想用可执行文件的话）、定时任务配置、
glibc 版本兼容性、杀软误报处理等，见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。

---

## 十、项目结构

```
src/datacompare/
├── config.py       配置模型（dataclass + YAML 加载）
├── sources.py      数据接入：CSV / 文本 / Excel / Parquet / JSON → DuckDB
├── normalize.py    ★ 核心：把「归一化 + 差异判定」翻译成 DuckDB SQL
├── profile.py      字段画像 + 主键自动推断
├── engine.py       对比引擎：主键匹配 → 逐字段对比 → 差异表
├── anomaly.py      异常数据检测
├── models.py       结果数据模型（状态、统计、异常记录）
├── sqlutil.py      SQL 字面量安全拼接
├── cli.py          命令行入口
├── gui.py          本机 Web 图形界面（标准库 http.server，无额外依赖）
└── report/         控制台 / Markdown / HTML / Excel / CSV 报告

scripts/
├── build_offline_bundle.py   制作离线交付包（wheel + 源码，联网机器上跑）
├── build_windows_release.ps1 Windows 一键发布（exe 包 + 离线包）
├── extract_release_notes.py  从 CHANGELOG 抽取 Release 说明（CI 用）
├── install_offline.sh        Linux/macOS 离线安装（只部署）
├── install_offline.ps1       Windows 离线安装（只部署）
├── install_dev_offline.sh    Linux/macOS 离线二次开发环境
├── install_dev_offline.ps1   Windows 离线二次开发环境
└── build_exe.py              打包成免安装可执行程序

docs/DEPLOY.md                离线内网部署方案（Windows / Linux）
```

开发依赖单独放在 `requirements-dev.txt`（含构建后端 setuptools/wheel），
生产部署只认 `requirements.txt`。离线做可编辑安装时 pip 的 build isolation
也要联网找 setuptools/wheel，所以这两个必须一起进离线包——`requirements-dev.txt`
里列了，打包脚本会自动带上。

设计上只有一处关键取舍：**归一化和对比全部在 SQL 里做**。
这样数据不用搬到 Python，DuckDB 可以对磁盘上的大表流式跑完；
Python 只负责把规则翻译成 SQL。

图形界面刻意只用标准库的 `http.server`，不引入 Flask/FastAPI 之类依赖，
这样离线包里不需要多带任何东西；页面是一个内嵌的单文件 HTML，也不需要前端构建。

---

## 十一、开发与测试

```bash
.venv/bin/pip install -r requirements-dev.txt      # 开发依赖（含 pytest）
.venv/bin/python -m pytest tests -q                # 51 个用例
.venv/bin/python -m pyflakes src tests scripts     # 静态检查

# 生成示例数据（故意埋了各类差异）并跑一遍
.venv/bin/python examples/make_sample.py
.venv/bin/python -m datacompare compare \
    -b examples/before.csv -a examples/after.csv -o examples/out
```

CI（`.github/workflows/release.yml`）在发版时跑四个作业：`test`（ubuntu 门禁）、
`test-offline`（容器里真离线装一遍再跑测试）、`build`（Windows 上出 exe 包）、
`offline-bundle`（出跨平台离线包）；后面两个都依赖前两个。

---

## 十二、常见问题

**Q：需要单独安装 DuckDB 吗？**
A：**不需要。** `duckdb` 就是一个普通的 Python 包，`pip install duckdb` 装完即可，
它自带完整的 DuckDB 引擎（Linux 下是 58MB 的 `_duckdb.cpython-*.so`，只依赖 libc /
libstdc++ 这些系统基础库，没有外部的 `libduckdb` 需要另装）。

需要区分三个概念：

| | 要不要装 | 说明 |
| --- | --- | --- |
| **Python 包 `duckdb`** | **要**（已写进 `requirements.txt`） | 自带完整引擎，本工具的全部计算都靠它 |
| DuckDB CLI（`duckdb` 命令） | 不要 | 工具不调用任何外部进程，纯 Python 进程内使用。只有你想像 `sqlite3` 那样手工查库时才需要另外装一个单文件二进制 |
| DuckDB 扩展（`excel` / `json` 等） | 不要 | 本工具不依赖任何扩展，Excel 报告走 openpyxl。离线环境如需扩展见 `docs/DEPLOY.md` |

**Q：为什么我的数值列被当成文本比了？**
A：三种可能——① 列里有带前导零的值（`001`）；② 整数位超过 15 位；
③ 多数值都不是数字。用 `datacompare profile -f 文件` 看「可数值化」比例，
然后在配置里用 `columns: [{name: 列名, type: numeric}]` 强制指定。

**Q：两侧列名不一样怎么办？**
A：用 `column_map: {前侧列名: 后侧列名}`，或在 `columns` 里写 `after_name`。

**Q：我只想看真正的数据错误，不想被格式差异干扰。**
A：默认就是这样——`FORMAT_ONLY` 不计入「实质差异」，`consistent` 判定也只算实质差异。
用 `--fail-on-diff` 时同样只看实质差异。

**Q：图形界面要不要装别的东西？端口冲突怎么办？**
A：不用装任何东西，就是标准库的 `http.server`，只有一个 Python 进程。
默认只监听 `127.0.0.1`，别人访问不到。端口冲突就 `--port 0` 让它自己挑一个空闲端口。
界面上跑的任务和命令行完全一样（同一套引擎、同一套报告），
「导出配置」导出的 YAML 可以直接用 `datacompare compare -c` 复现。

**Q：数据里有大段文本，报告会不会很大？**
A：用 `report.max_cell_chars` 截断（默认 200），`html_max_rows` 限制内嵌行数，
超出部分留在 DuckDB 里。

**Q：能对比两个不同格式的文件吗？**
A：可以，比如前是 CSV、后是 Excel，只要字段名对得上。
