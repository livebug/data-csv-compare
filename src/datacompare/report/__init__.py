"""报告输出：统一入口。"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

from ..config import Config
from ..models import CompareResult


def write_reports(
    result: CompareResult,
    cfg: Config,
    out_dir: Optional[str] = None,
    formats: Optional[Sequence[str]] = None,
    log=None,
) -> List[str]:
    """按配置生成报告，返回生成的文件路径列表。"""
    log = log or (lambda msg: None)
    out_dir = out_dir or cfg.output_dir
    formats = list(formats if formats is not None else cfg.report.formats)
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []

    for fmt in formats:
        fmt = (fmt or "").strip().lower()
        if not fmt:
            continue
        try:
            path = _write_one(result, cfg, out_dir, fmt, log)
        except Exception as exc:  # 单个报告失败不影响其它报告
            log(f"生成 {fmt} 报告失败：{type(exc).__name__}: {exc}")
            continue
        if path:
            written.append(path)
    return written


def _write_one(result, cfg: Config, out_dir: str, fmt: str, log):
    if fmt in ("console", "stdout", "text"):
        from .console import render_console

        log(render_console(result, cfg))
        return None
    if fmt in ("md", "markdown"):
        from .markdown import write_markdown

        path = os.path.join(out_dir, "comparison_report.md")
        write_markdown(result, cfg, path)
        return path
    if fmt in ("html", "htm"):
        from .html import write_html

        path = os.path.join(out_dir, "comparison_report.html")
        write_html(result, cfg, path)
        return path
    if fmt in ("excel", "xlsx"):
        from .excel import write_excel

        path = os.path.join(out_dir, "comparison_report.xlsx")
        write_excel(result, cfg, path)
        return path
    if fmt in ("csv", "diffs"):
        from .csv_out import write_diff_csv

        path = os.path.join(out_dir, "diff_detail.csv")
        write_diff_csv(result, cfg, path)
        return path
    raise ValueError(
        f"不支持的报告格式：{fmt}（可选：console / html / excel / markdown / csv）"
    )
