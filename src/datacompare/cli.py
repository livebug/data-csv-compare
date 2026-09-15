"""命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .config import ColumnRule, Config, dump_config, load_config


def _bool_flag(parser, name: str, default: Optional[bool], help_on: str, help_off: str):
    dest = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true", default=default, help=help_on)
    group.add_argument(f"--no-{name}", dest=dest, action="store_false", help=help_off)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datacompare",
        description="前后数据集全字段对比工具：出对比报告 + 找异常数据（大文件走 DuckDB）",
    )
    parser.add_argument("--version", action="version", version=f"datacompare {__version__}")
    sub = parser.add_subparsers(dest="command")

    # ---------------- compare ----------------
    p = sub.add_parser("compare", help="对比两个数据集")
    p.add_argument("--before", "-b", required=False, help="前（旧）数据集路径")
    p.add_argument("--after", "-a", required=False, help="后（新）数据集路径")
    p.add_argument("--config", "-c", help="YAML/JSON 配置文件路径")
    p.add_argument("--keys", "-k", help="主键列，多个用逗号分隔，例如 id 或 门店,日期")
    p.add_argument("--out", "-o", help="报告输出目录")
    p.add_argument("--db", help="DuckDB 工作库路径（指定后会保留，可用 SQL 复查）")
    p.add_argument("--formats", help="报告格式，逗号分隔：console,html,excel,markdown,csv")
    p.add_argument("--html", dest="html", action="store_true", default=None, help="只出 HTML 报告")
    p.add_argument("--excel", dest="excel", action="store_true", default=None, help="只出 Excel 报告")

    p.add_argument("--delimiter", help="两侧通用分隔符，例如 ',' 或 '\\t'（默认自动嗅探）")
    p.add_argument("--delimiter-before", help="前数据集分隔符")
    p.add_argument("--delimiter-after", help="后数据集分隔符")
    p.add_argument("--encoding", help="两侧通用编码，例如 utf-8 / gbk / gb18030")
    p.add_argument("--encoding-before", help="前数据集编码")
    p.add_argument("--encoding-after", help="后数据集编码")
    p.add_argument("--sheet-before", help="前数据集 Excel 工作表名或序号")
    p.add_argument("--sheet-after", help="后数据集 Excel 工作表名或序号")
    p.add_argument("--no-header", dest="header", action="store_false", default=None,
                   help="文件首行不是表头")

    p.add_argument("--tolerance", type=float, help="相对容差，例如 0.0001")
    p.add_argument("--abs-tolerance", type=float, help="绝对容差")
    _bool_flag(p, "case-insensitive", None, "文本比较忽略大小写", "文本比较区分大小写")
    _bool_flag(p, "ignore-whitespace", None, "文本比较忽略所有空白", "文本比较保留空白语义")
    _bool_flag(p, "detect-types", None, "自动推断字段类型（默认开）", "全部按文本严格比较")
    _bool_flag(p, "auto-key", None, "自动推断主键（默认开）", "不自动推断主键")
    _bool_flag(p, "row-index", None, "没有主键时按行号对比", "没有主键时报错")
    _bool_flag(p, "anomaly", None, "开启异常检测（默认开）", "关闭异常检测")
    _bool_flag(p, "fail-on-diff", None,
               "存在实质差异时退出码为 2（便于 CI 卡口）", "任何情况都返回 0")
    p.add_argument("--treat-format-only-as-diff", action="store_true", default=None,
                   help="把「仅格式差异」也视为差异（默认不视为差异）")
    p.add_argument("--memory-limit", help="DuckDB 内存上限，例如 2GB")
    p.add_argument("--threads", type=int, help="DuckDB 线程数")
    p.add_argument("--top-n", type=int, help="报告里展示的差异条数")
    p.add_argument("--max-rows", type=int, help="HTML 报告内嵌的差异行数上限")
    p.add_argument("--quiet", "-q", action="store_true", help="不打印控制台报告")
    p.add_argument("--keep-intermediate", action="store_true", default=None,
                   help="保留中间表（便于排查，占用更多空间）")

    # ---------------- profile ----------------
    q = sub.add_parser("profile", help="输出单个文件的字段画像")
    q.add_argument("--file", "-f", required=True, help="数据文件路径")
    q.add_argument("--delimiter", help="分隔符")
    q.add_argument("--encoding", help="编码")
    q.add_argument("--sheet", help="Excel 工作表名或序号")
    q.add_argument("--config", "-c", help="配置文件路径")
    q.add_argument("--rows", type=int, default=20, help="每个字段展示的样例值个数")

    # ---------------- init-config ----------------
    r = sub.add_parser("init-config", help="生成一个配置文件模板")
    r.add_argument("--out", "-o", default="datacompare.yaml", help="输出路径")
    r.add_argument("--force", action="store_true", help="覆盖已存在的文件")

    # ---------------- gui ----------------
    g = sub.add_parser("gui", help="启动本机 Web 界面（适合不熟悉命令行的同事）")
    g.add_argument("--host", default="127.0.0.1",
                   help="监听地址，默认仅本机；要让同事访问用 0.0.0.0")
    g.add_argument("--port", type=int, default=8765, help="端口，0 表示自动选空端口")
    g.add_argument("--dir", dest="work_dir", help="工作目录（默认当前目录）")
    _bool_flag(g, "open-browser", True, "启动后自动打开浏览器（默认）", "不自动打开浏览器")

    return parser


# --------------------------------------------------------------------------
# 配置装配
# --------------------------------------------------------------------------
def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def build_config(args) -> Config:
    cfg = load_config(args.config) if getattr(args, "config", None) else Config()

    if getattr(args, "before", None):
        cfg.before.path = args.before
    if getattr(args, "after", None):
        cfg.after.path = args.after

    if not cfg.before.path or not cfg.after.path:
        raise SystemExit("缺少 --before / --after（或配置文件里的 before / after）")

    for side, spec in (("before", cfg.before), ("after", cfg.after)):
        delim = getattr(args, f"delimiter_{side}", None) or getattr(args, "delimiter", None)
        if delim is not None:
            spec.delimiter = _unescape(delim)
        enc = getattr(args, f"encoding_{side}", None) or getattr(args, "encoding", None)
        if enc:
            spec.encoding = enc
        sheet = getattr(args, f"sheet_{side}", None)
        if sheet is not None:
            spec.sheet = _to_int(sheet)
        if getattr(args, "header", None) is not None:
            spec.header = args.header

    if getattr(args, "keys", None):
        cfg.keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    if getattr(args, "tolerance", None) is not None:
        cfg.compare.numeric.rel_tol = args.tolerance
    if getattr(args, "abs_tolerance", None) is not None:
        cfg.compare.numeric.abs_tol = args.abs_tolerance

    for name in ("case_insensitive", "ignore_whitespace", "detect_types", "auto_key",
                 "row_index", "anomaly", "keep_intermediate"):
        value = getattr(args, name, None)
        if value is None:
            continue
        if name == "case_insensitive":
            cfg.compare.string.case_insensitive = value
        elif name == "ignore_whitespace":
            cfg.compare.string.ignore_whitespace = value
        elif name == "detect_types":
            cfg.compare.detect_types = value
        elif name == "auto_key":
            cfg.key_options.auto_detect = value
        elif name == "row_index":
            cfg.key_options.use_row_index_if_no_key = value
        elif name == "anomaly":
            cfg.anomaly.enabled = value
        elif name == "keep_intermediate":
            cfg.keep_intermediate = value

    if getattr(args, "treat_format_only_as_diff", None):
        cfg.report.treat_format_only_as_diff = True

    if getattr(args, "out", None):
        cfg.output_dir = args.out
    if getattr(args, "db", None):
        cfg.work_db = args.db
    if getattr(args, "memory_limit", None):
        cfg.memory_limit = args.memory_limit
    if getattr(args, "threads", None):
        cfg.threads = args.threads
    if getattr(args, "top_n", None):
        cfg.report.top_n = args.top_n
    if getattr(args, "max_rows", None):
        cfg.report.html_max_rows = args.max_rows

    if getattr(args, "formats", None):
        cfg.report.formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    elif getattr(args, "html", None):
        cfg.report.formats = ["html"]
    elif getattr(args, "excel", None):
        cfg.report.formats = ["excel"]

    return cfg


def _unescape(text: str) -> str:
    return text.replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r")


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_compare(args) -> int:
    from .engine import CompareEngine
    from .report import write_reports

    cfg = build_config(args)
    verbose = not args.quiet
    log = (lambda msg: print(f"[datacompare] {msg}", file=sys.stderr)) if verbose else (lambda m: None)

    engine = CompareEngine(cfg, log=log)
    try:
        result = engine.run()
        written = write_reports(result, cfg, log=log if not args.quiet else (lambda m: None))
        for path in written:
            print(f"[datacompare] 已生成报告：{os.path.abspath(path)}", file=sys.stderr)
        if cfg.anomaly.enabled and result.anomalies:
            errors = [a for a in result.anomalies if a.level == "error"]
            if errors and verbose:
                print(
                    f"[datacompare] 发现 {len(errors)} 项严重异常，详见报告",
                    file=sys.stderr,
                )

        if getattr(args, "fail_on_diff", None) and not result.stats.consistent:
            return 2
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    finally:
        keep_db = bool(cfg.work_db)
        engine.close(cleanup=not keep_db)


def cmd_profile(args) -> int:
    from .config import SourceSpec
    from .engine import CompareEngine
    from .profile import detect_keys

    cfg = load_config(args.config) if args.config else Config()
    spec = SourceSpec(path=args.file)
    if args.delimiter:
        spec.delimiter = _unescape(args.delimiter)
    if args.encoding:
        spec.encoding = args.encoding
    if args.sheet:
        spec.sheet = _to_int(args.sheet)
    cfg.before = spec
    cfg.after = SourceSpec(path=args.file)
    cfg.anomaly.enabled = False

    engine = CompareEngine(cfg, log=lambda m: None)
    engine.cfg = cfg
    con = engine._open()
    try:
        from .sources import load_source

        loaded = load_source(con, spec, "dc_probe", engine._ensure_temp_dir())
        engine._temp_files.extend(loaded.temp_paths)
        print(f"文件：{args.file}")
        print(f"格式：{loaded.kind}    行数：{loaded.row_count:,}    列数：{loaded.ncols}")
        for w in loaded.warnings:
            print(f"  提示：{w}")
        print()

        from .profile import profile_table
        from .report.console import render_table

        profiles, _ = profile_table(con, "dc_probe", loaded.columns, cfg)
        rows = []
        for col in loaded.columns:
            p = profiles[col]
            non_null = (p.non_null / p.total) if p.total else 0.0
            num_try = p.numeric_ok + p.numeric_fail
            date_try = p.date_ok + p.date_fail
            rows.append([
                col,
                f"{non_null:.1%}",
                f"{p.distinct_vals:,}",
                f"{(p.numeric_ok / num_try if num_try else 0):.1%}",
                f"{(p.date_ok / date_try if date_try else 0):.1%}",
                f"{p.leading_zero:,}",
                f"{p.max_digits}",
                ", ".join(p.samples[:2]),
            ])
        print(
            render_table(
                ["列名", "非空率", "取值数", "可数值化", "可日期化", "前导零", "最长数字", "样例"],
                rows,
                aligns=["left", "right", "right", "right", "right", "right", "right", "left"],
                max_widths=[24, 8, 10, 10, 10, 8, 9, 34],
            )
        )
        print()
        keys, strategy, warnings = detect_keys(con, "dc_probe", loaded.columns, profiles, cfg)
        if keys:
            print(f"建议主键：{', '.join(keys)}    （{strategy}）")
        else:
            print("未能自动识别主键，请用 --keys 指定")
        for w in warnings:
            print(f"  提示：{w}")
        return 0
    finally:
        engine.close()


def cmd_init_config(args) -> int:
    if os.path.exists(args.out) and not args.force:
        print(f"文件已存在：{args.out}（加 --force 覆盖）", file=sys.stderr)
        return 1
    cfg = Config()
    cfg.before.path = "data/before.csv"
    cfg.after.path = "data/after.csv"
    cfg.keys = []
    cfg.columns = [
        ColumnRule(name="金额", type="numeric", abs_tol=0.01),
        ColumnRule(name="备注", ignore=True),
    ]
    text = _CONFIG_HEADER + dump_config(cfg)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"已生成配置模板：{args.out}")
    return 0


def cmd_gui(args) -> int:
    from .gui import run_gui

    return run_gui(
        host=args.host,
        port=args.port,
        open_browser=bool(args.open_browser),
        work_dir=args.work_dir,
    )


_CONFIG_HEADER = """# datacompare 配置文件
# 所有项都有默认值，只写需要覆盖的部分即可。
#
# 关键配置说明：
#   keys                行匹配主键；留空会自动推断（找唯一且非空的列）
#   compare.numeric     数值比较：rel_tol / abs_tol 控制容差
#                       —— 1.50 与 1.5、1234.00 与 1,234 默认会被识别为「仅格式差异」
#   compare.string      文本比较：trim / 折叠空白 / 大小写 / 全角转半角 / 空值口径
#   columns             针对单个字段的覆盖规则（类型、容差、忽略、脱敏）
#
"""


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "compare": cmd_compare,
        "profile": cmd_profile,
        "init-config": cmd_init_config,
        "gui": cmd_gui,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        parser.print_help()
        return 1

    try:
        return handler(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
