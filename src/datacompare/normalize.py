"""把「值归一化 + 差异判定」翻译成 DuckDB SQL 表达式。

为什么放在 SQL 里而不是 Python 里？
因为要跑大文件：DuckDB 可以在磁盘上流式处理上亿行，Python 侧只做规则翻译，
不搬运数据。

归一化分两阶段（性能关键）
--------------------------
每个单元格的归一化只应该算一次。判定表达式如果内联求值，同一条正则链会被
重复算十几次；再叠加 DuckDB ``translate()`` 的大映射表开销，性能会崩掉。
所以：

* **阶段一** ``string_expr``：全角转半角、去/折叠空白、可选小写
* **阶段二** 由阶段一的文本派生：数值/日期/布尔解析 + 空值判定

引擎会把阶段一物化成 ``b_s<i>`` / ``a_s<i>`` 列，阶段二再基于它们生成
``b_v<i>`` / ``a_v<i>`` / ``b_n<i>`` / ``a_n<i>``。

由此带来两个必须注意的约束：

1. 值解析函数（``numeric_expr`` / ``date_expr`` / ``boolean_expr``）必须能
   直接作用在**已经做过字符串归一化**的文本上，且结果与作用在原始文本上一致。
   它们内部只做「去掉所有空白 / 去符号 / 转数字」这类幂等操作，满足该要求。
2. 空值判定用阶段一的文本，不需要重算。

核心目标是区分两类差异：

* ``VALUE_DIFF`` —— 语义变了，例如 ``100`` vs ``120``
* ``FORMAT_ONLY`` —— 只是写法不同，语义一致，例如 ``1.50`` vs ``1.5``、
  ``1,234.00`` vs ``1234``、``¥1 200`` vs ``1200``、``2024-01-01`` vs ``2024/01/01``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .config import ColumnRule, Config, DateOptions, NumericOptions, StringOptions
from .models import (
    EQUAL,
    FORMAT_ONLY,
    NULL_MISMATCH,
    TYPE_BOOLEAN,
    TYPE_DATE,
    TYPE_NUMERIC,
    TYPE_STRING,
    TYPE_MISMATCH,
    VALUE_DIFF,
    ColumnProfileLike,
)
from .sqlutil import fmt_float, quote_ident, sql_regex, sql_str

# --------------------------------------------------------------------------
# 字符集
# --------------------------------------------------------------------------
#: 全角 ASCII（！..～）→ 半角，另外把表意空格 U+3000 也映射成普通空格
FULLWIDTH_FROM = "".join(chr(0xFF01 + i) for i in range(94)) + "\u3000"
FULLWIDTH_TO = "".join(chr(0x21 + i) for i in range(94)) + " "

#: 各种 unicode 减号 → ASCII 减号（注意不要和全角表里的 U+FF0D 重复）
UNICODE_MINUS = "\u2212\u2013\u2014\u2015\u2011"

#: 需要做字符替换的全部字符（全角 + unicode 减号）
_SPECIAL_FROM = FULLWIDTH_FROM + UNICODE_MINUS
_SPECIAL_TO = FULLWIDTH_TO + "-" * len(UNICODE_MINUS)

_NBSP = "\u00a0"
_IDEO_SPACE = "\u3000"
#: 正则用的空白字符类。不要用 \uXXXX 写法，DuckDB 会报 invalid escape sequence
_WS_CLASS = "[\\s" + _NBSP + _IDEO_SPACE + "]"
_WS_ANY = _WS_CLASS + "+"

_CURRENCY_CHARS = "\u00a5\uffe5$\u20ac\u00a3\u20b9\uff04"

#: 需要做替换的字符集合（用于廉价闸门；集合内不含 ASCII 的 ``-`` 和 ``]``）
_SPECIAL_GUARD = "[" + _SPECIAL_FROM + "]"

_TRUE_TOKENS = ["true", "t", "yes", "y", "1", "是", "真", "有"]
_FALSE_TOKENS = ["false", "f", "no", "n", "0", "否", "假", "无"]

_NUMERIC_TOKEN_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

#: 日期解析的「长相闸门」。
#: ``try_strptime`` 很贵（一次要试好几种格式），所以先用一个廉价正则筛掉
#: 明显不是日期的值——像「苹果」「SO202400000」这类文本直接跳过解析。
#: 注意这里要**宽松**，把 20240105 这种没有分隔符的写法也包进来，
#: 否则用户显式指定 ``type: date`` 时这些值会解析失败。
_DATE_GATE = (
    "([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}"
    "|[0-9]{8}"
    "|[0-9]{4}\u5e74"
    "|[0-9]{4}).*"
)

#: 默认日期格式表（按出现频率排序，常见写法放前面）
_DATE_FORMATS_TIME = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
]
_DATE_FORMATS_DAY = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
]


# --------------------------------------------------------------------------
# 基础 SQL 片段
# --------------------------------------------------------------------------
def _translate_specials(expr: str) -> str:
    """全角转半角 + unicode 减号统一。

    DuckDB 的 ``translate`` 是按字符在映射表里线性查找的，
    95 个字符的映射表会让它比一次正则还慢一个数量级。
    所以先用一个廉价正则判断「到底有没有需要转换的字符」，
    绝大多数值会走 ``ELSE`` 分支直接返回原值。
    """
    guard = f"regexp_full_match({expr}, {sql_regex('.*' + _SPECIAL_GUARD + '.*')})"
    convert = (
        f"translate({expr}, {sql_str(_SPECIAL_FROM)}, {sql_str(_SPECIAL_TO)})"
    )
    return f"CASE WHEN {guard} THEN {convert} ELSE {expr} END"


def string_expr(col: str, s: StringOptions) -> str:
    """文本归一化：全角转半角 → 去掉/折叠空白 → 去首尾空白 → 可选小写。"""
    expr = col
    if s.fullwidth_to_halfwidth:
        expr = _translate_specials(expr)
    if s.ignore_whitespace:
        expr = f"regexp_replace({expr}, {sql_regex(_WS_ANY)}, '', 'g')"
    elif s.collapse_whitespace:
        # 折叠后所有的首尾空白都变成了单个空格，可以直接用原生 trim
        expr = f"trim(regexp_replace({expr}, {sql_regex(_WS_ANY)}, ' ', 'g'))"
    if s.trim:
        expr = (
            expr
            if s.collapse_whitespace
            else f"regexp_replace({expr}, {sql_regex('^' + _WS_ANY + '|' + _WS_ANY + '$')}, '', 'g')"
        )
    if s.case_insensitive:
        expr = f"lower({expr})"
    return expr


def null_flag_from_normalized(
    raw_ref: str, norm_ref: str, tokens: Sequence[str]
) -> str:
    """基于「已归一化文本」判定是否等价于空，避免重算字符串归一化。"""
    parts = [f"{raw_ref} IS NULL"]
    if tokens:
        joined = ", ".join(sql_str(t) for t in tokens)
        parts.append(f"lower(coalesce({norm_ref}, '')) IN ({joined})")
    return "(" + " OR ".join(parts) + ")"


def null_flag_expr(col: str, s: StringOptions, tokens: Sequence[str]) -> str:
    """判定一个原始文本是否等价于「空」（需要重算字符串归一化时用）。"""
    return null_flag_from_normalized(col, string_expr(col, s), tokens)


# --------------------------------------------------------------------------
# 数值
# --------------------------------------------------------------------------
def _numeric_clean(col: str, n: NumericOptions, s: StringOptions) -> str:
    """把数值文本清洗成「只含可选正负号 + 数字 + 小数点 + 可选 %」的形式。

    货币符号、空白、千分位分隔符的替换目标都是空串，所以合并成一次正则，
    比拆成三次 ``regexp_replace`` 快得多。
    """
    expr = f"NULLIF(trim({col}), '')"
    if s.fullwidth_to_halfwidth:
        expr = _translate_specials(expr)

    junk = []
    if n.strip_currency:
        junk.append("[" + _CURRENCY_CHARS + "]")
    junk.append(_WS_ANY)
    if n.ignore_thousands_separator:
        # 欧洲写法下小数点是逗号，此时「千分位」是点号
        junk.append(r"\." if n.decimal_separator == "," else ",")
    expr = f"regexp_replace({expr}, {sql_regex('(' + '|'.join(junk) + ')')}, '', 'g')"

    # 会计写法负数 (123) -> -123（少数行才会走真分支，所以先做廉价 LIKE 判断）
    expr = (
        f"CASE WHEN {expr} LIKE '(%' AND {expr} LIKE '%)'"
        f" THEN '-' || substr({expr}, 2, length({expr}) - 2)"
        f" ELSE {expr} END"
    )
    if n.decimal_separator == ",":
        expr = f"replace({expr}, ',', '.')"
    return expr


def numeric_expr(col: str, n: NumericOptions, s: StringOptions) -> str:
    """数值归一化。解析失败返回 NULL（不会抛错）。"""
    clean = _numeric_clean(col, n, s)
    no_pct = f"regexp_replace({clean}, {sql_regex('%$')}, '')"
    cast = f"TRY_CAST({no_pct} AS DOUBLE)"
    if n.percent_mode == "ratio":
        return f"CASE WHEN {clean} LIKE '%' THEN ({cast}) / 100.0 ELSE {cast} END"
    return cast


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
def date_expr(col: str, d: DateOptions, s: StringOptions, formats: Sequence[str]) -> str:
    """日期归一化：先用长相闸门粗筛，再按「带时间 / 只带日期」分组尝试格式。

    ``try_strptime`` 单次约 2 微秒，十几条格式串起来对整表来说很贵，
    所以这里做两层剪枝：闸门 + 按是否有 ``:`` 分成两组。
    """
    expr = f"NULLIF(trim({col}), '')"
    if s.fullwidth_to_halfwidth:
        expr = _translate_specials(expr)
    expr = f"trim(regexp_replace({expr}, {sql_regex(_WS_ANY)}, ' ', 'g'))"

    custom = list(formats)
    extra = [f for f in custom if "%H" in f or "%M" in f or "%S" in f]
    day_only = [f for f in custom if f not in extra]

    time_fmts = _dedup(_DATE_FORMATS_TIME + extra)
    day_fmts = _dedup(_DATE_FORMATS_DAY + day_only)

    def attempt(fmt_list):
        calls = [f"try_strptime({expr}, {sql_str(f)})" for f in fmt_list]
        calls.append(f"TRY_CAST({expr} AS TIMESTAMP)")
        return "coalesce(" + ", ".join(calls) + ")"

    has_time = f"({expr} LIKE '%:%')"
    mid = f"CASE WHEN {has_time} THEN {attempt(time_fmts)} ELSE {attempt(day_fmts)} END"
    gate = f"regexp_full_match({expr}, {sql_regex(_DATE_GATE)})"
    parsed = f"CASE WHEN {gate} THEN {mid} ELSE NULL END"

    if d.granularity == "date":
        return f"CAST({parsed} AS DATE)"
    if d.granularity == "minute":
        return f"date_trunc('minute', {parsed})"
    return parsed


def _dedup(items: Sequence[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 布尔
# --------------------------------------------------------------------------
def boolean_expr(col: str, s: StringOptions) -> str:
    """布尔归一化，支持 Y/N、是/否、1/0。"""
    base = f"lower({string_expr(col, s)})"
    t = ", ".join(sql_str(v) for v in _TRUE_TOKENS)
    f = ", ".join(sql_str(v) for v in _FALSE_TOKENS)
    return (
        f"CASE WHEN {base} IN ({t}) THEN TRUE "
        f"WHEN {base} IN ({f}) THEN FALSE ELSE NULL END"
    )


def value_expr(col: str, ctype: str, cfg: Config, rule: Optional[ColumnRule],
               formats: Sequence[str], granularity: str) -> str:
    """按类型生成「归一化后的值」表达式（输入应当是阶段一的文本）。"""
    if ctype == TYPE_NUMERIC:
        return numeric_expr(col, cfg.compare.numeric, _effective_string_opts(cfg, rule))
    if ctype == TYPE_DATE:
        opts = DateOptions(
            enabled=cfg.compare.date.enabled,
            day_first=cfg.compare.date.day_first,
            granularity=granularity,
            tolerance_seconds=cfg.compare.date.tolerance_seconds,
            formats=cfg.compare.date.formats,
        )
        return date_expr(col, opts, _effective_string_opts(cfg, rule), formats)
    if ctype == TYPE_BOOLEAN:
        return boolean_expr(col, _effective_string_opts(cfg, rule))
    return col


# --------------------------------------------------------------------------
# 单列比对计划
# --------------------------------------------------------------------------
@dataclass
class NormSpec:
    """一个字段两侧的归一化片段（拆成两阶段，便于物化后复用）。"""

    raw_b: str          # 阶段一：原始文本
    raw_a: str
    str_b: str          # 阶段一：字符串归一化结果
    str_a: str
    val_b: str = ""     # 阶段二：归一化后的值（引用阶段一列）
    val_a: str = ""
    null_b: str = ""    # 阶段二：是否等价于空
    null_a: str = ""


@dataclass
class ColumnPlan:
    """一个字段的完整比对计划。"""

    name: str                    # 显示名（before 侧列名）
    after_name: str              # after 侧列名
    ctype: str                   # string | numeric | date | boolean
    index: int                   # 在 paired 表里的列序号
    spec: NormSpec
    status_expr: str = ""
    abs_diff_expr: Optional[str] = None
    rel_diff_expr: Optional[str] = None
    notes: str = ""              # 推断依据说明

    # -- 物化列别名 ------------------------------------------------------
    def stage1_columns(self) -> List[Tuple[str, str]]:
        i = self.index
        return [
            (f"b_r{i}", self.spec.raw_b),
            (f"a_r{i}", self.spec.raw_a),
            (f"b_s{i}", self.spec.str_b),
            (f"a_s{i}", self.spec.str_a),
        ]

    def stage2_columns(self) -> List[Tuple[str, str]]:
        i = self.index
        return [
            (f"b_v{i}", self.spec.val_b),
            (f"a_v{i}", self.spec.val_a),
            (f"b_n{i}", self.spec.null_b),
            (f"a_n{i}", self.spec.null_a),
        ]

    @property
    def raw_refs(self) -> Tuple[str, str]:
        return (f'p.{quote_ident("b_r" + str(self.index))}',
                f'p.{quote_ident("a_r" + str(self.index))}')

    @property
    def val_refs(self) -> Tuple[str, str]:
        return (f'p.{quote_ident("b_v" + str(self.index))}',
                f'p.{quote_ident("a_v" + str(self.index))}')

    @property
    def null_refs(self) -> Tuple[str, str]:
        return (f'p.{quote_ident("b_n" + str(self.index))}',
                f'p.{quote_ident("a_n" + str(self.index))}')


def normalize_pair(
    before_ref: str,
    after_ref: str,
    ctype: str,
    cfg: Config,
    *,
    rule: Optional[ColumnRule] = None,
    detected_time: bool = False,
) -> NormSpec:
    """为两侧字段生成归一化 SQL 片段（不做任何「比较」）。"""
    s = _effective_string_opts(cfg, rule)

    str_b = string_expr(before_ref, s)
    str_a = string_expr(after_ref, s)

    return NormSpec(
        raw_b=f"CAST({before_ref} AS VARCHAR)",
        raw_a=f"CAST({after_ref} AS VARCHAR)",
        str_b=str_b,
        str_a=str_a,
        val_b="",   # 阶段二由引擎填入（引用阶段一的列）
        val_a="",
        null_b="",
        null_a="",
    )


def link_stage2(
    plan: ColumnPlan,
    cfg: Config,
    *,
    rule: Optional[ColumnRule] = None,
    detected_time: bool = False,
) -> None:
    """把计划的阶段二表达式接到阶段一物化出来的列上。"""
    i = plan.index
    raw_b, raw_a = plan.raw_refs
    s_b = f'p.{quote_ident("b_s" + str(i))}'
    s_a = f'p.{quote_ident("a_s" + str(i))}'
    formats = rule.date_formats if (rule and rule.date_formats) else cfg.date_formats()
    granularity = _effective_granularity(cfg, rule, plan.ctype, detected_time)
    tokens = cfg.null_tokens(rule)

    plan.spec.val_b = value_expr(s_b, plan.ctype, cfg, rule, formats, granularity)
    plan.spec.val_a = value_expr(s_a, plan.ctype, cfg, rule, formats, granularity)
    plan.spec.null_b = null_flag_from_normalized(raw_b, s_b, tokens)
    plan.spec.null_a = null_flag_from_normalized(raw_a, s_a, tokens)


def build_status_expr(
    plan: ColumnPlan,
    cfg: Config,
    *,
    rule: Optional[ColumnRule] = None,
    detected_time: bool = False,
) -> None:
    """生成状态判定表达式（全部引用物化列，不重复计算归一化）。"""
    b_null, a_null = plan.null_refs
    b_val, a_val = plan.val_refs
    b_raw, a_raw = plan.raw_refs
    ctype = plan.ctype

    if ctype == TYPE_STRING:
        plan.status_expr = (
            f"CASE WHEN {b_null} AND {a_null} THEN '{EQUAL}'"
            f" WHEN {b_null} OR {a_null} THEN '{NULL_MISMATCH}'"
            f" WHEN {b_raw} = {a_raw} THEN '{EQUAL}'"
            f" WHEN {b_val} IS NOT DISTINCT FROM {a_val} THEN '{FORMAT_ONLY}'"
            f" ELSE '{VALUE_DIFF}' END"
        )
        return

    tol = None
    if ctype == TYPE_DATE:
        granularity = _effective_granularity(cfg, rule, ctype, detected_time)
        seconds = cfg.compare.date.tolerance_seconds
        if seconds > 0 and granularity != "date":
            tol = _date_tolerance(b_val, a_val, seconds)
        plan.abs_diff_expr = (
            f"abs(epoch(CAST({a_val} AS TIMESTAMP)) - epoch(CAST({b_val} AS TIMESTAMP)))"
        )
    elif ctype == TYPE_NUMERIC:
        abs_tol, rel_tol = _effective_tolerances(cfg, rule)
        tol = _numeric_tolerance(b_val, a_val, abs_tol, rel_tol)
        plan.abs_diff_expr = f"abs({a_val} - {b_val})"
        plan.rel_diff_expr = (
            f"CASE WHEN {b_val} IS NULL OR {b_val} = 0 THEN NULL "
            f"ELSE ({a_val} - {b_val}) / abs({b_val}) END"
        )

    branches = [
        f"WHEN {b_null} AND {a_null} THEN '{EQUAL}'",
        f"WHEN {b_null} OR {a_null} THEN '{NULL_MISMATCH}'",
        f"WHEN {b_raw} = {a_raw} THEN '{EQUAL}'",
        f"WHEN {b_val} IS NULL OR {a_val} IS NULL THEN '{TYPE_MISMATCH}'",
        f"WHEN {b_val} = {a_val} THEN '{FORMAT_ONLY}'",
    ]
    if tol:
        branches.append(f"WHEN {tol} THEN '{FORMAT_ONLY}'")
    branches.append(f"ELSE '{VALUE_DIFF}'")
    plan.status_expr = "CASE " + " ".join(branches) + " END"


def build_column_plan(
    name: str,
    after_name: str,
    ctype: str,
    index: int,
    cfg: Config,
    *,
    before_ref: str,
    after_ref: str,
    rule: Optional[ColumnRule] = None,
    detected_time: bool = False,
) -> ColumnPlan:
    """为一个字段生成完整比对计划（含两阶段归一化与状态判定）。"""
    spec = normalize_pair(
        before_ref, after_ref, ctype, cfg, rule=rule, detected_time=detected_time
    )
    plan = ColumnPlan(
        name=name, after_name=after_name, ctype=ctype, index=index, spec=spec
    )
    link_stage2(plan, cfg, rule=rule, detected_time=detected_time)
    build_status_expr(plan, cfg, rule=rule, detected_time=detected_time)
    return plan


# --------------------------------------------------------------------------
# 比较选项与小工具
# --------------------------------------------------------------------------
def _effective_string_opts(cfg: Config, rule: Optional[ColumnRule]) -> StringOptions:
    base = cfg.compare.string
    if rule is None:
        return base
    return StringOptions(
        trim=base.trim if rule.trim is None else rule.trim,
        collapse_whitespace=(
            base.collapse_whitespace
            if rule.collapse_whitespace is None
            else rule.collapse_whitespace
        ),
        ignore_whitespace=base.ignore_whitespace,
        case_insensitive=(
            base.case_insensitive
            if rule.case_insensitive is None
            else rule.case_insensitive
        ),
        fullwidth_to_halfwidth=base.fullwidth_to_halfwidth,
        null_equals_empty=base.null_equals_empty,
        null_tokens=base.null_tokens,
    )


def _effective_tolerances(cfg: Config, rule: Optional[ColumnRule]) -> Tuple[float, float]:
    abs_tol = cfg.compare.numeric.abs_tol
    rel_tol = cfg.compare.numeric.rel_tol
    if rule is not None:
        if rule.abs_tol is not None:
            abs_tol = rule.abs_tol
        if rule.rel_tol is not None:
            rel_tol = rule.rel_tol
    return float(abs_tol), float(rel_tol)


def _effective_granularity(
    cfg: Config, rule: Optional[ColumnRule], ctype: str, detected_time: bool
) -> str:
    if rule is not None and rule.granularity:
        return rule.granularity
    configured = cfg.compare.date.granularity
    if configured != "auto":
        return configured
    # 两侧都是纯日期（不含时分秒）时，按「天」比较，避免 00:00:00 噪声
    return "date" if ctype == TYPE_DATE and not detected_time else "timestamp"


def _numeric_tolerance(
    b_val: str, a_val: str, abs_tol: float, rel_tol: float
) -> Optional[str]:
    if abs_tol <= 0 and rel_tol <= 0:
        return None
    diff = f"abs({a_val} - {b_val})"
    conds = []
    if abs_tol > 0:
        conds.append(f"{diff} <= {fmt_float(abs_tol)}")
    if rel_tol > 0:
        scale = f"greatest(abs({b_val}), abs({a_val}))"
        conds.append(f"{diff} <= {fmt_float(rel_tol)} * {scale}")
    return "(" + " OR ".join(conds) + ")"


def _date_tolerance(b_val: str, a_val: str, seconds: float) -> Optional[str]:
    if seconds <= 0:
        return None
    return (
        f"abs(epoch(CAST({a_val} AS TIMESTAMP)) - epoch(CAST({b_val} AS TIMESTAMP)))"
        f" <= {fmt_float(seconds)}"
    )


# --------------------------------------------------------------------------
# 类型推断
# --------------------------------------------------------------------------
def infer_type(
    before: Optional["ColumnProfileLike"],
    after: Optional["ColumnProfileLike"],
    cfg: Config,
    rule: Optional[ColumnRule] = None,
) -> Tuple[str, str]:
    """根据两侧字段画像推断比较方式。

    返回 ``(类型, 说明)``。有意保守：能当文本就当文本，
    避免把 ``001`` 当成数字 ``1``、把 18 位身份证号当浮点数。
    """
    if rule is not None and rule.type:
        return rule.type, "配置指定"

    profiles = [p for p in (before, after) if p is not None and p.total > 0]
    if not profiles:
        return TYPE_STRING, "无数据，按文本比较"

    if not cfg.compare.detect_types:
        return TYPE_STRING, "已关闭自动类型推断"

    numeric_ok = sum(p.numeric_ok for p in profiles)
    numeric_try = sum(p.numeric_ok + p.numeric_fail for p in profiles)
    numeric_ratio = numeric_ok / numeric_try if numeric_try else 0.0

    leading_zero = sum(p.leading_zero for p in profiles)
    max_digits = max((p.max_digits for p in profiles), default=0)

    if cfg.compare.date.enabled:
        date_ok = sum(p.date_ok for p in profiles)
        date_try = sum(p.date_ok + p.date_fail for p in profiles)
        date_ratio = date_ok / date_try if date_try else 0.0
        # 只对「看起来就是日期」的值统计，避免把 20240101 之外的纯数字误判
        date_like = sum(p.date_like for p in profiles)
        date_like_ratio = date_like / date_try if date_try else 0.0
        if (
            date_try > 0
            and date_ratio >= 0.98
            and date_like_ratio >= 0.9
            and numeric_ratio < 0.98
        ):
            return TYPE_DATE, "两侧绝大多数值可解析为日期"

    if numeric_try > 0 and numeric_ratio >= 0.98:
        if cfg.compare.leading_zero_is_text and leading_zero > 0:
            return (
                TYPE_STRING,
                f"存在带前导零的值（{leading_zero} 个），按文本比较以免 001 与 1 混淆",
            )
        if max_digits > cfg.compare.max_digits_for_number:
            return (
                TYPE_STRING,
                f"最长整数位数 {max_digits} 超过 {cfg.compare.max_digits_for_number}，"
                "按文本比较以免浮点丢精度",
            )
        return TYPE_NUMERIC, "两侧绝大多数值可解析为数值"

    return TYPE_STRING, "按文本比较"
