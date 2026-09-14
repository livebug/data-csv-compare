"""异常数据检测。

这里回答的是「除了逐格差异，还有哪些地方不对劲」：

* 结构类：字段增减、行数突变
* 主键类：主键重复、主键对不上
* 质量类：数值/日期列出现脏数据、空值率突变
* 分布类：数值离群点、均值/中位数漂移、取值域增减
* 格式类：大量「仅格式差异」的字段，提示上游格式不统一
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .config import Config
from .models import (
    Anomaly,
    ColumnProfile,
    ColumnResult,
    MODE_COMPARED,
    ROW_ONLY_IN_AFTER,
    ROW_ONLY_IN_BEFORE,
    Stats,
    TYPE_BOOLEAN,
    TYPE_DATE,
    TYPE_NUMERIC,
)
from .normalize import (
    boolean_expr,
    date_expr,
    numeric_expr,
    string_expr,
    _effective_string_opts,
)
from .sqlutil import quote_ident, sql_regex, sql_str

_MAX_SAMPLES = 5


def detect_anomalies(
    con,
    cfg: Config,
    stats: Stats,
    columns: Sequence[ColumnResult],
    profiles_before: Optional[Dict[str, ColumnProfile]] = None,
    profiles_after: Optional[Dict[str, ColumnProfile]] = None,
) -> List[Anomaly]:
    opts = cfg.anomaly
    out: List[Anomaly] = []
    pb = profiles_before or {}
    pa = profiles_after or {}

    _schema(stats, out)
    _row_count(cfg, stats, out)
    _keys(con, cfg, stats, out)
    _format_only(con, cfg, out)

    for col in columns:
        if col.mode != MODE_COMPARED:
            continue
        _null_rate(cfg, col, out)
        _distinct_change(cfg, col, out)
        _parse_failures(cfg, col, pb.get(col.name), pa.get(col.name), out)
        if col.ctype == TYPE_NUMERIC:
            _numeric_column(con, cfg, col, out)
        if opts.category_max_distinct > 0 and col.ctype not in (TYPE_NUMERIC, TYPE_DATE):
            _categories(con, cfg, col, out)

    return out


# --------------------------------------------------------------------------
# 结构 / 行数 / 主键
# --------------------------------------------------------------------------
def _schema(stats: Stats, out: List[Anomaly]) -> None:
    if stats.only_before_columns:
        out.append(
            Anomaly(
                level="error",
                category="schema_change",
                title=f"后数据集缺少 {len(stats.only_before_columns)} 个字段",
                scope="after",
                detail="、".join(stats.only_before_columns[:20])
                + ("…" if len(stats.only_before_columns) > 20 else ""),
                count=len(stats.only_before_columns),
            )
        )
    if stats.only_after_columns:
        out.append(
            Anomaly(
                level="error",
                category="schema_change",
                title=f"后数据集新增 {len(stats.only_after_columns)} 个字段",
                scope="after",
                detail="、".join(stats.only_after_columns[:20])
                + ("…" if len(stats.only_after_columns) > 20 else ""),
                count=len(stats.only_after_columns),
            )
        )


def _row_count(cfg: Config, stats: Stats, out: List[Anomaly]) -> None:
    if stats.before_rows == stats.after_rows:
        return
    delta = stats.row_delta
    out.append(
        Anomaly(
            level="info" if abs(stats.row_delta_rate) < 0.05 else "warn",
            category="row_count_change",
            title=f"总行数变化 {delta:+,} 行（{stats.row_delta_rate:+.2%}）",
            scope="both",
            detail=f"前 {stats.before_rows:,} 行 → 后 {stats.after_rows:,} 行",
            count=abs(delta),
        )
    )


def _keys(con, cfg: Config, stats: Stats, out: List[Anomaly]) -> None:
    opts = cfg.anomaly

    for side, dup in (("前数据集", stats.dup_key_before), ("后数据集", stats.dup_key_after)):
        if dup <= 0:
            continue
        examples = _dup_examples(con, stats, side)
        out.append(
            Anomaly(
                level="error",
                category="duplicate_key",
                title=f"{side}有 {dup:,} 组主键重复",
                scope="before" if side.startswith("前") else "after",
                detail="主键不唯一，行匹配结果可能不可靠；示例：" + examples,
                count=dup,
            )
        )

    if stats.only_in_before:
        examples = _unmatched_examples(con, stats, ROW_ONLY_IN_BEFORE, opts.max_examples)
        out.append(
            Anomaly(
                level="warn",
                category="unmatched_key",
                title=f"{stats.only_in_before:,} 行只在前数据集存在",
                scope="before",
                detail="示例：" + examples,
                count=stats.only_in_before,
            )
        )
    if stats.only_in_after:
        examples = _unmatched_examples(con, stats, ROW_ONLY_IN_AFTER, opts.max_examples)
        out.append(
            Anomaly(
                level="warn",
                category="unmatched_key",
                title=f"{stats.only_in_after:,} 行只在后数据集存在",
                scope="after",
                detail="示例：" + examples,
                count=stats.only_in_after,
            )
        )


def _key_projection(stats: Stats) -> str:
    if stats.key_columns:
        return ", ".join(f"CAST({quote_ident(k)} AS VARCHAR)" for k in stats.key_columns)
    return "CAST(__b_row AS VARCHAR)"


def _dup_examples(con, stats: Stats, side: str) -> str:
    if not stats.key_columns:
        return "（按行号对齐模式，无主键）"
    table = "before_ranked" if side.startswith("前") else "after_ranked"
    keys = ", ".join(quote_ident(k) for k in stats.key_columns)
    try:
        rows = con.sql(
            f"SELECT {keys}, count(*) AS c FROM {table} "
            f"GROUP BY {keys} HAVING count(*) > 1 ORDER BY c DESC "
            f"LIMIT {_MAX_SAMPLES}"
        ).fetchall()
    except Exception:
        return ""
    parts = []
    for row in rows:
        key = " | ".join("" if v is None else str(v) for v in row[:-1])
        parts.append(f"{key}×{row[-1]}")
    return "；".join(parts)


def _unmatched_examples(con, stats: Stats, status: str, limit: int) -> str:
    proj = _key_projection(stats)
    col = "__b_row" if status == ROW_ONLY_IN_BEFORE else "__a_row"
    try:
        rows = con.sql(
            f"SELECT {proj} FROM v_row WHERE row_status = {sql_str(status)} "
            f"ORDER BY {col} LIMIT {max(1, min(limit, _MAX_SAMPLES * 4))}"
        ).fetchall()
    except Exception:
        return ""
    return "、".join("" if r[0] is None else str(r[0]) for r in rows)


# --------------------------------------------------------------------------
# 格式差异归类
# --------------------------------------------------------------------------
_WS_PAT = sql_regex("[\\s\u00a0\u3000]")


def _strip_ws(expr: str) -> str:
    return f"regexp_replace(coalesce({expr}, ''), {_WS_PAT}, '', 'g')"


def _format_only(con, cfg: Config, out: List[Anomaly]) -> None:
    """把「仅格式差异」再细分，直接回答「补零 / 大小写 / 空白能不能识别」。"""
    try:
        rows = con.sql(
            f"""
            SELECT column_name,
                   count(*) AS total,
                   count(*) FILTER (WHERE lower(before_raw) = lower(after_raw)) AS case_only,
                   count(*) FILTER (
                     WHERE {_strip_ws('before_raw')} = {_strip_ws('after_raw')}
                   ) AS whitespace_only,
                   count(*) FILTER (
                     WHERE column_type = 'numeric'
                       AND before_norm IS NOT DISTINCT FROM after_norm
                   ) AS numeric_format
            FROM v_diff WHERE status = 'FORMAT_ONLY'
            GROUP BY column_name ORDER BY total DESC
            """
        ).fetchall()
    except Exception:
        return

    total_fmt = sum(int(r[1]) for r in rows)
    if total_fmt == 0:
        return

    detail_bits = []
    for name, total, case_only, ws_only, numeric_fmt in rows[:8]:
        bits = []
        if numeric_fmt:
            bits.append(f"数值补零/千分位 {int(numeric_fmt):,}")
        if case_only:
            bits.append(f"大小写 {int(case_only):,}")
        if ws_only:
            bits.append(f"空白 {int(ws_only):,}")
        detail_bits.append(f"{name}（{int(total):,}：{'、'.join(bits) or '其它写法'}）")

    out.append(
        Anomaly(
            level="info",
            category="case_only_change",
            title=f"{total_fmt:,} 个单元格属于「仅格式差异」，语义一致、不算数据错误",
            scope="both",
            detail="涉及字段：" + "；".join(detail_bits),
            count=total_fmt,
        )
    )


# --------------------------------------------------------------------------
# 单字段检查
# --------------------------------------------------------------------------
def _null_rate(cfg: Config, col: ColumnResult, out: List[Anomaly]) -> None:
    delta = col.null_rate_delta
    threshold = cfg.anomaly.null_rate_delta
    before_rate = col.null_rate_before
    after_rate = col.null_rate_after

    if abs(delta) < threshold:
        return

    grew = delta > 0
    if before_rate > 0:
        ratio = abs(delta) / before_rate
        severe = ratio >= 0.5 or after_rate >= 0.5
    else:
        severe = after_rate >= threshold

    out.append(
        Anomaly(
            level="warn" if severe else "info",
            category="null_rate_change",
            title=(
                f"字段 `{col.name}` 空值率{'上升' if grew else '下降'} "
                f"{abs(delta):.2%}"
            ),
            scope="after" if grew else "before",
            column=col.name,
            detail=f"{before_rate:.2%} → {after_rate:.2%}",
        )
    )


def _distinct_change(cfg: Config, col: ColumnResult, out: List[Anomaly]) -> None:
    before = col.distinct_before
    after = col.distinct_after
    if before == 0 and after == 0:
        return
    base = max(before, after, 1)
    change = abs(after - before) / base
    if change < cfg.anomaly.distinct_change_ratio:
        return
    out.append(
        Anomaly(
            level="info",
            category="distribution_shift",
            title=f"字段 `{col.name}` 取值个数变化 {change:.0%}",
            scope="both",
            column=col.name,
            detail=f"{before:,} 个不同取值 → {after:,} 个",
        )
    )


def _parse_failures(
    cfg: Config,
    col: ColumnResult,
    pb: Optional[ColumnProfile],
    pa: Optional[ColumnProfile],
    out: List[Anomaly],
) -> None:
    """找出「本该是数值/日期，却有值解析不了」的脏数据。

    除了明确判定为数值/日期的列，如果一列大部分值都能解析成数值，
    也会顺带提示——这种情况通常就是数值列里混进了脏数据。
    """
    if col.ctype == TYPE_NUMERIC:
        kinds = [("数值", "numeric_parse_failure", TYPE_NUMERIC)]
    elif col.ctype == TYPE_DATE:
        kinds = [("日期", "date_parse_failure", TYPE_DATE)]
    else:
        kinds = [
            ("数值", "numeric_parse_failure", TYPE_NUMERIC),
            ("日期", "date_parse_failure", TYPE_DATE),
        ]

    for kind, category, _type in kinds:
        # 非数值/日期类型的列要求大部分值可解析，避免对普通文本误报
        min_ratio = 0.0 if col.ctype in (TYPE_NUMERIC, TYPE_DATE) else 0.8
        for side, prof in (("前数据集", pb), ("后数据集", pa)):
            if prof is None or prof.non_null == 0:
                continue
            if _type == TYPE_NUMERIC:
                fails, ok = prof.numeric_fail, prof.numeric_ok
            else:
                fails, ok = prof.date_fail, prof.date_ok
            attempted = fails + ok
            if attempted == 0:
                continue
            ratio = fails / attempted
            parse_ratio = ok / attempted
            if ratio <= 0 or ratio < cfg.anomaly.numeric_parse_failure_ratio:
                continue
            if parse_ratio < min_ratio:
                continue
            out.append(
                Anomaly(
                    level="warn" if ratio >= 0.05 else "info",
                    category=category,
                    title=f"字段 `{col.name}` 在{side}有 {fails:,} 个值无法解析为{kind}",
                    scope="before" if side.startswith("前") else "after",
                    column=col.name,
                    detail=(
                        f"共 {attempted:,} 个非空值，其中 {ratio:.1%} 疑似脏数据"
                        f"（{parse_ratio:.1%} 可以解析为{kind}）"
                    ),
                    count=fails,
                )
            )


def _value_expr(cfg: Config, col: ColumnResult, ref: str) -> str:
    rule = cfg.rule(col.name)
    eff = _effective_string_opts(cfg, rule)
    if col.ctype == TYPE_NUMERIC:
        return numeric_expr(ref, cfg.compare.numeric, eff)
    if col.ctype == TYPE_DATE:
        return date_expr(ref, cfg.compare.date, eff, cfg.date_formats())
    if col.ctype == TYPE_BOOLEAN:
        return boolean_expr(ref, eff)
    return string_expr(ref, eff)


def _numeric_stats(con, table: str, expr: str, multiplier: float) -> Optional[dict]:
    sql = f"""
        WITH base AS (SELECT {expr} AS v FROM {quote_ident(table)}),
        st AS (
          SELECT count(*) AS total,
                 count(v) AS non_null,
                 avg(v) AS mean,
                 median(v) AS med,
                 quantile_cont(v, 0.25) AS q1,
                 quantile_cont(v, 0.75) AS q3,
                 min(v) AS vmin,
                 max(v) AS vmax
          FROM base
        )
        SELECT st.*,
               (SELECT count(*) FROM base, st
                 WHERE v < st.q1 - {multiplier} * (st.q3 - st.q1)
                    OR v > st.q3 + {multiplier} * (st.q3 - st.q1)) AS outliers
        FROM st
    """
    row = con.sql(sql).fetchone()
    if row is None:
        return None
    keys = ["total", "non_null", "mean", "med", "q1", "q3", "vmin", "vmax", "outliers"]
    return dict(zip(keys, row))


def _numeric_column(con, cfg: Config, col: ColumnResult, out: List[Anomaly]) -> None:
    opts = cfg.anomaly
    before_expr = _value_expr(cfg, col, quote_ident(col.name))
    after_expr = _value_expr(cfg, col, quote_ident(col.after_name or col.name))

    before = _numeric_stats(con, "before_ranked", before_expr, opts.outlier_multiplier)
    after = _numeric_stats(con, "after_ranked", after_expr, opts.outlier_multiplier)
    if not before or not after:
        return

    # 离群点
    if opts.outlier_enabled:
        for side, st in (("前数据集", before), ("后数据集", after)):
            if st["outliers"] and st["non_null"]:
                ratio = st["outliers"] / st["non_null"]
                if ratio >= 0.001 or st["outliers"] >= 5:
                    out.append(
                        Anomaly(
                            level="info",
                            category="numeric_outlier",
                            title=f"字段 `{col.name}` 在{side}有 {int(st['outliers']):,} 个离群点",
                            scope="before" if side.startswith("前") else "after",
                            column=col.name,
                            detail=(
                                f"正常区间约 [{_fmt(st['q1'] - opts.outlier_multiplier * (st['q3'] - st['q1']))}, "
                                f"{_fmt(st['q3'] + opts.outlier_multiplier * (st['q3'] - st['q1']))}]，"
                                f"实际范围 [{_fmt(st['vmin'])}, {_fmt(st['vmax'])}]"
                            ),
                            count=int(st["outliers"]),
                        )
                    )

    # 分布漂移
    shift = _mean_shift(before, after)
    if shift is not None and shift >= opts.mean_shift_ratio:
        out.append(
            Anomaly(
                level="warn",
                category="distribution_shift",
                title=f"字段 `{col.name}` 均值漂移 {shift:.0%}",
                scope="both",
                column=col.name,
                detail=(
                    f"均值 {_fmt(before['mean'])} → {_fmt(after['mean'])}，"
                    f"中位数 {_fmt(before['med'])} → {_fmt(after['med'])}"
                ),
            )
        )


def _mean_shift(before: dict, after: dict) -> Optional[float]:
    b, a = before.get("mean"), after.get("mean")
    if b is None or a is None:
        return None
    try:
        b = float(b)
        a = float(a)
    except (TypeError, ValueError):
        return None
    if b == 0:
        return None if a == 0 else 1.0
    return abs(a - b) / abs(b)


def _fmt(value) -> str:
    if value is None:
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num == int(num) and abs(num) < 1e15:
        return f"{int(num):,}"
    return f"{num:,.4f}".rstrip("0").rstrip(".")


def _categories(con, cfg: Config, col: ColumnResult, out: List[Anomaly]) -> None:
    """低基数字段的取值域变化（新增/消失的枚举值）。"""
    limit = cfg.anomaly.category_max_distinct
    before_expr = _value_expr(cfg, col, quote_ident(col.name))
    after_expr = _value_expr(cfg, col, quote_ident(col.after_name or col.name))

    before_set = _value_set(con, "before_ranked", before_expr, limit)
    after_set = _value_set(con, "after_ranked", after_expr, limit)
    if before_set is None or after_set is None:
        return

    added = after_set - before_set
    removed = before_set - after_set
    examples = cfg.anomaly.category_max_examples

    if added and len(added) <= limit:
        out.append(
            Anomaly(
                level="info",
                category="new_category",
                title=f"字段 `{col.name}` 新增 {len(added)} 个取值",
                scope="after",
                column=col.name,
                detail=_join_values(sorted(added), examples),
                count=len(added),
            )
        )
    if removed and len(removed) <= limit:
        out.append(
            Anomaly(
                level="info",
                category="missing_category",
                title=f"字段 `{col.name}` 消失 {len(removed)} 个取值",
                scope="before",
                column=col.name,
                detail=_join_values(sorted(removed), examples),
                count=len(removed),
            )
        )

    # 字段退化为常量
    if len(before_set) > 1 and len(after_set) == 1:
        only = next(iter(after_set))
        out.append(
            Anomaly(
                level="warn",
                category="constant_collapse",
                title=f"字段 `{col.name}` 在后数据集退化为单一取值",
                scope="after",
                column=col.name,
                detail=f"只剩 `{only}`（原来有 {len(before_set)} 个取值）",
            )
        )


def _value_set(con, table: str, expr: str, limit: int) -> Optional[set]:
    """取一个低基数字段的取值集合；超过 limit 说明不是低基数，返回 None。"""
    try:
        rows = con.sql(
            f"SELECT CAST({expr} AS VARCHAR) AS v, count(*) AS c "
            f"FROM {quote_ident(table)} WHERE {expr} IS NOT NULL "
            f"GROUP BY 1 LIMIT {limit + 1}"
        ).fetchall()
    except Exception:
        return None
    if len(rows) > limit:
        return None
    return {r[0] for r in rows if r[0] is not None}


def _join_values(values: Sequence[str], limit: int) -> str:
    head = list(values)[:limit]
    text = "、".join(str(v) for v in head)
    if len(values) > limit:
        text += f" …（共 {len(values)} 个）"
    return text
