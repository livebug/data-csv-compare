"""控制台报告。"""

from __future__ import annotations

import shutil
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..models import (
    LEVEL_LABELS,
    STATUS_LABELS,
    TYPE_LABELS,
)
from . import data as D

_WIDTH_CACHE: Dict[str, int] = {}


def _char_width(ch: str) -> int:
    cached = _WIDTH_CACHE.get(ch)
    if cached is not None:
        return cached
    width = 0 if unicodedata.combining(ch) else (
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    )
    _WIDTH_CACHE[ch] = width
    return width


def disp_width(text: Any) -> int:
    return sum(_char_width(ch) for ch in str(text))


def truncate(text: Any, width: int) -> str:
    text = "" if text is None else str(text)
    if width <= 0:
        return ""
    if disp_width(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: Any, width: int, align: str = "left") -> str:
    text = truncate(text, width)
    gap = width - disp_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    aligns: Optional[Sequence[str]] = None,
    max_widths: Optional[Sequence[int]] = None,
) -> str:
    if not rows:
        return "  （无）"
    aligns = list(aligns or ["left"] * len(headers))
    widths = []
    for i, head in enumerate(headers):
        w = disp_width(head)
        for row in rows:
            cell = row[i] if i < len(row) else ""
            w = max(w, disp_width(cell))
        if max_widths and max_widths[i]:
            w = min(w, max_widths[i])
        widths.append(w)

    lines = []
    sep = "  "
    lines.append(sep.join(pad(h, widths[i], "center") for i, h in enumerate(headers)))
    lines.append(sep.join("─" * w for w in widths))
    last = len(headers) - 1
    for row in rows:
        cells = []
        for i in range(len(headers)):
            value = row[i] if i < len(row) else ""
            if isinstance(value, float):
                if value != value:
                    value = "-"
                elif headers[i].endswith("率"):
                    value = f"{value:.2%}"
                elif abs(value) >= 1000:
                    value = f"{value:,.2f}"
                else:
                    value = f"{value:g}"
            # 最后一列不补齐，避免输出大量行尾空格
            cells.append(value if i == last else pad(value, widths[i], aligns[i]))
        lines.append(sep.join(str(c) for c in cells))
    return "\n".join(lines)


def _term_width(minimum: int = 64, maximum: int = 78) -> int:
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except Exception:  # pragma: no cover
        width = 80
    return max(minimum, min(maximum, width - 2))


