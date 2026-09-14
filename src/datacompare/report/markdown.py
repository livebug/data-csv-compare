"""Markdown 报告。"""

from __future__ import annotations

from typing import Any, List, Sequence

from ..config import Config
from ..models import LEVEL_LABELS, STATUS_LABELS, TYPE_LABELS
from . import data as D


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        cells = []
        for value in row:
            if value is None:
                cells.append("")
            elif isinstance(value, float):
                cells.append(f"{value:.4f}".rstrip("0").rstrip(".") or "0")
            else:
                cells.append(str(value).replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _pct(value: float) -> str:
    return f"{value:.2%}"


_CN_NUM = "一二三四五六七八九十"


class _Section:
    """按出现顺序自动编号，避免因为某节被跳过而出现「三」前面没有「二」。"""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return _CN_NUM[self._n - 1] if self._n <= len(_CN_NUM) else str(self._n)


def render_markdown(result, cfg: Config) -> str:
    ctx = D.build_context(result, cfg)
    stats = ctx["stats"]
    out: List[str] = []

    out.append(f"# {ctx['title']}")
    out.append("")
    out.append(f"- **前数据集**：`{stats.before_path}`（{stats.before_rows:,} 行 × {len(stats.before_columns)} 列）")
    out.append(f"- **后数据集**：`{stats.after_path}`（{stats.after_rows:,} 行 × {len(stats.after_columns)} 列）")
    if stats.key_columns:
        out.append(f"- **主键**：{', '.join(f'`{k}`' for k in stats.key_columns)}（{stats.key_strategy}）")
    else:
        out.append(f"- **行匹配**：{stats.key_strategy}")
    out.append("")

    n = _Section()
    out.extend([f"## {n.next()}、总体结论", ""])
    verdict = "✅ 未发现实质性问题" if stats.consistent else "⚠️ 存在需要关注的差异"
    out.append(f"> **{verdict}**")
    out.append("")
    out.extend(
        _table(
            ["指标", "数值", "说明"],
            [
                ["前数据集行数", f"{stats.before_rows:,}", ""],
                ["后数据集行数", f"{stats.after_rows:,}", f"{stats.row_delta:+,}（{_pct(stats.row_delta_rate)}）"],
                ["匹配成功", f"{stats.matched_pairs:,}", ""],
                ["　完全一致", f"{stats.same_rows:,}", ""],
                ["　存在差异", f"{stats.changed_rows:,}", ""],
                ["仅前数据集存在", f"{stats.only_in_before:,}", ""],
                ["仅后数据集存在", f"{stats.only_in_after:,}", ""],
                ["单元格差异", f"{stats.cell_diffs:,}", ""],
                ["　实质差异", f"{stats.severe_cell_diffs:,}", "值不同 / 空值不一致 / 类型异常"],
                ["　仅格式差异", f"{stats.format_only_cells:,}", "补零、千分位、大小写、空白等，语义一致"],
                ["主键重复组数", f"{stats.dup_key_before:,} / {stats.dup_key_after:,}", "前 / 后"],
            ],
        )
    )
    out.append("")

    if ctx["schema"]:
        out.extend([f"## {n.next()}、字段增减", ""])
        out.extend(_table(["列名", "状态"], [[r["列名"], r["状态"]] for r in ctx["schema"]]))
        out.append("")

    cols = [c for c in ctx["top_columns"] if c.severe_total > 0]
    if cols:
        out.extend([f"## {n.next()}、实质差异字段", ""])
        out.extend(
            _table(
                ["字段", "类型", "对比数", "值不同", "空值不一致", "类型异常", "差异率", "空值率变化"],
                [
                    [
                        f"`{c.name}`",
                        TYPE_LABELS.get(c.ctype, c.ctype),
                        f"{c.matched_rows:,}",
                        f"{c.value_diff:,}",
                        f"{c.null_mismatch:,}",
                        f"{c.type_mismatch:,}",
                        _pct(c.severe_rate),
                        f"{_pct(c.null_rate_before)} → {_pct(c.null_rate_after)}",
                    ]
                    for c in cols
                ],
            )
        )
        out.append("")

    if ctx["format_columns"]:
        out.extend([f"## {n.next()}、仅格式差异字段", ""])
        out.append("这些字段两侧值语义一致，只是写法不同（补零、千分位、大小写、空白等），**不算数据错误**。")
        out.append("")
        out.extend(
            _table(
                ["字段", "类型", "仅格式差异", "占比"],
                [
                    [
                        f"`{c.name}`",
                        TYPE_LABELS.get(c.ctype, c.ctype),
                        f"{c.format_only:,}",
                        _pct(c.format_only / c.matched_rows) if c.matched_rows else "-",
                    ]
                    for c in ctx["format_columns"]
                ],
            )
        )
        out.append("")

    if ctx["anomalies"]:
        out.extend([f"## {n.next()}、异常数据", ""])
        out.extend(
            _table(
                ["级别", "说明", "范围", "字段", "详情"],
                [
                    [
                        LEVEL_LABELS.get(a["级别"], a["级别"]),
                        a["说明"],
                        a["范围"],
                        a["字段"],
                        a["详情"],
                    ]
                    for a in ctx["anomalies"]
                ],
            )
        )
        out.append("")

    limit = cfg.report.markdown_max_rows
    if limit > 0 and stats.cell_diffs > 0:
        samples = D.diff_rows(result.con, cfg, stats, limit=limit)
        if samples:
            out.extend([f"## {n.next()}、差异明细（前 {len(samples)} 条）", ""])
            keys = D.key_columns(stats) or ["行号"]
            headers = keys + ["字段", "状态", "前值", "后值"]
            rows = []
            for s in samples:
                row = [s.get(k, "") for k in keys]
                row += [
                    s["column_name"],
                    STATUS_LABELS.get(s["status"], s["status"]),
                    s["before_raw"],
                    s["after_raw"],
                ]
                rows.append(row)
            out.extend(_table(headers, rows))
            out.append("")

    if ctx["warnings"]:
        out.extend(["## 附：提示", ""])
        for w in ctx["warnings"]:
            out.append(f"- {w}")
        out.append("")

    if ctx["db_path"]:
        out.extend(
            [
                "## 附：明细数据",
                "",
                f"完整差异明细保存在 DuckDB 库：`{ctx['db_path']}`",
                "",
                "```sql",
                "SELECT * FROM v_diff LIMIT 100;",
                "SELECT * FROM v_row WHERE row_status <> 'SAME' LIMIT 100;",
                "```",
            ]
        )
    return "\n".join(out)


def write_markdown(result, cfg: Config, path: str) -> str:
    text = render_markdown(result, cfg)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
