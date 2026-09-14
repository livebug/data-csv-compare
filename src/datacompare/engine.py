"""对比引擎：把两侧数据加载进 DuckDB，做全字段对比。

整体流程
--------
1. 两侧数据落 DuckDB（全 VARCHAR，保留原始写法）
2. 字段画像 → 推断每个字段该按数值 / 日期 / 文本比
3. 确定主键（配置指定 或 自动推断）
4. 主键 + 出现序号对齐成 1:1 配对（重复主键按顺序配对，多出来的算单边行）
5. 生成逐字段差异表 ``diff_detail`` 与行级结论表 ``row_status``
6. 跑异常检测

所有中间结果都是 DuckDB 里的真实表，可以事后自己写 SQL 复查。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

from .config import ColumnRule, Config
from .models import (
    ColumnProfile,
    ColumnResult,
    CompareResult,
    MODE_COMPARED,
    MODE_IGNORED,
    MODE_KEY,
    MODE_ONLY_AFTER,
    MODE_ONLY_BEFORE,
    ROW_CHANGED,
    ROW_ONLY_IN_AFTER,
    ROW_ONLY_IN_BEFORE,
    ROW_SAME,
    Stats,
    TYPE_STRING,
)
from .normalize import ColumnPlan, build_column_plan, infer_type
from .sqlutil import quote_ident, sql_str

BEFORE_TABLE = "dc_before"
AFTER_TABLE = "dc_after"

#: 一次 INSERT ... SELECT 里塞多少个字段的 UNION 分支
_DIFF_CHUNK = 60

#: 保留列名，源数据里出现就报错
_RESERVED = ("__row_id", "__key", "__rank", "__pair_id")


class CompareEngine:
    def __init__(self, cfg: Config, db_path: Optional[str] = None, log=None) -> None:
        self.cfg = cfg
        self._log = log or (lambda msg: None)
        self._temp_dir: Optional[str] = None
        self._own_temp_dir = False
        self._own_db = False
        self._temp_files: List[str] = []
        self.profiles_before: Dict[str, ColumnProfile] = {}
        self.profiles_after: Dict[str, ColumnProfile] = {}
        self._profile_cache: Dict[Tuple[str, Tuple[str, ...]], Dict[str, ColumnProfile]] = {}
        self.con = None
        self.db_path = db_path or cfg.work_db

    def profile(self, table: str, columns: Sequence[str]) -> Dict[str, ColumnProfile]:
        """字段画像（带缓存，同一份列清单不重复扫描）。"""
        key = (table, tuple(columns))
        cached = self._profile_cache.get(key)
        if cached is not None:
            return cached
        from .profile import profile_table

        profiles, _ = profile_table(self.con, table, columns, self.cfg)
        self._profile_cache[key] = profiles
        return profiles

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _open(self):
        import duckdb

        if not self.db_path:
            store = self._ensure_temp_dir()
            fd, path = tempfile.mkstemp(suffix=".duckdb", dir=store, prefix="dc_")
            os.close(fd)
            os.unlink(path)
            self.db_path = path
            self._own_db = True
        else:
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self.con = duckdb.connect(self.db_path)
        if self.cfg.memory_limit:
            self.con.execute(f"SET memory_limit={sql_str(self.cfg.memory_limit)}")
        if self.cfg.threads:
            self.con.execute(f"SET threads={int(self.cfg.threads)}")
        store = self.cfg.temp_dir or self._ensure_temp_dir()
        self.con.execute(f"SET temp_directory={sql_str(store)}")
        self.con.execute("SET preserve_insertion_order=false")
        return self.con

    def _ensure_temp_dir(self) -> str:
        if self._temp_dir:
            return self._temp_dir
        if self.cfg.temp_dir:
            os.makedirs(self.cfg.temp_dir, exist_ok=True)
            self._temp_dir = self.cfg.temp_dir
            return self._temp_dir
        self._temp_dir = tempfile.mkdtemp(prefix="datacompare_")
        self._own_temp_dir = True
        return self._temp_dir

    def close(self, cleanup: bool = True) -> None:
        """关闭连接。``cleanup=True`` 时清掉临时文件和临时库。"""
        if self.con is not None:
            try:
                self.con.close()
            except Exception:
                pass
            self.con = None
        if not cleanup:
            return
        from .sources import _cleanup_paths

        _cleanup_paths(self._temp_files)
        self._temp_files = []
        if self._own_db and self.db_path and os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass
            self.db_path = None
        if self._own_temp_dir and self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self) -> CompareResult:
        from .sources import load_source

        con = self._open()
        warnings: List[str] = []
        cfg = self.cfg

        self._log(f"读取前数据集：{cfg.before.path}")
        before = load_source(con, cfg.before, BEFORE_TABLE, self._ensure_temp_dir())
        self._temp_files.extend(before.temp_paths)
        warnings.extend(before.warnings)
        self._log(f"  → {before.row_count:,} 行 × {before.ncols} 列")

        self._log(f"读取后数据集：{cfg.after.path}")
        after = load_source(con, cfg.after, AFTER_TABLE, self._ensure_temp_dir())
        self._temp_files.extend(after.temp_paths)
        warnings.extend(after.warnings)
        self._log(f"  → {after.row_count:,} 行 × {after.ncols} 列")

        for cols in (before.columns, after.columns):
            for reserved in _RESERVED:
                if reserved in cols:
                    raise ValueError(
                        f"数据中存在保留列名 `{reserved}`，请先在源数据里重命名该列"
                    )

        stats = Stats(
            before_rows=before.row_count,
            after_rows=after.row_count,
            before_columns=list(before.columns),
            after_columns=list(after.columns),
            before_path=cfg.before.path,
            after_path=cfg.after.path,
        )

        pairs, only_before_cols, only_after_cols, col_warnings = self._align_columns(
            before.columns, after.columns
        )
        warnings.extend(col_warnings)
        stats.only_before_columns = only_before_cols
        stats.only_after_columns = only_after_cols

        keys, key_strategy, key_expr, key_warnings = self._resolve_keys(pairs)
        warnings.extend(key_warnings)
        stats.key_columns = list(keys)
        stats.key_strategy = key_strategy

        plans = self._build_plans(pairs)
        stats.total_columns = len(before.columns)
        stats.compared_columns = len(plans)

        self._log("按主键匹配行…")
        self._build_match_tables(key_expr)

        self._log("逐字段比对…")
        self._build_paired(plans)
        self._build_diff_detail(plans)
        self._build_row_status()
        self._build_views(keys)

        columns, cell_diffs, severe, fmt_only = self._collect_column_results(
            pairs, plans, keys, only_after_cols
        )

        matched = int(
            con.sql(
                "SELECT count(*) FROM row_status "
                "WHERE __b_row IS NOT NULL AND __a_row IS NOT NULL"
            ).fetchone()[0]
        )
        for col in columns:
            col.matched_rows = matched

        row_counts = dict(
            con.sql("SELECT row_status, count(*) FROM row_status GROUP BY 1").fetchall()
        )
        stats.matched_pairs = matched
        stats.same_rows = int(row_counts.get(ROW_SAME, 0))
        stats.changed_rows = int(row_counts.get(ROW_CHANGED, 0))
        stats.only_in_before = int(row_counts.get(ROW_ONLY_IN_BEFORE, 0))
        stats.only_in_after = int(row_counts.get(ROW_ONLY_IN_AFTER, 0))
        stats.cell_diffs = cell_diffs
        stats.severe_cell_diffs = severe
        stats.format_only_cells = fmt_only
        stats.dup_key_before = self._dup_key_count("before_ranked")
        stats.dup_key_after = self._dup_key_count("after_ranked")

        if not cfg.keep_intermediate:
            self._drop_intermediates()

        anomalies: List = []
        if cfg.anomaly.enabled:
            self._log("检测异常数据…")
            from .anomaly import detect_anomalies

            anomalies = detect_anomalies(
                con,
                cfg,
                stats,
                columns,
                self.profiles_before,
                self.profiles_after,
            )

        return CompareResult(
            stats=stats,
            columns=columns,
            anomalies=anomalies,
            db_path=self.db_path,
            con=con,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # 列对齐
    # ------------------------------------------------------------------
    def _align_columns(
        self, before_cols: Sequence[str], after_cols: Sequence[str]
    ) -> Tuple[
        List[Tuple[str, str, Optional[ColumnRule]]], List[str], List[str], List[str]
    ]:
        cfg = self.cfg
        after_set = set(after_cols)
        warnings: List[str] = []
        pairs: List[Tuple[str, str, Optional[ColumnRule]]] = []
        used_after: Dict[str, str] = {}

        for col in before_cols:
            target = cfg.after_name_for(col)
            if target not in after_set:
                pairs.append((col, "", cfg.rule(col)))
                continue
            if target in used_after:
                warnings.append(
                    f"前数据集的 `{col}` 和 `{used_after[target]}` 都映射到后数据集的 "
                    f"`{target}`，保留第一个映射，`{col}` 记为「仅前数据集有」"
                )
                pairs.append((col, "", cfg.rule(col)))
                continue
            used_after[target] = col
            pairs.append((col, target, cfg.rule(col)))

        only_before = [b for b, a, _ in pairs if not a]
        only_after = [c for c in after_cols if c not in set(used_after.keys())]
        return pairs, only_before, only_after, warnings

    # ------------------------------------------------------------------
    # 主键
    # ------------------------------------------------------------------
    def _resolve_keys(
        self, pairs: Sequence[Tuple[str, str, Optional[ColumnRule]]]
    ) -> Tuple[List[str], str, str, List[str]]:
        cfg = self.cfg
        warnings: List[str] = []
        common = [b for b, a, _ in pairs if a]

        if cfg.keys:
            missing = [k for k in cfg.keys if k not in common]
            if missing:
                raise ValueError(
                    f"主键列在两侧数据中不同时存在：{missing}。\n"
                    f"当前两侧都有的列：{common[:80]}"
                )
            from .profile import build_key_expr

            return (
                list(cfg.keys),
                "配置指定",
                build_key_expr(list(cfg.keys), cfg.key_options),
                warnings,
            )

        if not cfg.key_options.auto_detect:
            if cfg.key_options.use_row_index_if_no_key:
                return [], "按行号（源文件物理顺序）对齐", "CAST(__row_id AS VARCHAR)", warnings
            raise ValueError(
                "未指定主键：请在配置里设置 keys，"
                "或开启 key_options.auto_detect / use_row_index_if_no_key"
            )

        keys, strategy, warns = self._auto_keys(common)
        warnings.extend(warns)
        if keys:
            from .profile import build_key_expr

            return keys, strategy, build_key_expr(keys, cfg.key_options), warnings

        if cfg.key_options.use_row_index_if_no_key:
            return [], "未找到唯一键，按行号（源文件物理顺序）对齐", "CAST(__row_id AS VARCHAR)", warnings
        raise ValueError(
            "无法自动识别主键。请用 --keys 指定主键列，"
            "或设置 key_options.use_row_index_if_no_key: true 按行号对比。\n"
            f"两侧都有的列：{common[:80]}"
        )

    def _auto_keys(self, common: Sequence[str]) -> Tuple[List[str], str, List[str]]:
        from .profile import detect_keys, key_is_unique

        if not common:
            return [], "", ["两侧没有共同列，无法按主键匹配"]

        profiles = self.profile(BEFORE_TABLE, common)
        keys, strategy, warnings = detect_keys(
            self.con, BEFORE_TABLE, common, profiles, self.cfg
        )
        if not keys:
            return [], "", warnings

        ok_after, total_after, _ = key_is_unique(
            self.con, AFTER_TABLE, keys, self.cfg.key_options
        )
        if not ok_after and total_after > 0:
            warnings.append(
                f"主键 {keys} 在前数据集唯一，但在后数据集不唯一："
                "已按「主键 + 出现顺序」配对，多出来的行会记为单边行"
            )
        return keys, strategy, warnings

    # ------------------------------------------------------------------
    # 画像与比对计划
    # ------------------------------------------------------------------
    def _build_plans(
        self, pairs: Sequence[Tuple[str, str, Optional[ColumnRule]]]
    ) -> List[ColumnPlan]:
        cfg = self.cfg
        compared = [(b, a, r) for b, a, r in pairs if a]
        active = [b for b, a, r in compared if not (r and r.ignore)]
        after_for = {b: a for b, a, r in compared}

        if active and (cfg.compare.detect_types or cfg.anomaly.enabled):
            self._log(f"字段画像（{len(active)} 列）…")
            after_names = [after_for[b] for b in active]
            self.profiles_before = self.profile(BEFORE_TABLE, active)
            raw_after = self.profile(AFTER_TABLE, after_names)
            self.profiles_after = {
                b: raw_after.get(a) for b, a in zip(active, after_names)
            }

        key_set = set(cfg.keys)
        plans: List[ColumnPlan] = []
        for idx, (bcol, acol, rule) in enumerate(compared):
            if bcol in key_set:
                continue
            if rule and (rule.ignore or rule.type == "ignore"):
                continue

            pb = self.profiles_before.get(bcol)
            pa = self.profiles_after.get(bcol)
            if rule and rule.type:
                ctype, notes = rule.type, "配置指定"
            elif pb is not None or pa is not None:
                ctype, notes = infer_type(pb, pa, cfg, rule)
            else:
                ctype, notes = TYPE_STRING, "未画像，按文本比较"

            detected_time = any(
                p is not None and getattr(p, "has_time_part", 0) > 0 for p in (pb, pa)
            )
            plan = build_column_plan(
                name=bcol,
                after_name=acol,
                ctype=ctype,
                index=idx,
                cfg=cfg,
                before_ref=f'b.{quote_ident(bcol)}',
                after_ref=f'a.{quote_ident(acol)}',
                rule=rule,
                detected_time=detected_time,
            )
            plan.notes = notes
            plans.append(plan)
        return plans

    # ------------------------------------------------------------------
    # 中间表
    # ------------------------------------------------------------------
    def _build_match_tables(self, key_expr: str) -> None:
        con = self.con
        for side, table in (("before", BEFORE_TABLE), ("after", AFTER_TABLE)):
            con.execute(
                f"CREATE OR REPLACE TABLE {side}_match AS "
                f"SELECT *, {key_expr} AS __key FROM {quote_ident(table)}"
            )
            con.execute(
                f"CREATE OR REPLACE TABLE {side}_ranked AS "
                f"SELECT *, row_number() OVER (PARTITION BY __key ORDER BY __row_id) AS __rank "
                f"FROM {side}_match"
            )
        con.execute(
            """
            CREATE OR REPLACE TABLE pairs AS
            SELECT
              coalesce(CAST(b.__row_id AS VARCHAR), '') || '/' ||
                coalesce(CAST(a.__row_id AS VARCHAR), '') AS __pair_id,
              coalesce(b.__key, a.__key) AS __key,
              b.__row_id AS __b_row,
              a.__row_id AS __a_row,
              b.__rank AS __rank
            FROM before_ranked b
            FULL OUTER JOIN after_ranked a
              ON b.__key = a.__key AND b.__rank = a.__rank
            """
        )

    def _build_paired(self, plans: Sequence[ColumnPlan]) -> None:
        """把两侧字段的归一化结果物化成 ``paired_norm`` 表。

        这是性能关键，分两步走：

        1. ``paired_raw`` —— 阶段一：原始文本 + 字符串归一化（全角/空白/大小写）
        2. ``paired_norm`` —— 阶段二：数值/日期解析 + 空值判定

        为什么不用一条语句？因为差异判定表达式一旦内联展开，同一条正则链会被
        重复求值十几次。物化之后判定只剩列引用，快一个数量级。
        """
        stage1 = [
            "p.__pair_id AS __pair_id",
            "p.__key AS __key",
            "p.__b_row AS __b_row",
            "p.__a_row AS __a_row",
        ]
        for plan in plans:
            for alias, expr in plan.stage1_columns():
                stage1.append(f"({expr}) AS {quote_ident(alias)}")

        self.con.execute(
            "CREATE OR REPLACE TABLE paired_raw AS SELECT "
            + ", ".join(stage1)
            + " FROM pairs p"
            + " JOIN before_ranked b ON b.__row_id = p.__b_row"
            + " JOIN after_ranked a ON a.__row_id = p.__a_row"
        )

        stage2 = ["__pair_id", "__key", "__b_row", "__a_row"]
        for plan in plans:
            i = plan.index
            # 原始文本要带下来：报告展示与「原始文本是否完全一致」判定都要用
            stage2.append(quote_ident(f"b_r{i}"))
            stage2.append(quote_ident(f"a_r{i}"))
            for alias, expr in plan.stage2_columns():
                stage2.append(f"({expr}) AS {quote_ident(alias)}")

        self.con.execute(
            "CREATE OR REPLACE TABLE paired_norm AS SELECT "
            + ", ".join(stage2)
            + " FROM paired_raw p"
        )
        self.con.execute("DROP TABLE IF EXISTS paired_raw")

    def _build_diff_detail(self, plans: Sequence[ColumnPlan]) -> None:
        con = self.con
        con.execute(
            """
            CREATE OR REPLACE TABLE diff_detail (
              __pair_id     VARCHAR,
              __key         VARCHAR,
              __b_row       BIGINT,
              __a_row       BIGINT,
              column_name   VARCHAR,
              column_type   VARCHAR,
              column_notes  VARCHAR,
              before_raw    VARCHAR,
              after_raw     VARCHAR,
              before_norm   VARCHAR,
              after_norm    VARCHAR,
              status        VARCHAR,
              abs_diff      DOUBLE,
              rel_diff      DOUBLE
            )
            """
        )
        if not plans:
            return

        for start in range(0, len(plans), _DIFF_CHUNK):
            chunk = plans[start : start + _DIFF_CHUNK]
            body = " UNION ALL ".join(self._diff_branch(p) for p in chunk)
            con.execute(
                "INSERT INTO diff_detail "
                "SELECT __pair_id, __key, __b_row, __a_row, column_name, column_type, "
                "column_notes, before_raw, after_raw, before_norm, after_norm, status, "
                "abs_diff, rel_diff FROM ("
                + body
                + ") WHERE __b_row IS NOT NULL AND __a_row IS NOT NULL "
                "AND status <> 'EQUAL'"
            )

    def _diff_branch(self, plan: ColumnPlan) -> str:
        b_raw, a_raw = plan.raw_refs
        b_val, a_val = plan.val_refs
        null_double = "CAST(NULL AS DOUBLE)"
        abs_diff = (
            f"CAST({plan.abs_diff_expr} AS DOUBLE)" if plan.abs_diff_expr else null_double
        )
        rel_diff = (
            f"CAST({plan.rel_diff_expr} AS DOUBLE)" if plan.rel_diff_expr else null_double
        )
        return (
            "SELECT __pair_id, __key, __b_row, __a_row, "
            f"{sql_str(plan.name)} AS column_name, "
            f"{sql_str(plan.ctype)} AS column_type, "
            f"{sql_str(plan.notes)} AS column_notes, "
            f"{b_raw} AS before_raw, "
            f"{a_raw} AS after_raw, "
            f"CAST({b_val} AS VARCHAR) AS before_norm, "
            f"CAST({a_val} AS VARCHAR) AS after_norm, "
            f"({plan.status_expr}) AS status, "
            f"{abs_diff} AS abs_diff, "
            f"{rel_diff} AS rel_diff "
            "FROM paired_norm p"
        )

    def _build_row_status(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE row_status AS
            SELECT
              p.__pair_id AS __pair_id,
              p.__key AS __key,
              p.__b_row AS __b_row,
              p.__a_row AS __a_row,
              CASE
                WHEN p.__b_row IS NULL THEN 'ONLY_IN_AFTER'
                WHEN p.__a_row IS NULL THEN 'ONLY_IN_BEFORE'
                WHEN coalesce(d.cnt, 0) > 0 THEN 'CHANGED'
                ELSE 'SAME'
              END AS row_status,
              coalesce(d.cnt, 0) AS changed_columns,
              coalesce(d.severe, 0) AS severe_columns,
              coalesce(d.fmt, 0) AS format_only_columns
            FROM pairs p
            LEFT JOIN (
              SELECT __pair_id,
                     count(*) AS cnt,
                     count(*) FILTER (
                       WHERE status IN ('VALUE_DIFF','NULL_MISMATCH','TYPE_MISMATCH')
                     ) AS severe,
                     count(*) FILTER (WHERE status = 'FORMAT_ONLY') AS fmt
              FROM diff_detail GROUP BY __pair_id
            ) d ON d.__pair_id = p.__pair_id
            """
        )

    def _build_views(self, keys: Sequence[str]) -> None:
        con = self.con
        if keys:
            key_select = ", ".join(
                f"coalesce(b.{quote_ident(k)}, a.{quote_ident(k)}) AS {quote_ident(k)}"
                for k in keys
            )
            suffix = ", " + key_select
        else:
            suffix = ""

        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_row AS
            SELECT s.*{suffix}
            FROM row_status s
            LEFT JOIN before_ranked b ON b.__row_id = s.__b_row
            LEFT JOIN after_ranked a ON a.__row_id = s.__a_row
            """
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW v_diff AS
            SELECT d.*{suffix}
            FROM diff_detail d
            LEFT JOIN before_ranked b ON b.__row_id = d.__b_row
            LEFT JOIN after_ranked a ON a.__row_id = d.__a_row
            """
        )

    def _drop_intermediates(self) -> None:
        for name in ("before_match", "after_match", "pairs", "paired_raw", "paired_norm"):
            self.con.execute(f"DROP TABLE IF EXISTS {quote_ident(name)}")

    def _dup_key_count(self, table: str) -> int:
        return int(
            self.con.sql(
                f"SELECT count(*) FROM (SELECT __key FROM {quote_ident(table)} "
                f"GROUP BY 1 HAVING count(*) > 1)"
            ).fetchone()[0]
        )

    # ------------------------------------------------------------------
    # 结果汇总
    # ------------------------------------------------------------------
    def _collect_column_results(
        self,
        pairs: Sequence[Tuple[str, str, Optional[ColumnRule]]],
        plans: Sequence[ColumnPlan],
        keys: Sequence[str],
        only_after: Sequence[str],
    ) -> Tuple[List[ColumnResult], int, int, int]:
        agg: Dict[str, Dict[str, int]] = {}
        for name, status, cnt in self.con.sql(
            "SELECT column_name, status, count(*) FROM diff_detail GROUP BY 1, 2"
        ).fetchall():
            agg.setdefault(name, {})[status] = int(cnt)

        plan_by_name = {p.name: p for p in plans}
        key_set = set(keys)
        results: List[ColumnResult] = []

        for bcol, acol, rule in pairs:
            pb = self.profiles_before.get(bcol)
            pa = self.profiles_after.get(bcol)
            res = ColumnResult(
                name=bcol,
                after_name=acol,
                null_rate_before=pb.null_rate if pb else 0.0,
                null_rate_after=pa.null_rate if pa else 0.0,
                distinct_before=pb.distinct_vals if pb else 0,
                distinct_after=pa.distinct_vals if pa else 0,
            )
            if not acol:
                res.mode = MODE_ONLY_BEFORE
                res.ctype = TYPE_STRING
                res.note = "仅前数据集存在该字段"
            elif bcol in key_set:
                res.mode = MODE_KEY
                res.ctype = TYPE_STRING
                res.note = "主键列，用于行匹配，不参与值对比"
            elif rule and (rule.ignore or rule.type == "ignore"):
                res.mode = MODE_IGNORED
                res.ctype = TYPE_STRING
                res.note = "按配置忽略"
            else:
                plan = plan_by_name.get(bcol)
                res.mode = MODE_COMPARED
                res.ctype = plan.ctype if plan else TYPE_STRING
                res.note = plan.notes if plan else ""
                counts = agg.get(bcol, {})
                res.format_only = counts.get("FORMAT_ONLY", 0)
                res.value_diff = counts.get("VALUE_DIFF", 0)
                res.null_mismatch = counts.get("NULL_MISMATCH", 0)
                res.type_mismatch = counts.get("TYPE_MISMATCH", 0)
            results.append(res)

        for acol in only_after:
            results.append(
                ColumnResult(
                    name=acol,
                    after_name=acol,
                    mode=MODE_ONLY_AFTER,
                    ctype=TYPE_STRING,
                    note="仅后数据集存在该字段",
                )
            )

        cell_diffs = severe = fmt_only = 0
        for res in results:
            if res.mode != MODE_COMPARED:
                continue
            cell_diffs += res.diff_total
            severe += res.severe_total
            fmt_only += res.format_only
        return results, cell_diffs, severe, fmt_only
