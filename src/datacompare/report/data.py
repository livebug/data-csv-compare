"""报告层的数据查询助手。

报告需要的东西都从 DuckDB 里现查，避免把大结果集搬进内存。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..models import (
    FORMAT_ONLY,
    NULL_MISMATCH,
    ROW_ONLY_IN_BEFORE,
    STATUS_LABELS,
    Stats,
    TYPE_MISMATCH,
    VALUE_DIFF,
)
from ..sqlutil import quote_ident, sql_list, sql_str

#: 差异排序：严重的问题排前面
_STATUS_RANK = (
    "CASE status "
    f"WHEN '{VALUE_DIFF}' THEN 0 "
    f"WHEN '{NULL_MISMATCH}' THEN 1 "
    f"WHEN '{TYPE_MISMATCH}' THEN 2 "
    f"WHEN '{FORMAT_ONLY}' THEN 3 "
    "ELSE 4 END"
)

#: 「仅单边存在」的行预览最多展示几列
PREVIEW_COLS = 8

#: 把状态码翻成中文标签的 SQL 片段
STATUS_LABEL_SQL = (
    "CASE status "
    + " ".join(f"WHEN {sql_str(k)} THEN {sql_str(v)}" for k, v in STATUS_LABELS.items())
    + " ELSE status END"
)


def fetch_dicts(con, sql: str) -> List[Dict[str, Any]]:
    rel = con.sql(sql)
    cols = list(rel.columns)
    return [dict(zip(cols, row)) for row in rel.fetchall()]


def key_columns(stats: Stats) -> List[str]:
    return list(stats.key_columns)


def key_exprs(stats: Stats, prefix: str = "") -> List[str]:
    """生成主键列的 SELECT 表达式（没有主键时退化为行号）。"""
    cols = key_columns(stats)
    if cols:
        return [f"{prefix}{quote_ident(c)} AS {quote_ident(c)}" for c in cols]
    return [
        f"coalesce(CAST({prefix}__b_row AS VARCHAR), "
        f'CAST({prefix}__a_row AS VARCHAR)) AS "行号"'
    ]


def key_select(stats: Stats, prefix: str = "") -> str:
    return ", ".join(key_exprs(stats, prefix))


def status_counts(con) -> Dict[str, int]:
    rows = con.sql(
        "SELECT status, count(*) FROM diff_detail GROUP BY 1"
    ).fetchall()
    return {str(s): int(c) for s, c in rows}


def diff_rows(
    con,
    cfg: Config,
    stats: Stats,
    limit: Optional[int] = None,
    statuses: Optional[Sequence[str]] = None,
    column: Optional[str] = None,
    order_by_value: bool = True,
) -> List[Dict[str, Any]]:
    """查差异明细。默认按「严重程度 + 相对差异幅度」排序。"""
    return list(
        iter_diff_rows(
            con,
            cfg,
            stats,
            limit=limit,
            statuses=statuses,
            column=column,
            order_by_value=order_by_value,
        )
    )


def diff_rows_sql(
    con,
    cfg: Config,
    stats: Stats,
    limit: Optional[int] = None,
    statuses: Optional[Sequence[str]] = None,
    column: Optional[str] = None,
    order_by_value: bool = True,
    label_status: bool = False,
    max_cell_chars: Optional[int] = None,
) -> str:
    """生成差异明细查询的 SQL 文本（供 COPY 导出等场景复用）。"""
    where = []
    if statuses:
        where.append(f"status IN {sql_list(statuses)}")
    if column:
        where.append(f"column_name = {sql_str(column)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if order_by_value:
        order = (
            f"{_STATUS_RANK}, "
            "CASE WHEN abs_diff IS NOT NULL THEN abs(abs_diff) END DESC NULLS LAST, "
            "column_name"
        )
    else:
        order = f"{_STATUS_RANK}, __b_row, column_name"

    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    keys = key_select(stats)
    max_chars = int(
        cfg.report.max_cell_chars if max_cell_chars is None else max_cell_chars
    )
    status_col = STATUS_LABEL_SQL if label_status else "status"

    return f"""
        SELECT {keys},
               column_name,
               column_type,
               {status_col} AS status,
               left(coalesce(before_raw, '<NULL>'), {max_chars}) AS before_raw,
               left(coalesce(after_raw, '<NULL>'), {max_chars}) AS after_raw,
               left(coalesce(before_norm, ''), {max_chars}) AS before_norm,
               left(coalesce(after_norm, ''), {max_chars}) AS after_norm,
               abs_diff, rel_diff, __b_row, __a_row
        FROM v_diff
        {where_sql}
        ORDER BY {order}
        {limit_sql}
    """


def iter_diff_rows(
    con,
    cfg: Config,
    stats: Stats,
    limit: Optional[int] = None,
    statuses: Optional[Sequence[str]] = None,
    column: Optional[str] = None,
    order_by_value: bool = True,
    batch: int = 20000,
    max_cell_chars: Optional[int] = None,
):
    """流式产出差异明细。

    差异行可能有几百万条，一次性构造 list[dict] 会吃掉大量内存，
    所以这里按批 ``fetchmany``。
    """
    rel = con.sql(
        diff_rows_sql(
            con, cfg, stats, limit=limit, statuses=statuses,
            column=column, order_by_value=order_by_value,
            max_cell_chars=max_cell_chars,
        )
    )
    cols = list(rel.columns)
    while True:
        chunk = rel.fetchmany(batch)
        if not chunk:
            break
        for row in chunk:
            yield dict(zip(cols, row))


def only_in_rows_sql(
    con,
    cfg: Config,
    stats: Stats,
    status: str,
    limit: Optional[int] = None,
) -> str:
    """生成「只在一侧存在」的行查询 SQL。"""
    source = "before_ranked" if status == ROW_ONLY_IN_BEFORE else "after_ranked"
    row_col = "__b_row" if status == ROW_ONLY_IN_BEFORE else "__a_row"

    all_cols = [r[0] for r in con.sql(f"DESCRIBE {quote_ident(source)}").fetchall()]
    keys = key_columns(stats)
    preview = [c for c in all_cols if c not in keys and not c.startswith("__")][:PREVIEW_COLS]

    select = key_exprs(stats, "v.")
    select.append('CAST(s.__row_id AS VARCHAR) AS "源行号"')
    for col in preview:
        select.append(f"CAST(s.{quote_ident(col)} AS VARCHAR) AS {quote_ident(col)}")

    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = (
        f"SELECT {', '.join(select)} FROM v_row v "
        f"JOIN {quote_ident(source)} s ON s.__row_id = v.{row_col} "
        f"WHERE v.row_status = {sql_str(status)} "
        f"ORDER BY s.__row_id {limit_sql}"
    )
    return sql


def only_in_rows(
    con,
    cfg: Config,
    stats: Stats,
    status: str,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查「只在一侧存在」的行，附带少量列值预览。"""
    return list(iter_only_in_rows(con, cfg, stats, status, limit))


