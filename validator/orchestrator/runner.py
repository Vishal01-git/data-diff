"""Concurrent Batch Runner for multi-table SIT reconciliation."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from validator.config.connections import ConnectionResolver
from validator.config.manifest import SITManifest, TableConfig
from validator.engine.count_validator import CountValidationResult, CountValidator
from validator.engine.data_validator import DataValidationResult, DataValidator
from validator.engine.schema_validator import SchemaValidationResult, SchemaValidator
from validator.observability import global_metrics
from validator.orchestrator.checkpoint import CheckpointTracker
from validator.security import redact_sample_values, sanitize_error_message

logger = logging.getLogger("validator.runner")


class TableValidationResult(BaseModel):
    table_name: str
    source_table: str
    target_table: str
    status: str  # PASSED, FAILED, SKIPPED, ERROR
    passed: bool
    count_result: Optional[CountValidationResult] = None
    schema_result: Optional[SchemaValidationResult] = None
    data_result: Optional[DataValidationResult] = None
    error_message: Optional[str] = None
    execution_time_seconds: float = 0.0
    bytes_scanned: int = 0
    query_count: int = 0


class BatchValidationResult(BaseModel):
    run_name: str
    total_tables: int = 0
    passed_tables: int = 0
    failed_tables: int = 0
    skipped_tables: int = 0
    total_rows_compared: int = 0
    total_duration_seconds: float = 0.0
    total_bytes_scanned: int = 0
    total_queries: int = 0
    include_sample_values: bool = False
    overall_status: str = "PASSED"  # PASSED, FAILED
    table_results: List[TableValidationResult] = Field(default_factory=list)
    start_time_iso: str = ""
    end_time_iso: str = ""


def retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> Any:
    """Execute a function with exponential backoff on transient exceptions."""
    delay = base_delay
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exc = e
            if attempt == max_retries - 1:
                break
            logger.warning(
                f"Transient error on attempt {attempt + 1}/{max_retries}: {e}. Retrying in {delay:.2f}s..."
            )
            time.sleep(delay)
            delay *= backoff_factor

    raise last_exc  # type: ignore[misc]


class BatchRunner:
    """Orchestrates multi-table SIT runs with concurrency control, checkpoints, and fail-fast rules."""

    def __init__(
        self,
        manifest: SITManifest,
        resolver: Optional[ConnectionResolver] = None,
        checkpoint_tracker: Optional[CheckpointTracker] = None,
        max_workers: int = 4,
        resume: bool = False,
        include_sample_values: Optional[bool] = None,
    ) -> None:
        self.manifest = manifest
        self.resolver = resolver or ConnectionResolver(manifest.connections)
        self.checkpoint = checkpoint_tracker or CheckpointTracker(manifest.run_name)
        self.max_workers = max_workers
        self.resume = resume
        if include_sample_values is not None:
            self.include_sample_values = include_sample_values
        else:
            self.include_sample_values = manifest.defaults.reporting.include_sample_values

        # Per-connection semaphores to prevent cloud rate-limiting
        self.connection_semaphores: Dict[str, threading.Semaphore] = {
            conn_name: threading.Semaphore(cfg.max_concurrency)
            for conn_name, cfg in manifest.connections.items()
        }

        # Validators
        self.count_validator = CountValidator(max_concurrency=2)
        self.schema_validator = SchemaValidator()
        self.data_validator = DataValidator(max_concurrency=2)

    def _get_semaphore(self, conn_name: str) -> threading.Semaphore:
        if conn_name not in self.connection_semaphores:
            self.connection_semaphores[conn_name] = threading.Semaphore(5)
        return self.connection_semaphores[conn_name]

    def _validate_single_table(self, tbl: TableConfig) -> TableValidationResult:
        """Run all configured validation tiers for a single table."""
        start_time = time.time()
        logger.info(f"Starting validation for table '{tbl.name}'...")

        src_conn_name = tbl.source.connection
        tgt_conn_name = tbl.target.connection

        # Acquire semaphores in lexicographical order to prevent deadlocks
        first_conn, second_conn = sorted([src_conn_name, tgt_conn_name])
        sem1 = self._get_semaphore(first_conn)
        sem2 = self._get_semaphore(second_conn)

        count_res: Optional[CountValidationResult] = None
        schema_res: Optional[SchemaValidationResult] = None
        data_res: Optional[DataValidationResult] = None
        overall_passed = True
        error_msg: Optional[str] = None

        with sem1:
            with sem2:
                try:
                    src_db = self.resolver.get_database(src_conn_name)
                    tgt_db = self.resolver.get_database(tgt_conn_name)

                    active_tiers = tbl.tiers or self.manifest.defaults.tiers

                    # Tier 1: Count & Volume
                    if "count" in active_tiers:
                        logger.debug(f"[{tbl.name}] Running Tier 1 Count Validation...")
                        count_res = retry_with_backoff(
                            lambda: self.count_validator.validate(
                                source_db=src_db,
                                target_db=tgt_db,
                                source_table=tbl.source.table,
                                target_table=tbl.target.table,
                                table_name=tbl.name,
                                source_where=tbl.source.where,
                                target_where=tbl.target.where,
                                partition_column=tbl.partition_column,
                                threshold_pct=tbl.threshold_pct,
                            )
                        )
                        if not count_res.passed:
                            overall_passed = False

                    # Tier 2: Schema Validation
                    if "schema" in active_tiers:
                        logger.debug(f"[{tbl.name}] Running Tier 2 Schema Validation...")
                        schema_res = retry_with_backoff(
                            lambda: self.schema_validator.validate(
                                source_db=src_db,
                                target_db=tgt_db,
                                source_table=tbl.source.table,
                                target_table=tbl.target.table,
                                table_name=tbl.name,
                                exclude_columns=tbl.exclude_columns,
                                column_mapping=tbl.column_mapping,
                                allow_extra_target_columns=tbl.allow_extra_target_columns,
                            )
                        )
                        if not schema_res.passed:
                            overall_passed = False

                    # Fail-fast evaluation before Tier 3 Data Diff
                    skip_data = False
                    ff_cfg = self.manifest.defaults.fail_fast

                    if "data" in active_tiers:
                        if schema_res and not schema_res.passed and ff_cfg.skip_data_on_schema_fail:
                            logger.warning(
                                f"[{tbl.name}] Skipping Tier 3 Data Diff due to schema failure (fail_fast)."
                            )
                            skip_data = True

                        if count_res and count_res.count_diff_pct > ff_cfg.skip_data_if_count_diff_pct_gt:
                            logger.warning(
                                f"[{tbl.name}] Skipping Tier 3 Data Diff: count diff {count_res.count_diff_pct:.1f}% "
                                f"> threshold {ff_cfg.skip_data_if_count_diff_pct_gt}%."
                            )
                            skip_data = True

                        if not skip_data:
                            logger.debug(f"[{tbl.name}] Running Tier 3 Data Diff...")
                            dd_cfg = tbl.data_diff or self.manifest.defaults.data_diff
                            data_res = retry_with_backoff(
                                lambda: self.data_validator.validate(
                                    source_db=src_db,
                                    target_db=tgt_db,
                                    source_table=tbl.source.table,
                                    target_table=tbl.target.table,
                                    table_name=tbl.name,
                                    primary_keys=tbl.primary_keys,
                                    exclude_columns=tbl.exclude_columns,
                                    source_where=tbl.source.where,
                                    target_where=tbl.target.where,
                                    mode=dd_cfg.mode,
                                    bucket_count=dd_cfg.bucket_count,
                                    max_mismatch_samples=dd_cfg.max_mismatch_samples,
                                    float_abs_tol=dd_cfg.float_abs_tol,
                                )
                            )
                            if not data_res.passed:
                                overall_passed = False

                except Exception as e:
                    logger.error(f"[{tbl.name}] Validation failed with error: {e}", exc_info=True)
                    overall_passed = False
                    error_msg = str(e)

        elapsed = round(time.time() - start_time, 3)
        status = "PASSED" if overall_passed else ("ERROR" if error_msg else "FAILED")

        # Athena scanned bytes and query tracking
        tbl_bytes_scanned = 0
        tbl_queries = 0
        try:
            if hasattr(src_db, "last_query_stats") and isinstance(src_db.last_query_stats, dict):
                tbl_bytes_scanned += src_db.last_query_stats.get("data_scanned_in_bytes", 0) or 0
            if hasattr(tgt_db, "last_query_stats") and isinstance(tgt_db.last_query_stats, dict):
                tbl_bytes_scanned += tgt_db.last_query_stats.get("data_scanned_in_bytes", 0) or 0
        except Exception:
            pass

        # Estimate queries executed based on active tiers
        if "count" in active_tiers:
            tbl_queries += 2
        if "schema" in active_tiers:
            tbl_queries += 2
        if "data" in active_tiers and not skip_data:
            tbl_queries += 2

        # Redact mismatch samples if include_sample_values is False
        if data_res and data_res.mismatch_samples and not self.include_sample_values:
            for sample in data_res.mismatch_samples:
                sample.column_diffs = redact_sample_values(
                    sample.column_diffs, include_sample_values=False
                )

        # Sanitize error message if present
        if error_msg:
            error_msg = sanitize_error_message(error_msg)

        global_metrics.record_table_metrics(
            table_name=tbl.name,
            duration_seconds=elapsed,
            query_count=tbl_queries,
            bytes_scanned=tbl_bytes_scanned,
        )

        result = TableValidationResult(
            table_name=tbl.name,
            source_table=tbl.source.table,
            target_table=tbl.target.table,
            status=status,
            passed=overall_passed,
            count_result=count_res,
            schema_result=schema_res,
            data_result=data_res,
            error_message=error_msg,
            execution_time_seconds=elapsed,
            bytes_scanned=tbl_bytes_scanned,
            query_count=tbl_queries,
        )

        # Save to checkpoint
        self.checkpoint.save_checkpoint(
            table_name=tbl.name,
            status=status,
            passed=overall_passed,
            result_data=result.model_dump(),
        )

        return result

    def run(self) -> BatchValidationResult:
        """Run all table validations in the manifest concurrently."""
        start_time_dt = datetime.now(timezone.utc)
        start_time_iso = start_time_dt.isoformat()
        start_epoch = time.time()

        table_results: List[TableValidationResult] = []
        tables_to_run: List[TableConfig] = []

        # Check for resumable checkpoints
        for tbl in self.manifest.tables:
            if self.resume and self.checkpoint.is_completed_and_passed(tbl.name):
                logger.info(f"Resuming: table '{tbl.name}' already passed previously. Skipping.")
                prev_data = self.checkpoint.get_table_result(tbl.name)
                if prev_data:
                    res = TableValidationResult.model_validate(prev_data)
                    res.status = "SKIPPED"
                    table_results.append(res)
                    continue

            tables_to_run.append(tbl)

        # Execute remaining tables concurrently
        if tables_to_run:
            workers = min(self.max_workers, len(tables_to_run))
            logger.info(f"Executing {len(tables_to_run)} tables across {workers} worker threads...")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._validate_single_table, tbl): tbl for tbl in tables_to_run}
                for fut in as_completed(futures):
                    res = fut.result()
                    table_results.append(res)

        # Compute aggregate metrics
        total_duration = round(time.time() - start_epoch, 3)
        end_time_iso = datetime.now(timezone.utc).isoformat()

        passed_count = sum(1 for r in table_results if r.passed and r.status != "SKIPPED")
        failed_count = sum(1 for r in table_results if not r.passed)
        skipped_count = sum(1 for r in table_results if r.status == "SKIPPED")
        total_rows = sum(
            (r.count_result.source_total_count if r.count_result else 0) for r in table_results
        )
        total_bytes = sum(r.bytes_scanned for r in table_results)
        total_queries = sum(r.query_count for r in table_results)

        overall_status = "PASSED" if (failed_count == 0) else "FAILED"

        return BatchValidationResult(
            run_name=self.manifest.run_name,
            total_tables=len(self.manifest.tables),
            passed_tables=passed_count,
            failed_tables=failed_count,
            skipped_tables=skipped_count,
            total_rows_compared=total_rows,
            total_duration_seconds=total_duration,
            total_bytes_scanned=total_bytes,
            total_queries=total_queries,
            include_sample_values=self.include_sample_values,
            overall_status=overall_status,
            table_results=table_results,
            start_time_iso=start_time_iso,
            end_time_iso=end_time_iso,
        )
