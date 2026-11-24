"""Tier 1: Count and Volume Validator."""

from concurrent.futures import ThreadPoolExecutor
import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field

from data_diff.databases.base import Database

logger = logging.getLogger("validator.count")


class PartitionCountDiff(BaseModel):
    partition_key: str
    source_count: Optional[int] = None
    target_count: Optional[int] = None
    diff: int = 0
    diff_pct: float = 0.0
    status: str  # "MATCH", "MISMATCH", "MISSING_IN_TARGET", "MISSING_IN_SOURCE"


class ColumnProfileDiff(BaseModel):
    column_name: str
    source_null_count: int = 0
    target_null_count: int = 0
    null_diff: int = 0
    source_min: Optional[str] = None
    target_min: Optional[str] = None
    source_max: Optional[str] = None
    target_max: Optional[str] = None
    source_sum: Optional[float] = None
    target_sum: Optional[float] = None
    sum_diff: Optional[float] = None
    status: str  # "MATCH", "MISMATCH"


class CountValidationResult(BaseModel):
    table_name: str = ""
    source_table: str
    target_table: str
    source_total_count: int = 0
    target_total_count: int = 0
    count_diff: int = 0
    count_diff_pct: float = 0.0
    threshold_pct: float = 0.0
    passed: bool = True
    partition_column: Optional[str] = None
    partition_diffs: List[PartitionCountDiff] = Field(default_factory=list)
    column_profiles: List[ColumnProfileDiff] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error: Optional[str] = None


class CountValidator:
    """Tier 1 Validator: total row counts, partition-level breakdown, and column profiles."""

    def __init__(self, max_concurrency: int = 2) -> None:
        self.max_concurrency = max_concurrency

    def _qualify_table(self, db: Database, table_ref: Union[str, Sequence[str]]) -> str:
        if isinstance(table_ref, str):
            parts = table_ref.split(".")
        else:
            parts = list(table_ref)

        return ".".join(db.dialect.quote(p) for p in parts)

    def _query_total_count(self, db: Database, table_ref: str, where: Optional[str] = None) -> int:
        qualified_table = self._qualify_table(db, table_ref)
        sql = f"SELECT COUNT(*) FROM {qualified_table}"
        if where:
            sql += f" WHERE {where}"

        logger.debug(f"Executing count query on {db.name}: {sql}")
        rows = db.query(sql, list)
        if not rows or not rows[0]:
            return 0
        return int(rows[0][0])

    def _query_partition_counts(
        self, db: Database, table_ref: str, partition_col: str, where: Optional[str] = None
    ) -> Dict[str, int]:
        qualified_table = self._qualify_table(db, table_ref)
        quoted_col = db.dialect.quote(partition_col)
        sql = f"SELECT {quoted_col}, COUNT(*) FROM {qualified_table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" GROUP BY {quoted_col} ORDER BY {quoted_col}"

        logger.debug(f"Executing partition query on {db.name}: {sql}")
        rows = db.query(sql, list)
        result: Dict[str, int] = {}
        for row in rows:
            pk_val = str(row[0]) if row[0] is not None else "__NULL__"
            cnt = int(row[1]) if row[1] is not None else 0
            result[pk_val] = cnt
        return result

    def _query_column_profiles(
        self,
        db: Database,
        table_ref: str,
        profile_cols: List[str],
        where: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        qualified_table = self._qualify_table(db, table_ref)

        # Check column types to know which ones support numeric SUM
        schema = {}
        try:
            schema = db.query_table_schema(tuple(table_ref.split(".")))
        except Exception:
            pass

        select_exprs = []
        is_numeric_map = {}
        for col in profile_cols:
            q_col = db.dialect.quote(col)
            info = schema.get(col)
            dt = info.data_type.lower() if info else ""
            is_num = any(t in dt for t in ("int", "double", "float", "decimal", "numeric", "real"))
            is_numeric_map[col] = is_num

            # null count, min, max
            select_exprs.append(f"COUNT(*) - COUNT({q_col}) AS {db.dialect.quote(f'{col}_nulls')}")
            select_exprs.append(f"MIN({q_col}) AS {db.dialect.quote(f'{col}_min')}")
            select_exprs.append(f"MAX({q_col}) AS {db.dialect.quote(f'{col}_max')}")
            if is_num:
                select_exprs.append(f"SUM(CAST({q_col} AS double)) AS {db.dialect.quote(f'{col}_sum')}")

        sql = f"SELECT {', '.join(select_exprs)} FROM {qualified_table}"
        if where:
            sql += f" WHERE {where}"

        logger.debug(f"Executing column profile query on {db.name}: {sql}")
        try:
            rows = db.query(sql, list)
        except Exception as e:
            logger.warning(f"Column profiling query failed on {db.name}: {e}")
            return {}

        if not rows:
            return {}

        row = rows[0]
        result: Dict[str, Dict[str, Any]] = {}
        idx = 0
        for col in profile_cols:
            null_cnt = int(row[idx]) if row[idx] is not None else 0
            min_val = str(row[idx + 1]) if row[idx + 1] is not None else None
            max_val = str(row[idx + 2]) if row[idx + 2] is not None else None
            idx += 3
            if is_numeric_map.get(col):
                sum_val = float(row[idx]) if row[idx] is not None else None
                idx += 1
            else:
                sum_val = None

            result[col] = {
                "null_count": null_cnt,
                "min": min_val,
                "max": max_val,
                "sum": sum_val,
            }
        return result

    def validate(
        self,
        source_db: Database,
        target_db: Database,
        source_table: str,
        target_table: str,
        table_name: str = "",
        source_where: Optional[str] = None,
        target_where: Optional[str] = None,
        partition_column: Optional[str] = None,
        profile_columns: Optional[List[str]] = None,
        threshold_pct: float = 0.0,
    ) -> CountValidationResult:
        """Run complete Tier 1 Count & Volume validation."""
        start_time = time.time()
        table_label = table_name or source_table

        try:
            with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                src_fut = executor.submit(self._query_total_count, source_db, source_table, source_where)
                tgt_fut = executor.submit(self._query_total_count, target_db, target_table, target_where)
                src_count = src_fut.result()
                tgt_count = tgt_fut.result()

            count_diff = tgt_count - src_count
            base_count = max(src_count, 1)
            count_diff_pct = abs(count_diff) / base_count * 100.0
            passed = count_diff_pct <= threshold_pct

            # Partition breakdown if configured
            partition_diffs: List[PartitionCountDiff] = []
            if partition_column:
                with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                    src_part_fut = executor.submit(
                        self._query_partition_counts,
                        source_db,
                        source_table,
                        partition_column,
                        source_where,
                    )
                    tgt_part_fut = executor.submit(
                        self._query_partition_counts,
                        target_db,
                        target_table,
                        partition_column,
                        target_where,
                    )
                    src_parts = src_part_fut.result()
                    tgt_parts = tgt_part_fut.result()

                all_keys = sorted(set(src_parts.keys()) | set(tgt_parts.keys()))
                for key in all_keys:
                    s_c = src_parts.get(key)
                    t_c = tgt_parts.get(key)
                    if s_c is None:
                        status = "MISSING_IN_SOURCE"
                        delta = t_c or 0
                        pct = 100.0
                        passed = False
                    elif t_c is None:
                        status = "MISSING_IN_TARGET"
                        delta = -(s_c or 0)
                        pct = 100.0
                        passed = False
                    else:
                        delta = t_c - s_c
                        base = max(s_c, 1)
                        pct = abs(delta) / base * 100.0
                        status = "MATCH" if delta == 0 else "MISMATCH"
                        if pct > threshold_pct:
                            passed = False

                    partition_diffs.append(
                        PartitionCountDiff(
                            partition_key=key,
                            source_count=s_c,
                            target_count=t_c,
                            diff=delta,
                            diff_pct=round(pct, 4),
                            status=status,
                        )
                    )

            # Column profile if requested
            column_profiles: List[ColumnProfileDiff] = []
            if profile_columns:
                with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                    src_prof_fut = executor.submit(
                        self._query_column_profiles,
                        source_db,
                        source_table,
                        profile_columns,
                        source_where,
                    )
                    tgt_prof_fut = executor.submit(
                        self._query_column_profiles,
                        target_db,
                        target_table,
                        profile_columns,
                        target_where,
                    )
                    src_prof = src_prof_fut.result()
                    tgt_prof = tgt_prof_fut.result()

                for col in profile_columns:
                    s_info = src_prof.get(col, {})
                    t_info = tgt_prof.get(col, {})
                    null_diff = t_info.get("null_count", 0) - s_info.get("null_count", 0)

                    s_sum = s_info.get("sum")
                    t_sum = t_info.get("sum")
                    sum_diff = (t_sum - s_sum) if (s_sum is not None and t_sum is not None) else None

                    min_match = s_info.get("min") == t_info.get("min")
                    max_match = s_info.get("max") == t_info.get("max")
                    null_match = null_diff == 0
                    sum_match = sum_diff == 0 if sum_diff is not None else True

                    status = "MATCH" if (min_match and max_match and null_match and sum_match) else "MISMATCH"
                    column_profiles.append(
                        ColumnProfileDiff(
                            column_name=col,
                            source_null_count=s_info.get("null_count", 0),
                            target_null_count=t_info.get("null_count", 0),
                            null_diff=null_diff,
                            source_min=s_info.get("min"),
                            target_min=t_info.get("min"),
                            source_max=s_info.get("max"),
                            target_max=t_info.get("max"),
                            source_sum=s_sum,
                            target_sum=t_sum,
                            sum_diff=sum_diff,
                            status=status,
                        )
                    )

            elapsed = round(time.time() - start_time, 3)
            return CountValidationResult(
                table_name=table_label,
                source_table=source_table,
                target_table=target_table,
                source_total_count=src_count,
                target_total_count=tgt_count,
                count_diff=count_diff,
                count_diff_pct=round(count_diff_pct, 4),
                threshold_pct=threshold_pct,
                passed=passed,
                partition_column=partition_column,
                partition_diffs=partition_diffs,
                column_profiles=column_profiles,
                execution_time_seconds=elapsed,
            )

        except Exception as e:
            logger.error(f"Tier 1 count validation failed for {table_label}: {e}", exc_info=True)
            elapsed = round(time.time() - start_time, 3)
            return CountValidationResult(
                table_name=table_label,
                source_table=source_table,
                target_table=target_table,
                passed=False,
                execution_time_seconds=elapsed,
                error=str(e),
            )