def iter_only_in_rows(
    con,
    cfg: Config,
    stats: Stats,
    status: str,
    limit: Optional[int] = None,
    batch: int = 5000,
):
    """流式产出：先 yield 一次列名列表，之后每行 yield 一个值列表。"""
    sql = only_in_rows_sql(con, cfg, stats, status, limit)
    rel = con.sql(sql)
    cols = list(rel.columns)
    yield cols
    while True:
        chunk = rel.fetchmany(batch)
        if not chunk:
            break
        for row in chunk:
            yield list(row)


def column_summary_rows(cfg: Config, result) -> List[Dict[str, Any]]:
    """字段级汇总（含未参与对比的字段）。"""
    rows: List[Dict[str, Any]] = []
    for col in result.columns:
        rows.append(
            {
                "列名": col.name,
                "后数据集列名": col.after_name if col.after_name != col.name else "",
                "处理方式": _mode_label(col.mode),
                "推断类型": col.ctype if col.mode == "compared" else "",
                "对比行数": col.matched_rows,
                "一致": col.equal,
                "仅格式差异": col.format_only,
                "值不同": col.value_diff,
                "空值不一致": col.null_mismatch,
                "类型异常": col.type_mismatch,
                "差异合计": col.diff_total,
                "实质差异": col.severe_total,
                "差异率": col.severe_rate,
                "格式差异率": (col.format_only / col.matched_rows) if col.matched_rows else 0.0,
                "前空值率": col.null_rate_before,
                "后空值率": col.null_rate_after,
                "前取值数": col.distinct_before,
                "后取值数": col.distinct_after,
                "说明": col.note,
            }
        )
    return rows


def _mode_label(mode: str) -> str:
    return {
        "compared": "参与对比",
        "key": "主键",
        "ignored": "已忽略",
        "only_before": "仅前数据集",
        "only_after": "仅后数据集",
    }.get(mode, mode)


def top_diff_columns(result, limit: int = 20) -> List[Any]:
    cols = [c for c in result.columns if c.mode == "compared"]
    cols.sort(
        key=lambda c: (c.severe_total, c.diff_total, c.name),
        reverse=True,
    )
    return cols[:limit]


def format_only_columns(result, limit: int = 20) -> List[Any]:
    cols = [c for c in result.columns if c.mode == "compared" and c.format_only > 0]
    cols.sort(key=lambda c: c.format_only, reverse=True)
    return cols[:limit]


def anomaly_rows(result) -> List[Dict[str, Any]]:
    rows = []
    for a in result.anomalies:
        rows.append(
            {
                "级别": a.level,
                "类别": a.category,
                "说明": a.title,
                "范围": a.scope,
                "字段": a.column,
                "详情": a.detail,
                "数量": a.count,
            }
        )
    return rows


def schema_rows(stats: Stats) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for col in stats.only_before_columns:
        rows.append({"列名": col, "状态": "仅前数据集存在", "说明": ""})
    for col in stats.only_after_columns:
        rows.append({"列名": col, "状态": "仅后数据集存在", "说明": ""})
    return rows


def build_context(result, cfg: Config):
    """组装报告用的上下文（不含差异明细，明细按需查）。"""
    stats = result.stats
    return {
        "title": cfg.report.title,
        "stats": stats,
        "columns": column_summary_rows(cfg, result),
        "top_columns": top_diff_columns(result, cfg.report.top_n),
        "format_columns": format_only_columns(result, cfg.report.top_n),
        "anomalies": anomaly_rows(result),
        "schema": schema_rows(stats),
        "status_counts": status_counts(result.con),
        "warnings": list(result.warnings),
        "db_path": result.db_path,
        "treat_format_only_as_diff": cfg.report.treat_format_only_as_diff,
    }
