"""运行期数据模型：对比结果、字段统计、异常记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

# --------------------------------------------------------------------------
# 单元格对比状态
# --------------------------------------------------------------------------
EQUAL = "EQUAL"                    # 完全一致（原始文本都一样）
FORMAT_ONLY = "FORMAT_ONLY"        # 仅格式不同，语义一致（补零 / 千分位 / 空白 / 大小写 / 日期写法）
VALUE_DIFF = "VALUE_DIFF"          # 值确实不同
NULL_MISMATCH = "NULL_MISMATCH"    # 一侧为空一侧有值
TYPE_MISMATCH = "TYPE_MISMATCH"    # 一侧能解析成数字/日期，另一侧不能

CELL_STATUSES = [EQUAL, FORMAT_ONLY, VALUE_DIFF, NULL_MISMATCH, TYPE_MISMATCH]

#: 真正需要人工关注的差异类型（FORMAT_ONLY 不算）
SEVERE_STATUSES = [VALUE_DIFF, NULL_MISMATCH, TYPE_MISMATCH]

STATUS_LABELS: Dict[str, str] = {
    EQUAL: "一致",
    FORMAT_ONLY: "仅格式差异",
    VALUE_DIFF: "值不同",
    NULL_MISMATCH: "空值不一致",
    TYPE_MISMATCH: "类型/格式异常",
}

STATUS_COLORS: Dict[str, str] = {
    EQUAL: "#16a34a",
    FORMAT_ONLY: "#d97706",
    VALUE_DIFF: "#dc2626",
    NULL_MISMATCH: "#9333ea",
    TYPE_MISMATCH: "#0891b2",
}

# --------------------------------------------------------------------------
# 行状态
# --------------------------------------------------------------------------
ROW_SAME = "SAME"
ROW_CHANGED = "CHANGED"
ROW_ONLY_IN_BEFORE = "ONLY_IN_BEFORE"
ROW_ONLY_IN_AFTER = "ONLY_IN_AFTER"

ROW_STATUS_LABELS: Dict[str, str] = {
    ROW_SAME: "完全一致",
    ROW_CHANGED: "存在差异",
    ROW_ONLY_IN_BEFORE: "仅前数据集存在",
    ROW_ONLY_IN_AFTER: "仅后数据集存在",
}

# --------------------------------------------------------------------------
# 字段比对方式
# --------------------------------------------------------------------------
MODE_COMPARED = "compared"
MODE_IGNORED = "ignored"
MODE_KEY = "key"
MODE_ONLY_BEFORE = "only_before"
MODE_ONLY_AFTER = "only_after"

TYPE_STRING = "string"
TYPE_NUMERIC = "numeric"
TYPE_DATE = "date"
TYPE_BOOLEAN = "boolean"

TYPE_LABELS: Dict[str, str] = {
    TYPE_STRING: "文本",
    TYPE_NUMERIC: "数值",
    TYPE_DATE: "日期",
    TYPE_BOOLEAN: "布尔",
}


@dataclass
class ColumnProfile:
    """单侧字段画像，用于自动推断如何比较。"""

    name: str
    total: int = 0
    nulls: int = 0
    distinct_vals: int = 0
    numeric_ok: int = 0
    numeric_fail: int = 0
    date_ok: int = 0
    date_fail: int = 0
    date_like: int = 0       # 长得像日期（带分隔符）的值的个数
    date_gate: int = 0       # 通过宽松日期闸门、值得尝试解析的值的个数
    leading_zero: int = 0
    max_digits: int = 0
    has_time_part: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def non_null(self) -> int:
        return max(self.total - self.nulls, 0)

    @property
    def null_rate(self) -> float:
        return (self.nulls / self.total) if self.total else 0.0


class ColumnProfileLike(Protocol):
    """:class:`ColumnProfile` 的结构化类型，供类型推断函数使用。"""

    total: int
    numeric_ok: int
    numeric_fail: int
    date_ok: int
    date_fail: int
    date_like: int
    date_gate: int
    leading_zero: int
    max_digits: int
    has_time_part: int


@dataclass
class ColumnResult:
    """一个字段的前后对比结论。"""

    name: str
    after_name: str = ""
    ctype: str = TYPE_STRING
    mode: str = MODE_COMPARED
    matched_rows: int = 0
    equal: int = 0
    format_only: int = 0
    value_diff: int = 0
    null_mismatch: int = 0
    type_mismatch: int = 0
    null_rate_before: float = 0.0
    null_rate_after: float = 0.0
    distinct_before: int = 0
    distinct_after: int = 0
    note: str = ""

    @property
    def diff_total(self) -> int:
        return (
            self.format_only
            + self.value_diff
            + self.null_mismatch
            + self.type_mismatch
        )

    @property
    def severe_total(self) -> int:
        return self.value_diff + self.null_mismatch + self.type_mismatch

    @property
    def severe_rate(self) -> float:
        return (self.severe_total / self.matched_rows) if self.matched_rows else 0.0

    @property
    def null_rate_delta(self) -> float:
        return self.null_rate_after - self.null_rate_before


@dataclass
class Stats:
    """整体统计。"""

    before_rows: int = 0
    after_rows: int = 0
    matched_pairs: int = 0
    same_rows: int = 0
    changed_rows: int = 0
    only_in_before: int = 0
    only_in_after: int = 0
    dup_key_before: int = 0
    dup_key_after: int = 0
    total_columns: int = 0
    compared_columns: int = 0
    cell_diffs: int = 0
    severe_cell_diffs: int = 0
    format_only_cells: int = 0
    key_columns: List[str] = field(default_factory=list)
    key_strategy: str = ""
    before_columns: List[str] = field(default_factory=list)
    after_columns: List[str] = field(default_factory=list)
    only_before_columns: List[str] = field(default_factory=list)
    only_after_columns: List[str] = field(default_factory=list)
    before_path: str = ""
    after_path: str = ""

    @property
    def row_delta(self) -> int:
        return self.after_rows - self.before_rows

    @property
    def row_delta_rate(self) -> float:
        return (self.row_delta / self.before_rows) if self.before_rows else 0.0

    @property
    def consistent(self) -> bool:
        """数据是否没有实质问题（仅格式差异不算问题）。"""
        return (
            self.severe_cell_diffs == 0
            and self.only_in_before == 0
            and self.only_in_after == 0
            and self.dup_key_before == 0
            and self.dup_key_after == 0
        )


@dataclass
class Anomaly:
    """一条异常数据记录。"""

    level: str                    # info | warn | error
    category: str                 # 异常类别（机器可读）
    title: str                    # 一句话描述
    scope: str = ""               # before | after | both
    column: str = ""
    detail: str = ""
    value: str = ""
    key_value: str = ""
    count: int = 0


ANOMALY_CATEGORY_LABELS: Dict[str, str] = {
    "schema_change": "字段增减",
    "row_count_change": "行数变化",
    "duplicate_key": "主键重复",
    "unmatched_key": "主键无法匹配",
    "null_rate_change": "空值率突变",
    "numeric_parse_failure": "数值列脏数据",
    "date_parse_failure": "日期列脏数据",
    "numeric_outlier": "数值离群点",
    "distribution_shift": "分布漂移",
    "new_category": "新增取值",
    "missing_category": "消失取值",
    "constant_collapse": "字段退化为常量",
    "case_only_change": "仅大小写变化",
    "whitespace_change": "仅空白变化",
}

LEVEL_LABELS: Dict[str, str] = {
    "info": "提示",
    "warn": "警告",
    "error": "严重",
}


@dataclass
class CompareResult:
    """一次完整对比的产物，报告层直接消费这个对象。"""

    stats: Stats
    columns: List[ColumnResult] = field(default_factory=list)
    anomalies: List[Anomaly] = field(default_factory=list)
    db_path: Optional[str] = None
    con: Any = None
    warnings: List[str] = field(default_factory=list)

    def column(self, name: str) -> Optional[ColumnResult]:
        for col in self.columns:
            if col.name == name:
                return col
        return None
