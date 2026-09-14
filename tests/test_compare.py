"""对比引擎的端到端测试。

重点验证「补零 / 千分位 / 小数位数 / 日期写法 / 大小写 / 空白」这些
只有写法差异的情况，能被识别成 FORMAT_ONLY 而不是 VALUE_DIFF。
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from typing import Dict, Iterable, Sequence, Tuple

import pytest

from datacompare.config import ColumnRule, Config, SourceSpec
from datacompare.engine import CompareEngine
from datacompare.models import (
    FORMAT_ONLY,
    MODE_COMPARED,
    MODE_IGNORED,
    MODE_ONLY_AFTER,
    MODE_ONLY_BEFORE,
    NULL_MISMATCH,
    ROW_CHANGED,
    ROW_ONLY_IN_AFTER,
    ROW_ONLY_IN_BEFORE,
    TYPE_MISMATCH,
    VALUE_DIFF,
)


def _write_csv(path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


@contextmanager
def compare(
    tmp_path,
    header: Sequence[str],
    before_rows: Sequence[Sequence[object]],
    after_rows: Sequence[Sequence[object]],
    keys: Sequence[str] = ("id",),
    rules: Sequence[ColumnRule] = (),
    mutate=None,
):
    """把两份数据写成 CSV 跑一次对比，用完自动清理临时 DuckDB。"""
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    _write_csv(before, header, before_rows)
    _write_csv(after, header, after_rows)

    cfg = Config()
    cfg.before = SourceSpec(path=str(before))
    cfg.after = SourceSpec(path=str(after))
    cfg.keys = list(keys)
    cfg.columns = list(rules)
    cfg.anomaly.enabled = False
    cfg.report.formats = []
    if mutate:
        mutate(cfg)

    engine = CompareEngine(cfg)
    try:
        yield engine, engine.run()
    finally:
        engine.close()


def cell_statuses(result) -> Dict[Tuple[str, str], str]:
    """``{(主键值, 列名): 状态}``，方便断言。"""
    keys = result.stats.key_columns
    sql_keys = ", ".join(f'"{k}"' for k in keys)
    rows = result.con.sql(
        f"SELECT {sql_keys}, column_name, status FROM v_diff ORDER BY 1, 2"
    ).fetchall()
    out = {}
    for row in rows:
        out[(str(row[0]), str(row[-2]))] = str(row[-1])
    return out


# --------------------------------------------------------------------------
# 数值：这是「补零 / 小数位数」的核心场景
# --------------------------------------------------------------------------
def test_numeric_trailing_zeros_are_format_only(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "1.50"], ["B", "1234"], ["C", "0.10"]],
        [["A", "1.5"], ["B", "1234.00"], ["C", "0.1"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "金额")] == FORMAT_ONLY
        assert statuses[("B", "金额")] == FORMAT_ONLY
        assert statuses[("C", "金额")] == FORMAT_ONLY
        assert res.stats.severe_cell_diffs == 0


def test_numeric_thousands_separator(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "1,234.50"], ["B", "12,000"]],
        [["A", "1234.5"], ["B", "12000.00"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "金额")] == FORMAT_ONLY
        assert statuses[("B", "金额")] == FORMAT_ONLY


def test_numeric_currency_and_accounting_negative(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "¥1,200.00"], ["B", "(300.50)"]],
        [["A", "1200"], ["B", "-300.5"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "金额")] == FORMAT_ONLY
        assert statuses[("B", "金额")] == FORMAT_ONLY


def test_numeric_real_change_is_value_diff(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "100.00"], ["B", "1.5"]],
        [["A", "120.00"], ["B", "2.5"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "金额")] == VALUE_DIFF
        assert statuses[("B", "金额")] == VALUE_DIFF
        assert res.stats.severe_cell_diffs == 2


def test_float_noise_is_within_default_tolerance(tmp_path):
    with compare(
        tmp_path,
        ["id", "值"],
        [["A", "0.1"], ["B", "1.0000000001"]],
        [["A", "0.10000000000000001"], ["B", "1"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "值")] == FORMAT_ONLY
        assert statuses[("B", "值")] == FORMAT_ONLY


def test_relative_tolerance_configurable(tmp_path):
    def mutate(cfg):
        cfg.compare.numeric.rel_tol = 0.01

    with compare(
        tmp_path,
        ["id", "值"],
        [["A", "100"], ["B", "100"]],
        [["A", "100.5"], ["B", "102"]],
        mutate=mutate,
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "值")] == FORMAT_ONLY   # 0.5% < 1%
        assert statuses[("B", "值")] == VALUE_DIFF    # 2% > 1%


def test_absolute_tolerance_configurable(tmp_path):
    def mutate(cfg):
        cfg.compare.numeric.rel_tol = 0.0
        cfg.compare.numeric.abs_tol = 0.5

    with compare(
        tmp_path,
        ["id", "值"],
        [["A", "100"], ["B", "100"]],
        [["A", "100.4"], ["B", "100.6"]],
        mutate=mutate,
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "值")] == FORMAT_ONLY
        assert statuses[("B", "值")] == VALUE_DIFF


def test_percent_mode_keep(tmp_path):
    with compare(
        tmp_path,
        ["id", "折扣"],
        [["A", "12%"]],
        [["A", "12.00%"]],
    ) as (_, res):
        assert cell_statuses(res)[("A", "折扣")] == FORMAT_ONLY


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
def test_date_format_variants_are_format_only(tmp_path):
    with compare(
        tmp_path,
        ["id", "日期"],
        [["A", "2024-01-05"], ["B", "2024/03/09"], ["C", "2024年4月1日"]],
        [["A", "2024/01/05"], ["B", "2024-03-09"], ["C", "2024-04-01"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        for key in ("A", "B", "C"):
            assert statuses[(key, "日期")] == FORMAT_ONLY, key


def test_datetime_with_midnight_is_same_day(tmp_path):
    with compare(
        tmp_path,
        ["id", "日期"],
        [["A", "2024-01-05"]],
        [["A", "2024-01-05 00:00:00"]],
    ) as (_, res):
        assert cell_statuses(res)[("A", "日期")] == FORMAT_ONLY


def test_datetime_different_time_is_value_diff(tmp_path):
    with compare(
        tmp_path,
        ["id", "时间"],
        [["A", "2024-01-05 08:00:00"]],
        [["A", "2024-01-05 17:30:00"]],
    ) as (_, res):
        assert cell_statuses(res)[("A", "时间")] == VALUE_DIFF


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------
def test_whitespace_differences_are_format_only(tmp_path):
    with compare(
        tmp_path,
        ["id", "名称"],
        [["A", "  苹果 "], ["B", "香  蕉"]],
        [["A", "苹果"], ["B", "香 蕉"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "名称")] == FORMAT_ONLY
        assert statuses[("B", "名称")] == FORMAT_ONLY


def test_fullwidth_text_normalized(tmp_path):
    with compare(
        tmp_path,
        ["id", "编号"],
        [["A", "ＡＢＣ－１２３"], ["B", "（甲）"]],
        [["A", "ABC-123"], ["B", "(甲)"]],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("A", "编号")] == FORMAT_ONLY
        assert statuses[("B", "编号")] == FORMAT_ONLY


def test_case_sensitive_by_default(tmp_path):
    with compare(
        tmp_path,
        ["id", "名称"],
        [["A", "Apple"]],
        [["A", "apple"]],
    ) as (_, res):
        assert cell_statuses(res)[("A", "名称")] == VALUE_DIFF


def test_case_insensitive_option(tmp_path):
    def mutate(cfg):
        cfg.compare.string.case_insensitive = True

    with compare(
        tmp_path,
        ["id", "名称"],
        [["A", "Apple"]],
        [["A", "apple"]],
        mutate=mutate,
    ) as (_, res):
        assert cell_statuses(res)[("A", "名称")] == FORMAT_ONLY


# --------------------------------------------------------------------------
# 空值 / 类型
# --------------------------------------------------------------------------
def test_null_tokens_are_equal(tmp_path):
    with compare(
        tmp_path,
        ["id", "备注"],
        [["A", "N/A"], ["B", "NULL"], ["C", ""]],
        [["A", ""], ["B", ""], ["C", "null"]],
    ) as (_, res):
        assert res.stats.severe_cell_diffs == 0


def test_null_mismatch(tmp_path):
    with compare(
        tmp_path,
        ["id", "备注"],
        [["A", "有值"]],
        [["A", ""]],
    ) as (_, res):
        assert cell_statuses(res)[("A", "备注")] == NULL_MISMATCH


def test_null_equals_empty_can_be_disabled(tmp_path):
    def mutate(cfg):
        cfg.compare.string.null_equals_empty = False

    with compare(
        tmp_path,
        ["id", "备注"],
        [["A", ""]],
        [["A", "NULL"]],
        mutate=mutate,
    ) as (_, res):
        # 空串不再等价于 NULL 语义，而 "NULL" 仍是空值 token
        assert cell_statuses(res)[("A", "备注")] == NULL_MISMATCH


def test_type_mismatch_in_numeric_column(tmp_path):
    # 列里混了 1/3 的非数值，达不到「自动识别为数值」的置信度，
    # 所以这里显式声明该列是数值列（真实场景也建议这么做）
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "100"], ["B", "待确认"], ["C", "200"]],
        [["A", "100"], ["B", "300"], ["C", "abc"]],
        rules=[ColumnRule(name="金额", type="numeric")],
    ) as (_, res):
        statuses = cell_statuses(res)
        assert statuses[("B", "金额")] == TYPE_MISMATCH
        assert statuses[("C", "金额")] == TYPE_MISMATCH


def test_single_dirty_value_in_numeric_column_is_detected(tmp_path):
    """数值列里混进 1 个脏值——应被自动识别为数值列并报类型异常。"""
    before_rows = [["R%03d" % i, str(100 + i)] for i in range(50)]
    after_rows = [list(r) for r in before_rows]
    after_rows[7][1] = "待确认"

    with compare(tmp_path, ["id", "金额"], before_rows, after_rows) as (_, res):
        col = res.column("金额")
        assert col.ctype == "numeric"
        assert col.type_mismatch == 1
        assert cell_statuses(res)[("R007", "金额")] == TYPE_MISMATCH


def test_leading_zero_columns_compare_as_text(tmp_path):
    with compare(
        tmp_path,
        ["id", "门店编码"],
        [["A", "001"], ["B", "010"]],
        [["A", "1"], ["B", "0010"]],
    ) as (_, res):
        col = res.column("门店编码")
        assert col.ctype == "string"
        statuses = cell_statuses(res)
        assert statuses[("A", "门店编码")] == VALUE_DIFF
        assert statuses[("B", "门店编码")] == VALUE_DIFF


def test_long_digit_strings_compare_as_text(tmp_path):
    long_a = "1234567890123456789"
    long_b = "1234567890123456790"
    with compare(
        tmp_path,
        ["id", "订单号"],
        [["A", long_a]],
        [["A", long_b]],
    ) as (_, res):
        assert res.column("订单号").ctype == "string"
        assert cell_statuses(res)[("A", "订单号")] == VALUE_DIFF


# --------------------------------------------------------------------------
# 行匹配
# --------------------------------------------------------------------------
def test_row_statuses(tmp_path):
    with compare(
        tmp_path,
        ["id", "值"],
        [["A", "1"], ["B", "2"], ["C", "3"]],
        [["A", "1"], ["B", "9"], ["D", "4"]],
    ) as (_, res):
        stats = res.stats
        assert stats.matched_pairs == 2
        assert stats.same_rows == 1
        assert stats.changed_rows == 1
        assert stats.only_in_before == 1
        assert stats.only_in_after == 1
        rows = dict(
            res.con.sql("SELECT __key, row_status FROM row_status").fetchall()
        )
        assert rows["B"] == ROW_CHANGED
        assert rows["C"] == ROW_ONLY_IN_BEFORE
        assert rows["D"] == ROW_ONLY_IN_AFTER


def test_duplicate_keys_pair_by_order(tmp_path):
    with compare(
        tmp_path,
        ["id", "值"],
        [["A", "1"], ["A", "2"]],
        [["A", "1"], ["A", "2"], ["A", "3"]],
    ) as (_, res):
        # 前两条按出现顺序配对成功，第三条是单边行
        assert res.stats.dup_key_after == 1
        assert res.stats.only_in_after == 1
        assert res.stats.severe_cell_diffs == 0


def test_composite_keys(tmp_path):
    with compare(
        tmp_path,
        ["门店", "日期", "值"],
        [["001", "2024-01-01", "1"], ["001", "2024-01-02", "2"]],
        [["001", "2024-01-01", "1"], ["001", "2024-01-02", "3"]],
        keys=("门店", "日期"),
    ) as (_, res):
        assert res.stats.matched_pairs == 2
        assert res.stats.severe_cell_diffs == 1


def test_row_index_fallback(tmp_path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    _write_csv(before, ["值"], [["a"], ["b"], ["c"]])
    _write_csv(after, ["值"], [["a"], ["x"], ["c"]])

    cfg = Config()
    cfg.before = SourceSpec(path=str(before))
    cfg.after = SourceSpec(path=str(after))
    cfg.keys = []
    cfg.key_options.auto_detect = False
    cfg.key_options.use_row_index_if_no_key = True
    cfg.anomaly.enabled = False
    engine = CompareEngine(cfg)
    try:
        res = engine.run()
        assert res.stats.key_columns == []
        assert res.stats.matched_pairs == 3
        assert res.stats.severe_cell_diffs == 1
        assert "行号" in res.stats.key_strategy
    finally:
        engine.close()


def test_missing_key_raises(tmp_path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    _write_csv(before, ["值"], [["a"]])
    _write_csv(after, ["值"], [["a"]])
    cfg = Config()
    cfg.before = SourceSpec(path=str(before))
    cfg.after = SourceSpec(path=str(after))
    cfg.keys = []
    cfg.key_options.auto_detect = False
    engine = CompareEngine(cfg)
    try:
        with pytest.raises(ValueError):
            engine.run()
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 字段级
# --------------------------------------------------------------------------
def test_schema_change(tmp_path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    _write_csv(before, ["id", "甲", "乙"], [["A", "1", "2"]])
    _write_csv(after, ["id", "甲", "丙"], [["A", "1", "3"]])

    cfg = Config()
    cfg.before = SourceSpec(path=str(before))
    cfg.after = SourceSpec(path=str(after))
    cfg.keys = ["id"]
    cfg.anomaly.enabled = False
    engine = CompareEngine(cfg)
    try:
        res = engine.run()
        assert res.stats.only_before_columns == ["乙"]
        assert res.stats.only_after_columns == ["丙"]
        assert res.column("乙").mode == MODE_ONLY_BEFORE
        assert res.column("丙").mode == MODE_ONLY_AFTER
        assert any(a.category == "schema_change" for a in
                   _enable_anomalies(str(before), str(after)))
    finally:
        engine.close()


def _enable_anomalies(before_path, after_path):
    cfg = Config()
    cfg.before = SourceSpec(path=before_path)
    cfg.after = SourceSpec(path=after_path)
    cfg.keys = ["id"]
    engine = CompareEngine(cfg)
    try:
        return engine.run().anomalies
    finally:
        engine.close()


def test_ignored_column(tmp_path):
    with compare(
        tmp_path,
        ["id", "备注"],
        [["A", "1"]],
        [["A", "2"]],
        rules=[ColumnRule(name="备注", ignore=True)],
    ) as (_, res):
        assert res.column("备注").mode == MODE_IGNORED
        assert res.stats.severe_cell_diffs == 0


def test_column_rename_mapping(tmp_path):
    with compare(
        tmp_path,
        ["id", "旧名"],
        [["A", "1"]],
        [["A", "2"]],
        rules=[ColumnRule(name="旧名", after_name="新名")],
    ) as (_, res):
        # after.csv 里没有「新名」列，映射失败会退化为「仅前数据集」
        assert res.column("旧名").mode == MODE_ONLY_BEFORE


def test_column_map_is_used(tmp_path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    _write_csv(before, ["id", "金额"], [["A", "1"]])
    _write_csv(after, ["id", "amount"], [["A", "2"]])

    cfg = Config()
    cfg.before = SourceSpec(path=str(before))
    cfg.after = SourceSpec(path=str(after))
    cfg.keys = ["id"]
    cfg.column_map = {"金额": "amount"}
    cfg.anomaly.enabled = False
    engine = CompareEngine(cfg)
    try:
        res = engine.run()
        col = res.column("金额")
        assert col.mode == MODE_COMPARED
        assert col.after_name == "amount"
        assert col.value_diff == 1
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 统计口径
# --------------------------------------------------------------------------
def test_format_only_does_not_count_as_severe(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额", "数量"],
        [["A", "1.50", "1"]],
        [["A", "1.5", "2"]],
    ) as (_, res):
        assert res.stats.format_only_cells == 1
        assert res.stats.severe_cell_diffs == 1
        assert res.stats.cell_diffs == 2
        assert not res.stats.consistent
        assert res.column("金额").severe_total == 0
        assert res.column("数量").severe_total == 1


def test_consistent_when_only_format_differs(tmp_path):
    with compare(
        tmp_path,
        ["id", "金额"],
        [["A", "1.50"], ["B", "2,000.00"]],
        [["A", "1.5"], ["B", "2000"]],
    ) as (_, res):
        assert res.stats.format_only_cells == 2
        assert res.stats.severe_cell_diffs == 0
        assert res.stats.consistent


def test_empty_files(tmp_path):
    with compare(tmp_path, ["id", "值"], [], [], keys=("id",)) as (_, res):
        assert res.stats.before_rows == 0
        assert res.stats.after_rows == 0
        assert res.stats.consistent
