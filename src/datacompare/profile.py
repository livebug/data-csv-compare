"""字段画像（profiling）与主键推断。

画像的作用是「决定每个字段该怎么比」：
  * 全是数字（且没有前导零、没有超长整数）→ 按数值比，容忍小数位数差异
  * 看起来是日期 → 解析成时间戳再比，容忍 2024-01-01 / 2024/01/01 的写法差异
  * 其它 → 按文本比

同时画像数据也直接喂给异常检测（空值率、取值分布变化）。

性能设计
--------
日期解析（十几种 ``try_strptime``）是整个画像里最贵的一步，所以分两阶段：

* **阶段一**：只算廉价指标（空值、取值数、数值可解析数、日期长相、样例值）
* **阶段二**：只对「确实长得像日期」的字段真正去解析

一张 20 列的宽表里通常只有 1-2 列需要跑阶段二，整体快一个数量级。
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

from .config import Config, KeyOptions
from .models import ColumnProfile
from .normalize import (
    FULLWIDTH_FROM,
    FULLWIDTH_TO,
    _DATE_GATE,
    _WS_ANY,
    _effective_string_opts,
    date_expr,
    null_flag_expr,
    numeric_expr,
    string_expr,
)
from .sqlutil import quote_ident, sql_regex, sql_str

#: 一次性查询里最多塞多少个字段的聚合（太多会让执行计划过于庞大）
_PROFILE_CHUNK = 12

#: 主键候选列名特征
_KEY_NAME_PATTERNS = [
    re.compile(r"^(id|pk|uuid|guid)$", re.I),
    re.compile(r"(_id|_key|_no|_code|_sn|编号|号码|单号|序号|主键|编码|代码)$", re.I),
    re.compile(r"^(id_|key_|code_|no_)", re.I),
    re.compile(r"号$"),
]

#: 唯一性预筛：取值个数至少要达到行数的这个比例，才值得做精确唯一性检查
_UNIQUE_PREFILTER_RATIO = 0.5

#: 「看起来像日期」——必须带分隔符，避免把 20240101 这种纯数字误判成日期
_DATE_LIKE_PATTERN = r"[0-9]{4}[-/.年][0-9]{1,2}([-/.月][0-9]{1,2}日?)?"

#: 阶段二候选门槛：至少这个比例的非空值能通过日期长相闸门才值得真去解析
_DATE_PARSE_CANDIDATE_RATIO = 0.3

#: 阶段一每个字段的聚合个数
_METRICS = 10


def profile_table(
    con,
    table: str,
    columns: Sequence[str],
    cfg: Config,
) -> Tuple[Dict[str, ColumnProfile], List[str]]:
    """对一张表做字段画像。返回 ``{列名: 画像}``。"""
    profiles: Dict[str, ColumnProfile] = {}
    if not columns:
        return profiles, []

    total_rows = int(con.sql(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()[0])
    if total_rows == 0:
        for col in columns:
            profiles[col] = ColumnProfile(name=col)
        return profiles, []

    exact = total_rows <= cfg.profile_exact_distinct_limit
    numeric = cfg.compare.numeric
    date_formats = cfg.date_formats()

    # ---------------- 阶段一：廉价指标 ---------------------------------
    for start in range(0, len(columns), _PROFILE_CHUNK):
        chunk = list(columns[start : start + _PROFILE_CHUNK])
        exprs: List[str] = []
        for idx, col in enumerate(chunk):
            ref = quote_ident(col)
            rule = cfg.rule(col)
            eff = _effective_string_opts(cfg, rule)
            tokens = cfg.null_tokens(rule)
            is_null = null_flag_expr(ref, eff, tokens)
            text = string_expr(ref, eff)
            num = numeric_expr(ref, numeric, eff)
            distinct_expr = (
                f"count(DISTINCT {text})" if exact else f"approx_count_distinct({text})"
            )
            p = f'"__p{idx}__'
            exprs.extend(
                [
                    f"count(*) FILTER (WHERE {is_null}) AS {p}nulls\"",
                    f"{distinct_expr} AS {p}distinct\"",
                    f"count(*) FILTER (WHERE NOT {is_null} AND {num} IS NOT NULL) AS {p}num_ok\"",
                    f"count(*) FILTER (WHERE NOT {is_null} AND regexp_full_match({text}, "
                    f"{sql_regex('[+-]?0[0-9].*')})) AS {p}leading_zero\"",
                    # 只数「整数部分的位数」：18 位身份证号、银行卡号这类长整数才需要
                    # 退回文本比较；小数 0.10000000000000001 不该被误伤
                    f"max(length(regexp_replace(split_part("
                    f"regexp_replace({text}, {sql_regex('^[+-]')}, ''), '.', 1), "
                    f"{sql_regex('[^0-9]')}, '', 'g'))) AS {p}max_digits\"",
                    f"count(*) FILTER (WHERE NOT {is_null} AND regexp_full_match({text}, "
                    f"{sql_regex('.*[0-9]{1,2}:[0-9]{2}.*')})) AS {p}has_time\"",
                    f"count(*) FILTER (WHERE NOT {is_null} AND regexp_full_match({text}, "
                    f"{sql_regex(_DATE_LIKE_PATTERN + '.*')})) AS {p}date_like\"",
                    f"count(*) FILTER (WHERE NOT {is_null} AND regexp_full_match({text}, "
                    f"{sql_regex(_DATE_GATE)})) AS {p}date_gate\"",
                    f"min({text}) AS {p}sample_min\"",
                    f"max({text}) AS {p}sample_max\"",
                ]
            )

        sql = (
            "SELECT count(*) AS __total, "
            + ",\n       ".join(exprs)
            + f"\nFROM {quote_ident(table)}"
        )
        row = con.sql(sql).fetchone()
        for idx, col in enumerate(chunk):
            base = 1 + idx * _METRICS
            nulls = int(row[base] or 0)
            non_null = total_rows - nulls
            num_ok = int(row[base + 2] or 0)
            prof = ColumnProfile(
                name=col,
                total=total_rows,
                nulls=nulls,
                distinct_vals=int(row[base + 1] or 0),
                numeric_ok=num_ok,
                numeric_fail=max(non_null - num_ok, 0),
                leading_zero=int(row[base + 3] or 0),
                max_digits=int(row[base + 4] or 0),
                has_time_part=int(row[base + 5] or 0),
                date_like=int(row[base + 6] or 0),
                date_gate=int(row[base + 7] or 0),
            )
            lo, hi = row[base + 8], row[base + 9]
            if lo is not None or hi is not None:
                prof.samples = [str(v) for v in (lo, hi) if v is not None]
            profiles[col] = prof

    # ---------------- 阶段二：只对疑似日期的字段做解析 -------------------
    candidates = [
        col
        for col in columns
        if profiles[col].non_null > 0
        and profiles[col].date_gate >= profiles[col].non_null * _DATE_PARSE_CANDIDATE_RATIO
    ]
    for start in range(0, len(candidates), _PROFILE_CHUNK):
        chunk = candidates[start : start + _PROFILE_CHUNK]
        exprs = []
        for idx, col in enumerate(chunk):
            rule = cfg.rule(col)
            eff = _effective_string_opts(cfg, rule)
            expr = date_expr(quote_ident(col), cfg.compare.date, eff, date_formats)
            exprs.append(f'count(*) FILTER (WHERE {expr} IS NOT NULL) AS "__d{idx}__"')
        row = con.sql(f"SELECT {', '.join(exprs)} FROM {quote_ident(table)}").fetchone()
        for idx, col in enumerate(chunk):
            prof = profiles[col]
            ok = int(row[idx] or 0)
            prof.date_ok = ok
            prof.date_fail = max(prof.non_null - ok, 0)

    return profiles, []


# --------------------------------------------------------------------------
# 主键推断
# --------------------------------------------------------------------------
def build_key_expr(cols: Sequence[str], opts: KeyOptions) -> str:
    """把主键列拼成一个可比较的字符串键。

    使用 ``chr(31)``（单元分隔符）做拼接分隔，避免 ``('a','bc')`` 与
    ``('ab','c')`` 撞成同一个键。
    """
    parts: List[str] = []
    for col in cols:
        expr = quote_ident(col)
        if opts.fullwidth_to_halfwidth:
            expr = f"translate({expr}, {sql_str(FULLWIDTH_FROM)}, {sql_str(FULLWIDTH_TO)})"
        if opts.trim:
            expr = f"regexp_replace({expr}, {sql_regex('^' + _WS_ANY + '|' + _WS_ANY + '$')}, '', 'g')"
        if opts.numeric_normalize:
            # 把 001 / 1 / 1.0 归一成同一个键
            expr = f"coalesce(CAST(TRY_CAST({expr} AS DOUBLE) AS VARCHAR), {expr})"
        if opts.case_insensitive:
            expr = f"lower({expr})"
        parts.append(f"coalesce({expr}, chr(0))")
    return "concat_ws(chr(31), " + ", ".join(parts) + ")"


def key_is_unique(
    con, table: str, cols: Sequence[str], opts: KeyOptions
) -> Tuple[bool, int, int]:
    """检查一组列能否唯一标识行。

    返回 ``(是否唯一, 总行数, 非空行数)``。
    """
    if not cols:
        return False, 0, 0
    expr = build_key_expr(cols, opts)
    sql = f"""
        SELECT
          (SELECT count(*) FROM {quote_ident(table)}) AS total,
          (SELECT count(*) FROM (
              SELECT {expr} AS __k FROM {quote_ident(table)}
              WHERE {" AND ".join(f"{quote_ident(c)} IS NOT NULL" for c in cols)}
              GROUP BY 1
          )) AS uniq,
          (SELECT count(*) FROM {quote_ident(table)}
           WHERE {" OR ".join(f"{quote_ident(c)} IS NULL" for c in cols)}) AS with_null
        FROM (SELECT 1) t
    """
    total, uniq, with_null = con.sql(sql).fetchone()
    total = int(total or 0)
    uniq = int(uniq or 0)
    with_null = int(with_null or 0)
    non_null = total - with_null
    is_uniq = total > 0 and with_null == 0 and uniq == total
    return is_uniq, total, non_null


def detect_keys(
    con,
    table: str,
    common_cols: Sequence[str],
    profiles: Dict[str, ColumnProfile],
    cfg: Config,
) -> Tuple[List[str], str, List[str]]:
    """自动推断主键。

    返回 ``(主键列, 说明, 警告列表)``。找不到时返回空列表。
    """
    warnings: List[str] = []
    opts = cfg.key_options
    total = int(con.sql(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()[0] or 0)
    if total == 0:
        return [], "表为空", warnings

    named: List[str] = []
    unique_like: List[str] = []
    for col in common_cols:
        prof = profiles.get(col)
        if prof is None or prof.total == 0:
            continue
        if prof.nulls > 0:
            continue
        # 注意：approx_count_distinct 有误差，这里只做粗筛，
        # 真正确认唯一性靠下面的 key_is_unique（精确 GROUP BY）
        high_cardinality = prof.distinct_vals >= total * _UNIQUE_PREFILTER_RATIO
        if _looks_like_key_name(col):
            named.append(col)
        if high_cardinality:
            unique_like.append(col)

    # 1) 名字像主键、且高基数
    for col in named:
        if col in unique_like:
            ok, _, _ = key_is_unique(con, table, [col], opts)
            if ok:
                return [col], f"自动识别：`{col}` 名称像主键且在数据中唯一", warnings

    # 2) 任意高基数列，优先取值个数更多的
    candidates = sorted(
        unique_like,
        key=lambda c: (profiles[c].distinct_vals, c),
        reverse=True,
    )
    for col in candidates:
        ok, _, _ = key_is_unique(con, table, [col], opts)
        if ok:
            return [col], f"自动识别：`{col}` 在数据中唯一", warnings

    # 3) 名字像主键的列组合
    if 2 <= len(named) <= 4:
        ok, _, _ = key_is_unique(con, table, named, opts)
        if ok:
            joined = ", ".join(f"`{c}`" for c in named)
            return list(named), f"自动识别：({joined}) 组合唯一", warnings

    warnings.append(
        "无法自动识别主键（没有找到唯一且非空的列）。"
        "请在配置里用 keys 显式指定，或开启 key_options.use_row_index_if_no_key 按行号对比。"
    )
    return [], "", warnings


def _looks_like_key_name(col: str) -> bool:
    return any(p.search(col) for p in _KEY_NAME_PATTERNS)
