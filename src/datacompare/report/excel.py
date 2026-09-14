"""Excel 报告（多 Sheet）。

性能要点
--------
openpyxl 的**普通模式**每写一个单元格都会构造 `Cell` 对象并维护整张工作表，
写 20 万行要 6 秒以上；换成 **write_only 流式模式**后只做「行 → XML」的序列化，
同样数据量只要 0.3 秒（实测快 20 倍，甚至比 DuckDB 的 C++ xlsx 导出还快）。

代价是流式模式下不能再回头改单元格，所以：

* 列宽、冻结行、筛选器必须在写行**之前**设置好
* 需要着色的单元格要用 ``WriteOnlyCell`` 提前包好
"""

from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence

from ..config import Config
from ..models import (
    LEVEL_LABELS,
    ROW_ONLY_IN_AFTER,
    ROW_ONLY_IN_BEFORE,
    STATUS_LABELS,
    TYPE_LABELS,
)
from . import data as D

_HEADER_FILL = "FF1F2937"
_LEVEL_FILL = {
    "error": "FFFEE2E2",
    "warn": "FFFEF3C7",
    "info": "FFE0F2FE",
}
_FILL_CACHE: dict = {}

_STATUS_FILL = {
    "VALUE_DIFF": "FFFEE2E2",
    "NULL_MISMATCH": "FFF3E8FF",
    "TYPE_MISMATCH": "FFE0F7FA",
    "FORMAT_ONLY": "FFFEF3C7",
}


def _require_openpyxl():
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("导出 Excel 需要 openpyxl，请执行 pip install openpyxl") from exc
    return openpyxl


class _SheetWriter:
    """write_only 模式下的工作表封装：统一处理表头样式与列宽。"""

    def __init__(self, wb, title: str, headers: Sequence[str],
                 widths: Optional[Sequence[int]] = None,
                 freeze: Optional[str] = None,
                 total_rows: Optional[int] = None):
        self.ws = wb.create_sheet(title)
        self.headers = list(headers)
        self.ws.freeze_panes = freeze or "A2"
        if widths:
            from openpyxl.utils import get_column_letter

            for idx, width in enumerate(widths, start=1):
                self.ws.column_dimensions[get_column_letter(idx)].width = width
        if total_rows is not None and self.headers:
            from openpyxl.utils import get_column_letter

            self.ws.auto_filter.ref = (
                f"A1:{get_column_letter(len(self.headers))}{max(total_rows, 1)}"
            )
        self._header_font = None
        self._header_fill = None
        self._written = False

    def _styled(self, value: Any, *, header: bool = False, fill: Optional[str] = None):
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment, Font, PatternFill

        cell = WriteOnlyCell(self.ws, value=value)
        if header:
            if self._header_font is None:
                self._header_font = Font(bold=True, color="FFFFFFFF")
                self._header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(vertical="center", horizontal="center")
        elif fill:
            cached = _FILL_CACHE.get(fill)
            if cached is None:
                cached = PatternFill("solid", fgColor=fill)
                _FILL_CACHE[fill] = cached
            cell.fill = cached
        return cell

    def write_header(self):
        if self.headers and not self._written:
            self.ws.append([self._styled(h, header=True) for h in self.headers])
            self._written = True

    def append(self, values: Sequence[Any],
               fill: Optional[str] = None, fill_index: int = 0):
        """写一行。``fill`` 会作用在 ``fill_index`` 指定的那一列上。"""
        self.write_header()
        cells = list(values)
        if fill and 0 <= fill_index < len(cells):
            # 只为这一列包 WriteOnlyCell；整行都包会凭空多出上万次对象构造
            cells[fill_index] = self._styled(cells[fill_index], fill=fill)
        self.ws.append(cells)


def write_excel(result, cfg: Config, path: str) -> str:
    openpyxl = _require_openpyxl()
    wb = openpyxl.Workbook(write_only=True)
    stats = result.stats
    ctx = D.build_context(result, cfg)
    con = result.con

    # ---------------------------------------------------------------- 概览
    overview = [
        ["报告标题", cfg.report.title],
        ["前数据集", stats.before_path],
        ["前数据集行数", stats.before_rows],
        ["前数据集列数", len(stats.before_columns)],
        ["后数据集", stats.after_path],
        ["后数据集行数", stats.after_rows],
        ["后数据集列数", len(stats.after_columns)],
        ["主键列", ", ".join(stats.key_columns) or "（按行号对齐）"],
        ["主键识别方式", stats.key_strategy],
        ["匹配成功行数", stats.matched_pairs],
        ["完全一致行数", stats.same_rows],
        ["存在差异行数", stats.changed_rows],
        ["仅前数据集行数", stats.only_in_before],
        ["仅后数据集行数", stats.only_in_after],
        ["主键重复组数（前 / 后）", f"{stats.dup_key_before} / {stats.dup_key_after}"],
        ["参与对比字段数", stats.compared_columns],
        ["字段总数（前）", stats.total_columns],
        ["单元格差异总数", stats.cell_diffs],
        ["　实质差异", stats.severe_cell_diffs],
        ["　仅格式差异", stats.format_only_cells],
        ["结论", "未发现实质性问题" if stats.consistent else "存在需要关注的差异"],
    ]
    ws = _SheetWriter(wb, "概览", ["项目", "值"], widths=[28, 80],
                      freeze="A2", total_rows=len(overview))
    ws.write_header()
    for row in overview:
        ws.append(row)

    # ------------------------------------------------------------ 字段汇总
    headers = [
        "列名", "后数据集列名", "处理方式", "推断类型", "对比行数", "一致",
        "仅格式差异", "值不同", "空值不一致", "类型异常", "差异合计", "实质差异",
        "实质差异率", "前空值率", "后空值率", "前取值数", "后取值数", "说明",
    ]
    columns = ctx["columns"]
    ws = _SheetWriter(
        wb, "字段汇总", headers,
        widths=[24, 20, 12, 8, 10, 10, 11, 9, 11, 9, 10, 10, 10, 10, 10, 10, 10, 40],
        freeze="C2", total_rows=len(columns) + 1,
    )
    ws.write_header()
    for r in columns:
        ws.append([
            r["列名"], r["后数据集列名"], r["处理方式"],
            TYPE_LABELS.get(r["推断类型"], r["推断类型"]),
            r["对比行数"], r["一致"], r["仅格式差异"], r["值不同"],
            r["空值不一致"], r["类型异常"], r["差异合计"], r["实质差异"],
            r["差异率"], r["前空值率"], r["后空值率"],
            r["前取值数"], r["后取值数"], r["说明"],
        ])

    # ------------------------------------------------------------ 差异明细
    keys = D.key_columns(stats)
    headers = keys + [
        "字段", "类型", "状态", "前值", "后值", "归一化前值", "归一化后值",
        "绝对差", "相对差", "前行号", "后行号",
    ]
    limit = cfg.report.excel_max_rows or None
    total = stats.cell_diffs if not limit else min(stats.cell_diffs, limit)
    ws = _SheetWriter(
        wb, "差异明细", headers,
        widths=[18] * len(keys) + [22, 8, 12, 30, 30, 30, 30, 12, 12, 10, 10],
        freeze="A2", total_rows=total + 1,
    )
    ws.write_header()
    status_index = len(keys) + 2
    for values, fill in _iter_excel_diff_rows(con, cfg, stats, keys, limit):
        ws.append(values, fill=fill, fill_index=status_index)

    # ------------------------------------------------------------ 单边数据
    for name, status in (("仅前数据集", ROW_ONLY_IN_BEFORE),
                         ("仅后数据集", ROW_ONLY_IN_AFTER)):
        count = stats.only_in_before if status == ROW_ONLY_IN_BEFORE else stats.only_in_after
        if not count:
            continue
        cols_rows = D.iter_only_in_rows(
            con, cfg, stats, status, cfg.report.only_in_max_rows
        )
        try:
            cols = next(cols_rows)
        except StopIteration:
            continue
        ws = _SheetWriter(wb, name, cols, widths=[20] * len(cols),
                          freeze="A2", total_rows=count + 1)
        ws.write_header()
        for row in cols_rows:
            ws.ws.append(row)

    # ------------------------------------------------------------ 异常数据
    if ctx["anomalies"]:
        anomalies = ctx["anomalies"]
        ws = _SheetWriter(wb, "异常数据",
                          ["级别", "说明", "范围", "字段", "数量", "详情"],
                          widths=[8, 50, 8, 22, 10, 70], total_rows=len(anomalies) + 1)
        ws.write_header()
        for a in anomalies:
            ws.append(
                [LEVEL_LABELS.get(a["级别"], a["级别"]),
                 a["说明"], a["范围"], a["字段"], a["数量"], a["详情"]],
                fill=_LEVEL_FILL.get(a["级别"]),
            )

    # ------------------------------------------------------------ 字段增减
    if ctx["schema"]:
        schema = ctx["schema"]
        ws = _SheetWriter(wb, "字段增减", ["列名", "状态"], widths=[30, 20],
                          total_rows=len(schema) + 1)
        ws.write_header()
        for r in schema:
            ws.append([r["列名"], r["状态"]])

    # ---------------------------------------------------------------- 提示
    if ctx["warnings"]:
        ws = _SheetWriter(wb, "提示", ["提示"], widths=[100],
                          total_rows=len(ctx["warnings"]) + 1)
        ws.write_header()
        for w in ctx["warnings"]:
            ws.append([w])

    wb.save(path)
    return path


def _iter_excel_diff_rows(
    con, cfg: Config, stats, keys: Sequence[str], limit: Optional[int]
) -> Iterator[tuple]:
    """流式产出差异明细行，避免一次性构造几十万个 dict。

    产出 ``(值列表, 状态列底色)``，底色由状态决定。
    """
    for d in D.iter_diff_rows(
        con, cfg, stats, limit=limit, order_by_value=False,
        max_cell_chars=cfg.report.excel_max_cell_chars,
    ):
        values = [d.get(k) for k in keys] + [
            d["column_name"],
            TYPE_LABELS.get(d["column_type"], d["column_type"]),
            STATUS_LABELS.get(d["status"], d["status"]),
            d["before_raw"],
            d["after_raw"],
            d["before_norm"],
            d["after_norm"],
            d["abs_diff"],
            d["rel_diff"],
            d["__b_row"],
            d["__a_row"],
        ]
        yield values, _STATUS_FILL.get(d["status"])
