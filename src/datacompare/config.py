"""配置模型与 YAML/JSON 加载。

设计原则：所有开关都有合理默认值，用户不写配置文件也能跑起来，
配置文件只用来覆盖关心的部分。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------

#: 默认视为「空」的文本（NULL/NA 之类），大小写不敏感的匹配在归一化后做
DEFAULT_NULL_TOKENS: List[str] = [
    "", "null", "none", "nan", "na", "n/a", "n.a.", "#n/a", "<na>",
    "nil", "undefined", "-", "--", "—", "－", "无", "空", "未知",
]

DEFAULT_DATE_FORMATS: List[str] = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日",
]

DEFAULT_DATE_FORMATS_MD: List[str] = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y年%m月%d日",
]

KIND_BY_EXT: Dict[str, str] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".tab": "csv",
    ".txt": "csv",
    ".dat": "csv",
    ".psv": "csv",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xltx": "excel",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}


def _dc(cls, data: Optional[Dict[str, Any]]):
    """按 dataclass 定义递归构造对象，忽略未知键。"""
    data = data or {}
    kwargs: Dict[str, Any] = {}
    valid = {f.name: f for f in fields(cls)}
    for name, f in valid.items():
        if name not in data or data[name] is None:
            continue
        value = data[name]
        if is_dataclass(f.type) or (
            isinstance(f.type, str) and f.type in _DATACLASS_REGISTRY
        ):
            sub = f.type if is_dataclass(f.type) else _DATACLASS_REGISTRY[f.type]
            value = _dc(sub, value)
        kwargs[name] = value
    obj = cls(**kwargs)
    return obj


_DATACLASS_REGISTRY: Dict[str, Any] = {}


def _register(*classes):
    for cls in classes:
        _DATACLASS_REGISTRY[cls.__name__] = cls


# --------------------------------------------------------------------------
# 输入源
# --------------------------------------------------------------------------
@dataclass
class SourceSpec:
    path: str = ""
    kind: str = "auto"          # auto | csv | excel | parquet | json
    delimiter: Optional[str] = None
    quote: str = '"'
    escape: Optional[str] = None
    encoding: str = "utf-8"
    header: bool = True
    skip_rows: int = 0
    sheet: Optional[Any] = None     # Excel: 工作表名或 0 基序号
    name: str = ""                  # 报告里展示的名字
    sample_size: int = -1           # CSV 嗅探行数；-1 = 全量扫描（最准但多一遍 IO）

    def resolved_kind(self) -> str:
        if self.kind and self.kind != "auto":
            return self.kind
        ext = os.path.splitext(self.path or "")[1].lower()
        return KIND_BY_EXT.get(ext, "csv")

    def resolved_delimiter(self) -> Optional[str]:
        if self.delimiter is not None:
            return self.delimiter
        ext = os.path.splitext(self.path or "")[1].lower()
        if ext in (".tsv", ".tab"):
            return "\t"
        if ext == ".psv":
            return "|"
        return None  # 交给 DuckDB 嗅探


# --------------------------------------------------------------------------
# 比较策略
# --------------------------------------------------------------------------
@dataclass
class NumericOptions:
    """数值比较策略。

    ``rel_tol`` 默认给一个极小的值，用来吸收浮点表示噪声
    （例如 0.1+0.2 与 0.3 之间 1e-17 级别的差异），
    真正的业务容差请显式调大。
    """

    abs_tol: float = 0.0
    rel_tol: float = 1e-9
    ignore_thousands_separator: bool = True
    decimal_separator: str = "."          # "." 或 ","
    percent_mode: str = "keep"            # keep(去掉%号比数值) | ratio(12% 视作 0.12)
    strip_currency: bool = True


@dataclass
class StringOptions:
    trim: bool = True
    collapse_whitespace: bool = True
    ignore_whitespace: bool = False       # True 则删除所有空白再比较
    case_insensitive: bool = False
    fullwidth_to_halfwidth: bool = True
    null_equals_empty: bool = True
    null_tokens: List[str] = field(default_factory=lambda: list(DEFAULT_NULL_TOKENS))


@dataclass
class DateOptions:
    enabled: bool = True
    day_first: bool = True
    granularity: str = "auto"             # auto | timestamp | minute | date
    tolerance_seconds: float = 0.0
    formats: List[str] = field(default_factory=list)   # 留空则用默认格式表


@dataclass
class CompareOptions:
    detect_types: bool = True
    leading_zero_is_text: bool = True      # 001 这类带前导零的列按文本比，避免 001==1
    max_digits_for_number: int = 15        # 超过此位数的纯数字按文本比（防浮点丢精度）
    numeric: NumericOptions = field(default_factory=NumericOptions)
    string: StringOptions = field(default_factory=StringOptions)
    date: DateOptions = field(default_factory=DateOptions)


# --------------------------------------------------------------------------
# 主键
# --------------------------------------------------------------------------
@dataclass
class KeyOptions:
    trim: bool = True
    case_insensitive: bool = False
    fullwidth_to_halfwidth: bool = True
    numeric_normalize: bool = False        # True 则主键 001 与 1 视为同一行
    auto_detect: bool = True               # keys 为空时自动选主键
    auto_detect_max_distinct: int = 5_000_000
    use_row_index_if_no_key: bool = False  # 实在找不到主键时按行号对比
    max_dup_examples: int = 20


# --------------------------------------------------------------------------
# 单列规则
# --------------------------------------------------------------------------
@dataclass
class ColumnRule:
    name: str = ""
    after_name: Optional[str] = None
    type: Optional[str] = None             # string | numeric | date | boolean | ignore
    ignore: bool = False
    abs_tol: Optional[float] = None
    rel_tol: Optional[float] = None
    case_insensitive: Optional[bool] = None
    trim: Optional[bool] = None
    collapse_whitespace: Optional[bool] = None
    null_tokens: Optional[List[str]] = None
    date_formats: Optional[List[str]] = None
    granularity: Optional[str] = None
    mask: bool = False                     # 报告里脱敏显示


# --------------------------------------------------------------------------
# 异常检测
# --------------------------------------------------------------------------
@dataclass
class AnomalyOptions:
    enabled: bool = True
    outlier_enabled: bool = True
    outlier_multiplier: float = 3.0        # IQR  fence 倍数
    null_rate_delta: float = 0.05
    mean_shift_ratio: float = 0.30
    distinct_change_ratio: float = 0.30
    category_max_distinct: int = 60
    category_max_examples: int = 10
    numeric_parse_failure_ratio: float = 0.02
    max_examples: int = 20
    severity: str = "warn"                 # 默认级别


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
@dataclass
class ReportOptions:
    title: str = "前后数据集对比报告"
    formats: List[str] = field(
        default_factory=lambda: ["console", "html", "excel", "markdown"]
    )
    top_n: int = 30
    html_max_rows: int = 20000
    #: Excel 明细页的行数上限。Excel 里塞几十万行没人看，
    #: 而且写入耗时与行数线性相关；完整明细请用 CSV 或 DuckDB。
    excel_max_rows: int = 50000
    excel_max_cell_chars: int = 100
    markdown_max_rows: int = 100
    only_in_max_rows: int = 5000
    treat_format_only_as_diff: bool = False   # 报告口径/判定口径是否把格式差异算作差异
    include_equal_columns: bool = True
    max_cell_chars: int = 200


@dataclass
class Config:
    before: SourceSpec = field(default_factory=SourceSpec)
    after: SourceSpec = field(default_factory=SourceSpec)
    keys: List[str] = field(default_factory=list)
    key_options: KeyOptions = field(default_factory=KeyOptions)
    compare: CompareOptions = field(default_factory=CompareOptions)
    columns: List[ColumnRule] = field(default_factory=list)
    column_map: Dict[str, str] = field(default_factory=dict)   # before 列名 -> after 列名
    anomaly: AnomalyOptions = field(default_factory=AnomalyOptions)
    report: ReportOptions = field(default_factory=ReportOptions)
    output_dir: str = "compare_out"
    work_db: Optional[str] = None
    keep_intermediate: bool = True
    profile_sample_rows: int = 200000
    #: 行数不超过该值时用精确 count(DISTINCT)，超过则用近似统计（省内存）
    profile_exact_distinct_limit: int = 2_000_000
    memory_limit: Optional[str] = None
    threads: Optional[int] = None
    temp_dir: Optional[str] = None

    # -- 便捷访问 ---------------------------------------------------------
    def rule(self, name: str) -> Optional[ColumnRule]:
        for r in self.columns:
            if r.name == name:
                return r
        return None

    def after_name_for(self, name: str) -> str:
        r = self.rule(name)
        if r and r.after_name:
            return r.after_name
        return self.column_map.get(name, name)

    def date_formats(self) -> List[str]:
        if self.compare.date.formats:
            return self.compare.date.formats
        return list(
            DEFAULT_DATE_FORMATS
            if self.compare.date.day_first
            else DEFAULT_DATE_FORMATS_MD
        )

    def null_tokens(self, rule: Optional[ColumnRule] = None) -> List[str]:
        """返回用于判定「空值」的文本集合（统一小写、去重）。

        空值判定始终保持大小写不敏感：``NULL`` / ``Null`` / ``null``
        都应该被识别为空，这与值比较时的 ``case_insensitive`` 无关。
        """
        tokens = (
            list(rule.null_tokens)
            if rule and rule.null_tokens is not None
            else list(self.compare.string.null_tokens)
        )
        if not self.compare.string.null_equals_empty:
            tokens = [t for t in tokens if t != ""]
        seen = set()
        out: List[str] = []
        for token in tokens:
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out


_register(
    SourceSpec,
    NumericOptions,
    StringOptions,
    DateOptions,
    CompareOptions,
    KeyOptions,
    ColumnRule,
    AnomalyOptions,
    ReportOptions,
    Config,
)


def load_config(path: str) -> Config:
    """从 YAML / JSON 文件加载配置。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.lower().endswith(".json"):
        data = json.loads(text)
    else:
        data = _load_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {path} 顶层必须是字典/映射结构")

    before = data.get("before") or {}
    after = data.get("after") or {}
    # 支持简写: before: path/to.csv
    if isinstance(before, str):
        before = {"path": before}
    if isinstance(after, str):
        after = {"path": after}

    cfg = _dc(
        Config,
        {
            **{k: v for k, v in data.items() if k not in ("before", "after", "columns")},
            "before": before,
            "after": after,
            "columns": data.get("columns") or [],
        },
    )

    # columns 列表里可能是字符串简写
    rules: List[ColumnRule] = []
    for item in data.get("columns") or []:
        if isinstance(item, str):
            rules.append(ColumnRule(name=item))
        else:
            rules.append(_dc(ColumnRule, item))
    cfg.columns = rules
    return cfg


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "读取 YAML 配置需要 PyYAML，请执行 pip install PyYAML"
        ) from exc
    return yaml.safe_load(text)


def dump_config(cfg: Config) -> str:
    """把配置序列化成 YAML 文本（用于 init-config 生成模板）。"""
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return json.dumps(_to_plain(cfg), ensure_ascii=False, indent=2)
    return yaml.safe_dump(
        _to_plain(cfg), allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def _to_plain(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj
