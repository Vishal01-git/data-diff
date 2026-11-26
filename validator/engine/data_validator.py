"""Tier 3: Data Diff Engine with Routing, Single-Scan Bucket Checksum, and Normalization."""

from concurrent.futures import ThreadPoolExecutor
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from pydantic import BaseModel, Field

from data_diff import connect_to_table, diff_tables
from data_diff.databases.base import Database
from data_diff.diff_tables import Algorithm
from validator.engine.normalizers import (
    compare_floats,
    normalize_decimal_val,
    normalize_string_val,
    normalize_timestamp_val,
    sql_normalize_timestamp,
)

logger = logging.getLogger("validator.data")


class MismatchSample(BaseModel):
    key_values: Dict[str, Any]
    diff_type: str  # EXCLUSIVE_SOURCE, EXCLUSIVE_TARGET, VALUE_DIFF
    column_diffs: Dict[str, Tuple[Any, Any]] = Field(default_factory=dict)  # col -> (source_val, target_val)


class BucketDiff(BaseModel):
    bucket_id: int
    source_count: int
    target_count: int
    source_checksum: int
    target_checksum: int
    is_match: bool


class DataValidationResult(BaseModel):
    table_name: str = ""
    source_table: str
    target_table: str
    mode_used: str  # joindiff, hashdiff, bucket_hash, keyless
    match_scope: str = "exact"  # exact | source_in_target
    passed: bool = True
    total_rows_compared: int = 0
    differing_rows_count: int = 0
    extra_target_rows_count: int = 0
    differing_buckets_count: int = 0
    total_buckets: int = 0
    bucket_diffs: List[BucketDiff] = Field(default_factory=list)
    mismatch_samples: List[MismatchSample] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error: Optional[str] = None


def _col_to_safe_string_expr(db: Database, col_name: str, raw_type: Optional[str] = None) -> str:
    """Convert a column to a NULL-safe, type-safe string SQL expression for hashing.

    Handles binary/varbinary types (where CAST AS VARCHAR fails in Athena/Trino),
    complex types (array/map/struct), timestamps, booleans, and wraps every column
    in COALESCE(..., '<NULL>') to avoid NULL poisoning in row concatenation.
    """
    quoted_col = db.dialect.quote(col_name)
    name = db.name.lower()
    dtype = (raw_type or "").lower().strip()

    if any(k in dtype for k in ("binary", "varbinary", "bytea", "blob")):
        if name in ("athena", "trino", "presto"):
            str_expr = f"to_hex({quoted_col})"
        elif name in ("postgresql", "postgres"):
            str_expr = f"encode({quoted_col}, 'hex')"
        elif name in ("duckdb", "maxcompute", "odps"):
            str_expr = f"hex({quoted_col})"
        elif name in ("mssql", "sqlserver"):
            str_expr = f"CONVERT(VARCHAR(MAX), {quoted_col}, 2)"
        else:
            str_expr = db.dialect.to_string(quoted_col)
    elif any(dtype.startswith(k) for k in ("array", "map", "struct", "row", "json")):
        if name in ("athena", "trino", "presto"):
            str_expr = f"json_format(cast({quoted_col} as json))"
        else:
            str_expr = db.dialect.to_string(quoted_col)
    elif any(k in dtype for k in ("timestamp", "datetime")):
        str_expr = sql_normalize_timestamp(name, quoted_col, precision=0)
    elif dtype in ("bool", "boolean", "bit"):
        if name in ("mssql", "sqlserver"):
            str_expr = f"CASE WHEN {quoted_col} = 1 THEN '1' ELSE '0' END"
        else:
            str_expr = f"CASE WHEN {quoted_col} THEN '1' ELSE '0' END"
    else:
        str_expr = db.dialect.to_string(quoted_col)

    return f"COALESCE({str_expr}, '<NULL>')"