def render_console(result, cfg: Config) -> str:
    ctx = D.build_context(result, cfg)
    stats = ctx["stats"]
    out: List[str] = []
    bar = "═" * _term_width()

    out.append(bar)
    out.append(f"  {ctx['title']}")
    out.append(bar)
    out.append(f"  前数据集 : {stats.before_path}")
    out.append(f"            {stats.before_rows:,} 行 × {len(stats.before_columns)} 列")
    out.append(f"  后数据集 : {stats.after_path}")
    out.append(f"            {stats.after_rows:,} 行 × {len(stats.after_columns)} 列")
    if stats.key_columns:
        out.append(f"  主键     : {', '.join(stats.key_columns)}   （{stats.key_strategy}）")
    else:
        out.append(f"  行匹配   : {stats.key_strategy}")

    # -- 总体结论 ------------------------------------------------------
    out.append("")
    out.append("【总体结论】")
    rows = [
        ["总行数", f"{stats.before_rows:,}", f"{stats.after_rows:,}",
         f"{stats.row_delta:+,}（{stats.row_delta_rate:+.2%}）"],
        ["匹配成功的行", "", f"{stats.matched_pairs:,}", ""],
        ["  ├ 完全一致", "", f"{stats.same_rows:,}", ""],
        ["  └ 存在差异", "", f"{stats.changed_rows:,}", ""],
        ["仅前数据集存在", "", f"{stats.only_in_before:,}", ""],
        ["仅后数据集存在", "", f"{stats.only_in_after:,}", ""],
        ["单元格差异", "", f"{stats.cell_diffs:,}", ""],
        ["  ├ 实质差异", "", f"{stats.severe_cell_diffs:,}", "值不同/空值不等/类型异常"],
        ["  └ 仅格式差异", "", f"{stats.format_only_cells:,}", "补零、千分位、大小写、空白等"],
        ["参与对比字段", "", f"{stats.compared_columns:,}", f"共 {stats.total_columns} 列"],
    ]
    out.append(render_table(["项目", "前", "后", "备注"], rows,
                            aligns=["left", "right", "right", "left"],
                            max_widths=[20, 14, 14, 40]))

    verdict = "✅ 未发现实质性问题" if stats.consistent else "⚠️ 存在需要关注的差异"
    out.append("")
    out.append(f"  结论：{verdict}")

    if ctx["schema"]:
        out.append("")
        out.append("【字段增减】")
        for row in ctx["schema"]:
            out.append(f"  · {row['列名']}  —— {row['状态']}")

    # -- 差异字段 ------------------------------------------------------
    cols = [c for c in ctx["top_columns"] if c.severe_total > 0]
    if cols:
        out.append("")
        out.append("【实质差异最多的字段】")
        rows = [
            [
                c.name,
                TYPE_LABELS.get(c.ctype, c.ctype),
                f"{c.matched_rows:,}",
                f"{c.value_diff:,}",
                f"{c.null_mismatch:,}",
                f"{c.type_mismatch:,}",
                c.severe_rate,
                f"{c.null_rate_before:.1%}→{c.null_rate_after:.1%}",
            ]
            for c in cols
        ]
        out.append(
            render_table(
                ["字段", "类型", "对比数", "值不同", "空值不等", "类型异常", "差异率", "空值率变化"],
                rows,
                aligns=["left", "center", "right", "right", "right", "right", "right", "center"],
                max_widths=[26, 6, 10, 9, 9, 9, 9, 16],
            )
        )

    fmt_cols = ctx["format_columns"]
    if fmt_cols:
        out.append("")
        out.append("【仅格式差异的字段】（语义一致，不算数据错误）")
        rows = [
            [c.name, TYPE_LABELS.get(c.ctype, c.ctype), f"{c.format_only:,}",
             f"{c.format_only / c.matched_rows:.1%}" if c.matched_rows else "-"]
            for c in fmt_cols
        ]
        out.append(
            render_table(
                ["字段", "类型", "仅格式差异", "占比"],
                rows,
                aligns=["left", "center", "right", "right"],
                max_widths=[30, 6, 12, 10],
            )
        )

    # -- 异常 ----------------------------------------------------------
    if ctx["anomalies"]:
        out.append("")
        out.append("【异常数据】")
        for a in ctx["anomalies"]:
            level = LEVEL_LABELS.get(a["级别"], a["级别"])
            out.append(f"  [{level}] {a['说明']}")
            if a["详情"]:
                out.append(f"          {truncate(a['详情'], 150)}")

    # -- 差异示例 ------------------------------------------------------
    if cfg.report.top_n > 0 and stats.cell_diffs > 0:
        samples = D.diff_rows(
            result.con, cfg, stats, limit=min(cfg.report.top_n, 40)
        )
        if samples:
            out.append("")
            out.append(f"【差异示例】（按严重程度展示前 {len(samples)} 条）")
            headers = list(D.key_columns(stats)) + ["字段", "状态", "前值", "后值"]
            rows = []
            for s in samples:
                row = [s.get(k, "") for k in D.key_columns(stats)]
                row += [
                    s["column_name"],
                    STATUS_LABELS.get(s["status"], s["status"]),
                    s["before_raw"],
                    s["after_raw"],
                ]
                rows.append(row)
            aligns = ["left"] * len(headers)
            max_widths = [18] * len(D.key_columns(stats)) + [20, 10, 30, 30]
            out.append(render_table(headers, rows, aligns, max_widths))

    if ctx["warnings"]:
        out.append("")
        out.append("【提示】")
        for w in ctx["warnings"]:
            out.append(f"  · {truncate(w, 150)}")

    if ctx["db_path"]:
        out.append("")
        out.append(f"  明细数据保留在 DuckDB：{ctx['db_path']}")
        out.append("  可自查：SELECT * FROM v_diff LIMIT 20;")

    out.append(bar)
    return "\n".join(out)