def get_concat_row_expr(
    db: Database,
    columns: Sequence[str],
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate SQL expression concatenating column values into a deterministic string."""
    safe_cols = [
        _col_to_safe_string_expr(
            db,
            c,
            schema[c].data_type if (schema and c in schema) else None,
        )
        for c in columns
    ]
    if len(safe_cols) == 1:
        return safe_cols[0]

    name = db.name.lower()
    if name in ("athena", "trino", "presto"):
        # Use array_join to avoid deeply nested '||' AST limits on wide tables (70+ cols)
        return f"array_join(ARRAY[{', '.join(safe_cols)}], '|')"
    elif name in ("postgresql", "postgres", "duckdb", "snowflake"):
        return " || '|' || ".join(safe_cols)
    elif name in ("maxcompute", "odps", "mssql", "sqlserver", "mysql"):
        return f"CONCAT_WS('|', {', '.join(safe_cols)})"

    return " || '|' || ".join(safe_cols)


def get_bucket_expr(db: Database, pk_hash_expr: str, bucket_count: int) -> str:
    """Generate SQL expression mapping row hash to a deterministic bucket ID 0..bucket_count-1."""
    name = db.name.lower()
    if name in ("duckdb", "postgresql", "postgres", "mssql", "sqlserver"):
        return f"(ABS(CAST({pk_hash_expr} AS BIGINT)) % {bucket_count})"
    return f"MOD(ABS(CAST({pk_hash_expr} AS BIGINT)), {bucket_count})"


class DataValidator:
    """Tier 3 Validator: Multi-mode row data validation with Single-Scan Bucket Hash."""

    def __init__(self, max_concurrency: int = 2) -> None:
        self.max_concurrency = max_concurrency

    def _qualify_table(self, db: Database, table_ref: Union[str, Sequence[str]]) -> str:
        if isinstance(table_ref, str):
            parts = table_ref.split(".")
        else:
            parts = list(table_ref)
        return ".".join(db.dialect.quote(p) for p in parts)

    def _determine_mode(
        self,
        source_db: Database,
        target_db: Database,
        requested_mode: str,
        primary_keys: Sequence[str],
    ) -> str:
        if not primary_keys:
            return "keyless"

        req = requested_mode.lower().strip()
        if req in ("hashdiff", "joindiff", "bucket_hash", "keyless"):
            return req

        # mode == "auto"
        # If same database connection/instance -> joindiff
        if source_db is target_db or getattr(source_db, "_conn", None) == getattr(target_db, "_conn", None) is not None:
            return "joindiff"

        # If either side is Athena or MaxCompute -> bucket_hash for cost optimization
        s_name = source_db.name.lower()
        t_name = target_db.name.lower()
        if s_name in ("athena", "maxcompute", "odps") or t_name in ("athena", "maxcompute", "odps"):
            return "bucket_hash"

        return "hashdiff"

    def _validate_bucket_hash(
        self,
        source_db: Database,
        target_db: Database,
        source_table: str,
        target_table: str,
        primary_keys: Sequence[str],
        compared_columns: Sequence[str],
        source_where: Optional[str] = None,
        target_where: Optional[str] = None,
        bucket_count: int = 64,
        max_mismatch_samples: int = 100,
        float_abs_tol: float = 1e-4,
        src_schema: Optional[Dict[str, Any]] = None,
        tgt_schema: Optional[Dict[str, Any]] = None,
        match_scope: str = "exact",
    ) -> Tuple[bool, int, int, int, List[BucketDiff], List[MismatchSample]]:
        """Single-Scan Bucket Checksum execution: 1 query per side, followed by targeted sampling."""
        src_q_table = self._qualify_table(source_db, source_table)
        tgt_q_table = self._qualify_table(target_db, target_table)

        src_pk_expr = get_concat_row_expr(source_db, primary_keys, schema=src_schema)
        tgt_pk_expr = get_concat_row_expr(target_db, primary_keys, schema=tgt_schema)

        src_pk_hash = source_db.dialect.md5_as_int(src_pk_expr)
        tgt_pk_hash = target_db.dialect.md5_as_int(tgt_pk_expr)

        src_bucket_col = get_bucket_expr(source_db, src_pk_hash, bucket_count)
        tgt_bucket_col = get_bucket_expr(target_db, tgt_pk_hash, bucket_count)

        src_row_expr = get_concat_row_expr(source_db, compared_columns, schema=src_schema)
        tgt_row_expr = get_concat_row_expr(target_db, compared_columns, schema=tgt_schema)

        src_row_hash = source_db.dialect.md5_as_int(src_row_expr)
        tgt_row_hash = target_db.dialect.md5_as_int(tgt_row_expr)

        src_query = (
            f"SELECT {src_bucket_col} AS b_id, COUNT(*) AS row_cnt, "
            f"COALESCE(SUM(CAST({src_row_hash} AS DECIMAL(38, 0))), 0) AS chk "
            f"FROM {src_q_table} "
            f"{f'WHERE {source_where}' if source_where else ''} "
            f"GROUP BY {src_bucket_col} ORDER BY 1"
        )

        tgt_query = (
            f"SELECT {tgt_bucket_col} AS b_id, COUNT(*) AS row_cnt, "
            f"COALESCE(SUM(CAST({tgt_row_hash} AS DECIMAL(38, 0))), 0) AS chk "
            f"FROM {tgt_q_table} "
            f"{f'WHERE {target_where}' if target_where else ''} "
            f"GROUP BY {tgt_bucket_col} ORDER BY 1"
        )

        logger.info(f"Running bucket checksums (buckets={bucket_count})...")
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            src_fut = executor.submit(source_db.query, src_query, list)
            tgt_fut = executor.submit(target_db.query, tgt_query, list)
            src_rows = src_fut.result()
            tgt_rows = tgt_fut.result()

        src_buckets: Dict[int, Tuple[int, int]] = {int(r[0]): (int(r[1]), int(r[2])) for r in src_rows if r[0] is not None}
        tgt_buckets: Dict[int, Tuple[int, int]] = {int(r[0]): (int(r[1]), int(r[2])) for r in tgt_rows if r[0] is not None}

        bucket_diffs: List[BucketDiff] = []
        differing_bucket_ids: List[int] = []
        total_rows = 0
        extra_target_rows = 0

        for b_id in range(bucket_count):
            s_cnt, s_chk = src_buckets.get(b_id, (0, 0))
            t_cnt, t_chk = tgt_buckets.get(b_id, (0, 0))
            total_rows += s_cnt

            if match_scope == "source_in_target":
                if s_cnt == 0:
                    extra_target_rows += t_cnt
                    is_match = True
                else:
                    is_match = (s_cnt == t_cnt) and (s_chk == t_chk)
            else:
                is_match = (s_cnt == t_cnt) and (s_chk == t_chk)

            if not is_match:
                differing_bucket_ids.append(b_id)

            bucket_diffs.append(
                BucketDiff(
                    bucket_id=b_id,
                    source_count=s_cnt,
                    target_count=t_cnt,
                    source_checksum=s_chk,
                    target_checksum=t_chk,
                    is_match=is_match,
                )
            )

        if not differing_bucket_ids:
            return True, total_rows, extra_target_rows, 0, bucket_diffs, []

        # Sample rows only from differing buckets
        logger.info(f"Sampling mismatched rows from {len(differing_bucket_ids)} differing buckets...")
        mismatch_samples: List[MismatchSample] = []
        cols_to_fetch = list(primary_keys) + [c for c in compared_columns if c not in primary_keys]

        src_cols_str = ", ".join(source_db.dialect.quote(c) for c in cols_to_fetch)
        tgt_cols_str = ", ".join(target_db.dialect.quote(c) for c in cols_to_fetch)

        for b_id in differing_bucket_ids:
            if len(mismatch_samples) >= max_mismatch_samples and match_scope != "source_in_target":
                break

            src_sample_sql = (
                f"SELECT {src_cols_str} FROM {src_q_table} "
                f"WHERE {src_bucket_col} = {b_id} "
                f"{f'AND ({source_where})' if source_where else ''}"
            )
            tgt_sample_sql = (
                f"SELECT {tgt_cols_str} FROM {tgt_q_table} "
                f"WHERE {tgt_bucket_col} = {b_id} "
                f"{f'AND ({target_where})' if target_where else ''}"
            )

            with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                s_rows_fut = executor.submit(source_db.query, src_sample_sql, list)
                t_rows_fut = executor.submit(target_db.query, tgt_sample_sql, list)
                s_sample_rows = s_rows_fut.result()
                t_sample_rows = t_rows_fut.result()

            pk_len = len(primary_keys)
            s_map = {tuple(str(r[i]) for i in range(pk_len)): r for r in s_sample_rows}
            t_map = {tuple(str(r[i]) for i in range(pk_len)): r for r in t_sample_rows}

            # Exclusive source (missing in target) -> always a failure
            for k in set(s_map.keys()) - set(t_map.keys()):
                if len(mismatch_samples) >= max_mismatch_samples:
                    break
                row = s_map[k]
                k_dict = {primary_keys[i]: row[i] for i in range(pk_len)}
                mismatch_samples.append(MismatchSample(key_values=k_dict, diff_type="EXCLUSIVE_SOURCE"))

            # Exclusive target (extra in target) -> allowed in source_in_target mode!
            extra_keys = set(t_map.keys()) - set(s_map.keys())
            if match_scope == "source_in_target":
                extra_target_rows += len(extra_keys)
            else:
                for k in extra_keys:
                    if len(mismatch_samples) >= max_mismatch_samples:
                        break
                    row = t_map[k]
                    k_dict = {primary_keys[i]: row[i] for i in range(pk_len)}
                    mismatch_samples.append(MismatchSample(key_values=k_dict, diff_type="EXCLUSIVE_TARGET"))

            # Common keys with differing values -> always a failure
            for k in set(s_map.keys()) & set(t_map.keys()):
                if len(mismatch_samples) >= max_mismatch_samples:
                    break
                s_r = s_map[k]
                t_r = t_map[k]
                col_diffs = {}
                for idx, col in enumerate(cols_to_fetch[pk_len:], start=pk_len):
                    s_val = s_r[idx]
                    t_val = t_r[idx]
                    if not compare_floats(s_val, t_val, abs_tol=float_abs_tol) if (
                        isinstance(s_val, (int, float)) and isinstance(t_val, (int, float))
                    ) else str(s_val) != str(t_val):
                        col_diffs[col] = (s_val, t_val)

                if col_diffs:
                    k_dict = {primary_keys[i]: s_r[i] for i in range(pk_len)}
                    mismatch_samples.append(
                        MismatchSample(
                            key_values=k_dict,
                            diff_type="VALUE_DIFF",
                            column_diffs=col_diffs,
                        )
                    )

        if match_scope == "source_in_target":
            passed = len(mismatch_samples) == 0
            actual_diff_buckets = 0 if passed else len(differing_bucket_ids)
        else:
            passed = len(differing_bucket_ids) == 0
            actual_diff_buckets = len(differing_bucket_ids)

        return passed, total_rows, extra_target_rows, actual_diff_buckets, bucket_diffs, mismatch_samples

    def _validate_keyless(
        self,
        source_db: Database,
        target_db: Database,
        source_table: str,
        target_table: str,
        compared_columns: Sequence[str],
        source_where: Optional[str] = None,
        target_where: Optional[str] = None,
        src_schema: Optional[Dict[str, Any]] = None,
        tgt_schema: Optional[Dict[str, Any]] = None,
        match_scope: str = "exact",
        bucket_count: int = 64,
        max_mismatch_samples: int = 100,
    ) -> Tuple[bool, int, int, int, List[MismatchSample]]:
        """Keyless table validation: compares row hash frequencies across buckets and extracts mismatch samples."""
        src_q_table = self._qualify_table(source_db, source_table)
        tgt_q_table = self._qualify_table(target_db, target_table)

        src_row_expr = get_concat_row_expr(source_db, compared_columns, schema=src_schema)
        tgt_row_expr = get_concat_row_expr(target_db, compared_columns, schema=tgt_schema)

        src_row_hash = source_db.dialect.md5_as_int(src_row_expr)
        tgt_row_hash = target_db.dialect.md5_as_int(tgt_row_expr)

        # Fast 2-query check when exact parity is requested
        src_sql = (
            f"SELECT COUNT(*), COUNT(DISTINCT {src_row_hash}), "
            f"COALESCE(SUM(CAST({src_row_hash} AS DECIMAL(38, 0))), 0) "
            f"FROM {src_q_table} {f'WHERE {source_where}' if source_where else ''}"
        )
        tgt_sql = (
            f"SELECT COUNT(*), COUNT(DISTINCT {tgt_row_hash}), "
            f"COALESCE(SUM(CAST({tgt_row_hash} AS DECIMAL(38, 0))), 0) "
            f"FROM {tgt_q_table} {f'WHERE {target_where}' if target_where else ''}"
        )

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            src_fut = executor.submit(source_db.query, src_sql, list)
            tgt_fut = executor.submit(target_db.query, tgt_sql, list)
            src_res = src_fut.result()[0]
            tgt_res = tgt_fut.result()[0]

        src_cnt, src_dist, src_sum = int(src_res[0]), int(src_res[1]), int(src_res[2])
        tgt_cnt, tgt_dist, tgt_sum = int(tgt_res[0]), int(tgt_res[1]), int(tgt_res[2])

        if (src_cnt == tgt_cnt) and (src_dist == tgt_dist) and (src_sum == tgt_sum):
            return True, src_cnt, 0, 0, []

        # Drill down using deterministic row_hash buckets so we never OOM on large tables
        logger.info("Keyless checksums differ (or subset mode active); drilling down via hash buckets for sample rows...")
        src_b_expr = get_bucket_expr(source_db, src_row_hash, bucket_count)
        tgt_b_expr = get_bucket_expr(target_db, tgt_row_hash, bucket_count)

        src_bsql = (
            f"SELECT {src_b_expr} AS b_id, COUNT(*), COALESCE(SUM(CAST({src_row_hash} AS DECIMAL(38, 0))), 0) "
            f"FROM {src_q_table} {f'WHERE {source_where}' if source_where else ''} GROUP BY {src_b_expr}"
        )
        tgt_bsql = (
            f"SELECT {tgt_b_expr} AS b_id, COUNT(*), COALESCE(SUM(CAST({tgt_row_hash} AS DECIMAL(38, 0))), 0) "
            f"FROM {tgt_q_table} {f'WHERE {target_where}' if target_where else ''} GROUP BY {tgt_b_expr}"
        )

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            sb_fut = executor.submit(source_db.query, src_bsql, list)
            tb_fut = executor.submit(target_db.query, tgt_bsql, list)
            sb_rows = sb_fut.result()
            tb_rows = tb_fut.result()

        sb_map = {int(r[0]): (int(r[1]), int(r[2])) for r in sb_rows if r[0] is not None}
        tb_map = {int(r[0]): (int(r[1]), int(r[2])) for r in tb_rows if r[0] is not None}

        diff_buckets: List[int] = []
        extra_target_rows = 0
        for b_id in range(bucket_count):
            s_c, s_s = sb_map.get(b_id, (0, 0))
            t_c, t_s = tb_map.get(b_id, (0, 0))
            if match_scope == "source_in_target":
                if s_c == 0:
                    extra_target_rows += t_c
                elif (s_c != t_c) or (s_s != t_s):
                    diff_buckets.append(b_id)
            else:
                if (s_c != t_c) or (s_s != t_s):
                    diff_buckets.append(b_id)

        mismatch_samples: List[MismatchSample] = []
        missing_in_target_count = 0
        cols_preview = list(compared_columns[:6])  # Show up to 6 key columns for keyless sample context
        src_cols_sql = ", ".join(source_db.dialect.quote(c) for c in cols_preview)
        tgt_cols_sql = ", ".join(target_db.dialect.quote(c) for c in cols_preview)

        for b_id in diff_buckets:
            if len(mismatch_samples) >= max_mismatch_samples and match_scope != "source_in_target":
                break

            sf_sql = (
                f"SELECT {src_row_hash} AS h, COUNT(*) FROM {src_q_table} "
                f"WHERE {src_b_expr} = {b_id} {f'AND ({source_where})' if source_where else ''} "
                f"GROUP BY {src_row_hash}"
            )
            tf_sql = (
                f"SELECT {tgt_row_hash} AS h, COUNT(*) FROM {tgt_q_table} "
                f"WHERE {tgt_b_expr} = {b_id} {f'AND ({target_where})' if target_where else ''} "
                f"GROUP BY {tgt_row_hash}"
            )
            with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                sf_fut = executor.submit(source_db.query, sf_sql, list)
                tf_fut = executor.submit(target_db.query, tf_sql, list)
                sf_map = {int(r[0]): int(r[1]) for r in sf_fut.result() if r[0] is not None}
                tf_map = {int(r[0]): int(r[1]) for r in tf_fut.result() if r[0] is not None}

            # Hashes missing or with lower frequency in target
            src_only_hashes = [h for h, cnt in sf_map.items() if cnt > tf_map.get(h, 0)]
            for h in src_only_hashes:
                missing_in_target_count += sf_map[h] - tf_map.get(h, 0)

            if src_only_hashes and len(mismatch_samples) < max_mismatch_samples:
                hash_list = ", ".join(str(h) for h in src_only_hashes[:25])
                sample_q = (
                    f"SELECT {src_cols_sql} FROM {src_q_table} "
                    f"WHERE {src_b_expr} = {b_id} AND {src_row_hash} IN ({hash_list}) "
                    f"{f'AND ({source_where})' if source_where else ''} LIMIT {max_mismatch_samples - len(mismatch_samples)}"
                )
                for row in source_db.query(sample_q, list):
                    row_dict = {cols_preview[i]: row[i] for i in range(len(cols_preview))}
                    mismatch_samples.append(MismatchSample(key_values=row_dict, diff_type="EXCLUSIVE_SOURCE"))

            tgt_only_hashes = [h for h, cnt in tf_map.items() if cnt > sf_map.get(h, 0)]
            if match_scope == "source_in_target":
                for h in tgt_only_hashes:
                    extra_target_rows += tf_map[h] - sf_map.get(h, 0)
            elif tgt_only_hashes and len(mismatch_samples) < max_mismatch_samples:
                hash_list = ", ".join(str(h) for h in tgt_only_hashes[:25])
                sample_q = (
                    f"SELECT {tgt_cols_sql} FROM {tgt_q_table} "
                    f"WHERE {tgt_b_expr} = {b_id} AND {tgt_row_hash} IN ({hash_list}) "
                    f"{f'AND ({target_where})' if target_where else ''} LIMIT {max_mismatch_samples - len(mismatch_samples)}"
                )
                for row in target_db.query(sample_q, list):
                    row_dict = {cols_preview[i]: row[i] for i in range(len(cols_preview))}
                    mismatch_samples.append(MismatchSample(key_values=row_dict, diff_type="EXCLUSIVE_TARGET"))

        if match_scope == "source_in_target":
            is_match = missing_in_target_count == 0
            diff_cnt = missing_in_target_count
        else:
            is_match = (src_cnt == tgt_cnt) and (src_dist == tgt_dist) and (src_sum == tgt_sum)
            diff_cnt = max(abs(tgt_cnt - src_cnt), len(mismatch_samples)) if not is_match else 0

        return is_match, src_cnt, extra_target_rows, diff_cnt, mismatch_samples

    def validate(
        self,
        source_db: Database,
        target_db: Database,
        source_table: str,
        target_table: str,
        table_name: str = "",
        primary_keys: Optional[Sequence[str]] = None,
        compared_columns: Optional[Sequence[str]] = None,
        exclude_columns: Optional[Sequence[str]] = None,
        source_where: Optional[str] = None,
        target_where: Optional[str] = None,
        mode: str = "auto",
        match_scope: str = "exact",
        bucket_count: int = 64,
        max_mismatch_samples: int = 100,
        float_abs_tol: float = 1e-4,
    ) -> DataValidationResult:
        """Run complete Tier 3 Data Validation with automatic mode routing."""
        start_time = time.time()
        table_label = table_name or source_table
        pks = list(primary_keys or [])
        excludes = set(c.lower() for c in (exclude_columns or []))

        # Introspect schemas for type-aware hashing and column discovery
        src_schema = None
        tgt_schema = None
        try:
            src_schema = source_db.query_table_schema(tuple(source_table.split(".")))
        except Exception as e:
            logger.debug(f"Could not introspect source schema for type-aware hashing: {e}")

        try:
            tgt_schema = target_db.query_table_schema(tuple(target_table.split(".")))
        except Exception as e:
            logger.debug(f"Could not introspect target schema for type-aware hashing: {e}")

        if not compared_columns:
            if src_schema:
                comp_cols = [c for c in src_schema.keys() if c.lower() not in excludes]
            else:
                raise RuntimeError(f"Could not determine columns for table {source_table}")
        else:
            comp_cols = [c for c in compared_columns if c.lower() not in excludes]

        effective_mode = self._determine_mode(source_db, target_db, mode, pks)
        logger.info(f"Running Tier 3 Data Validation for {table_label} (mode={effective_mode}, scope={match_scope})...")

        try:
            if effective_mode == "bucket_hash":
                passed, total_rows, extra_tgt_rows, diff_buckets, bucket_diffs, samples = self._validate_bucket_hash(
                    source_db=source_db,
                    target_db=target_db,
                    source_table=source_table,
                    target_table=target_table,
                    primary_keys=pks,
                    compared_columns=comp_cols,
                    source_where=source_where,
                    target_where=target_where,
                    bucket_count=bucket_count,
                    max_mismatch_samples=max_mismatch_samples,
                    float_abs_tol=float_abs_tol,
                    src_schema=src_schema,
                    tgt_schema=tgt_schema,
                    match_scope=match_scope,
                )
                elapsed = round(time.time() - start_time, 3)
                return DataValidationResult(
                    table_name=table_label,
                    source_table=source_table,
                    target_table=target_table,
                    mode_used=effective_mode,
                    match_scope=match_scope,
                    passed=passed,
                    total_rows_compared=total_rows,
                    differing_rows_count=len(samples) if samples else (0 if passed else diff_buckets),
                    extra_target_rows_count=extra_tgt_rows,
                    differing_buckets_count=diff_buckets,
                    total_buckets=bucket_count,
                    bucket_diffs=bucket_diffs,
                    mismatch_samples=samples,
                    execution_time_seconds=elapsed,
                )

            elif effective_mode == "keyless":
                passed, total_rows, extra_tgt_rows, diff_cnt, samples = self._validate_keyless(
                    source_db=source_db,
                    target_db=target_db,
                    source_table=source_table,
                    target_table=target_table,
                    compared_columns=comp_cols,
                    source_where=source_where,
                    target_where=target_where,
                    src_schema=src_schema,
                    tgt_schema=tgt_schema,
                    match_scope=match_scope,
                    bucket_count=bucket_count,
                    max_mismatch_samples=max_mismatch_samples,
                )
                elapsed = round(time.time() - start_time, 3)
                return DataValidationResult(
                    table_name=table_label,
                    source_table=source_table,
                    target_table=target_table,
                    mode_used=effective_mode,
                    match_scope=match_scope,
                    passed=passed,
                    total_rows_compared=total_rows,
                    differing_rows_count=diff_cnt,
                    extra_target_rows_count=extra_tgt_rows,
                    mismatch_samples=samples,
                    execution_time_seconds=elapsed,
                )

            else:
                # Standard joindiff or hashdiff
                s_seg = connect_to_table(
                    source_db,
                    source_table,
                    key_columns=tuple(pks),
                    extra_columns=tuple(comp_cols),
                    where=source_where,
                )
                t_seg = connect_to_table(
                    target_db,
                    target_table,
                    key_columns=tuple(pks),
                    extra_columns=tuple(comp_cols),
                    where=target_where,
                )

                algo = Algorithm.JOINDIFF if effective_mode == "joindiff" else Algorithm.HASHDIFF
                diff_iter = diff_tables(s_seg, t_seg, algorithm=algo)

                samples = []
                diff_count = 0
                for diff_op, diff_row in diff_iter:
                    diff_count += 1
                    if len(samples) < max_mismatch_samples:
                        diff_type = "EXCLUSIVE_SOURCE" if diff_op == "-" else "EXCLUSIVE_TARGET"
                        k_dict = {pks[i]: diff_row[i] for i in range(min(len(pks), len(diff_row)))}
                        samples.append(MismatchSample(key_values=k_dict, diff_type=diff_type))

                passed = diff_count == 0
                elapsed = round(time.time() - start_time, 3)
                return DataValidationResult(
                    table_name=table_label,
                    source_table=source_table,
                    target_table=target_table,
                    mode_used=effective_mode,
                    passed=passed,
                    differing_rows_count=diff_count,
                    mismatch_samples=samples,
                    execution_time_seconds=elapsed,
                )

        except Exception as e:
            logger.error(f"Tier 3 data validation failed for {table_label}: {e}", exc_info=True)
            elapsed = round(time.time() - start_time, 3)
            return DataValidationResult(
                table_name=table_label,
                source_table=source_table,
                target_table=target_table,
                mode_used=effective_mode,
                passed=False,
                execution_time_seconds=elapsed,
                error=str(e),
            )
