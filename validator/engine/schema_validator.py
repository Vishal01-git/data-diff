"""Tier 2: Cross-Dialect Schema Validator."""

import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from pydantic import BaseModel, Field
import yaml

from data_diff.databases.base import Database
from data_diff.schema import RawColumnInfo

logger = logging.getLogger("validator.schema")

DEFAULT_TYPE_MAPPINGS_PATH = Path(__file__).resolve().parent.parent.parent / "configs" / "type_mappings.yaml"


class ColumnSchemaDiff(BaseModel):
    column_name: str
    source_column_name: Optional[str] = None
    target_column_name: Optional[str] = None
    source_raw_type: Optional[str] = None
    target_raw_type: Optional[str] = None
    source_canonical_type: Optional[str] = None
    target_canonical_type: Optional[str] = None
    source_precision: Optional[int] = None
    target_precision: Optional[int] = None
    source_scale: Optional[int] = None
    target_scale: Optional[int] = None
    diff_type: str  # MATCH, MISSING_IN_TARGET, EXTRA_IN_TARGET, TYPE_MISMATCH, TYPE_WIDENING, PRECISION_TRUNCATION, PRECISION_WIDENING, SCALE_MISMATCH
    severity: str  # OK, WARN, ERROR
    message: str


class SchemaValidationResult(BaseModel):
    table_name: str = ""
    source_table: str
    target_table: str
    passed: bool = True
    column_diffs: List[ColumnSchemaDiff] = Field(default_factory=list)
    missing_columns_count: int = 0
    extra_columns_count: int = 0
    type_mismatch_count: int = 0
    precision_loss_count: int = 0
    execution_time_seconds: float = 0.0
    error: Optional[str] = None


class SchemaValidator:
    """Tier 2 Validator: Canonical schema mapping, precision/scale drift, column renames & exclusions."""

    def __init__(self, type_mappings_path: Optional[Union[str, Path]] = None) -> None:
        self.type_mappings_path = Path(type_mappings_path or DEFAULT_TYPE_MAPPINGS_PATH)
        self.canonical_types: Dict[str, Dict[str, List[str]]] = {}
        self.type_widening: Dict[str, List[str]] = {}
        self._load_type_mappings()

    def _load_type_mappings(self) -> None:
        if not self.type_mappings_path.exists():
            logger.warning(f"Type mappings configuration not found at {self.type_mappings_path}, using defaults.")
            return

        with open(self.type_mappings_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self.canonical_types = data.get("canonical_types", {})
        self.type_widening = data.get("type_widening", {})

    def resolve_canonical_type(
        self, dialect_name: str, raw_type: str
    ) -> Tuple[str, Optional[int], Optional[int]]:
        """Map dialect-specific raw data type to (canonical_type, precision, scale)."""
        dt_clean = raw_type.strip().lower()
        dialect_key = dialect_name.lower()
        # Common aliases
        if dialect_key in ("odps", "maxcompute"):
            dialect_key = "maxcompute"
        elif dialect_key in ("postgres", "postgresql"):
            dialect_key = "postgresql"
        elif dialect_key in ("mssql", "sqlserver"):
            dialect_key = "mssql"

        # Extract precision and scale if present e.g. decimal(10,2) or varchar(100)
        precision: Optional[int] = None
        scale: Optional[int] = None

        m_dec = re.search(r"(\w+)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", dt_clean)
        if m_dec:
            base_type = m_dec.group(1)
            precision = int(m_dec.group(2))
            scale = int(m_dec.group(3))
        else:
            m_single = re.search(r"(\w+)\s*\(\s*(\d+)\s*\)", dt_clean)
            if m_single:
                base_type = m_single.group(1)
                precision = int(m_single.group(2))
            else:
                base_type = re.split(r"[\s\(]", dt_clean)[0]

        # Check in loaded canonical types
        for canon_type, dialect_map in self.canonical_types.items():
            valid_raw_types = dialect_map.get(dialect_key, [])
            for pattern in valid_raw_types:
                pat_clean = pattern.lower()
                if dt_clean == pat_clean or base_type == pat_clean:
                    return canon_type, precision, scale

        # Heuristic fallbacks if not explicitly in YAML
        if "char" in base_type or "text" in base_type or "string" in base_type:
            return "STRING", precision, None
        if "int8" in base_type or "bigint" in base_type:
            return "INT64", None, None
        if "int4" in base_type or "int" in base_type or "integer" in base_type:
            return "INT32", None, None
        if "int2" in base_type or "smallint" in base_type:
            return "INT16", None, None
        if "tinyint" in base_type:
            return "INT8", None, None
        if "double" in base_type or "float8" in base_type:
            return "FLOAT64", None, None
        if "float" in base_type or "real" in base_type:
            return "FLOAT32", None, None
        if "decimal" in base_type or "numeric" in base_type or "money" in base_type:
            return "DECIMAL", precision or 38, scale or 0
        if "time" in base_type or "date" in base_type:
            return "TIMESTAMP" if "time" in base_type else "DATE", precision, None
        if "bool" in base_type or "bit" in base_type:
            return "BOOLEAN", None, None
        if "blob" in base_type or "byte" in base_type or "binary" in base_type:
            return "BINARY", None, None
        if "json" in base_type:
            return "JSON", None, None

        return "UNKNOWN", precision, scale

    def _split_table_path(self, table_ref: str) -> Tuple[str, ...]:
        return tuple(table_ref.split("."))

    def _introspect_schema(self, db: Database, table_ref: str) -> Dict[str, RawColumnInfo]:
        path = self._split_table_path(table_ref)
        return db.query_table_schema(path)

    def validate(
        self,
        source_db: Database,
        target_db: Database,
        source_table: str,
        target_table: str,
        table_name: str = "",
        exclude_columns: Optional[Sequence[str]] = None,
        column_mapping: Optional[Dict[str, str]] = None,
        allow_extra_target_columns: bool = False,
    ) -> SchemaValidationResult:
        """Run complete Tier 2 Schema Validation comparing canonical types, precision, and renames."""
        start_time = time.time()
        table_label = table_name or source_table
        excludes: Set[str] = set(c.lower() for c in (exclude_columns or []))
        col_map: Dict[str, str] = {k.lower(): v.lower() for k, v in (column_mapping or {}).items()}

        try:
            src_raw_schema = self._introspect_schema(source_db, source_table)
            tgt_raw_schema = self._introspect_schema(target_db, target_table)

            # Filter excluded columns and index by lower name
            src_cols: Dict[str, RawColumnInfo] = {
                k.lower(): v for k, v in src_raw_schema.items() if k.lower() not in excludes
            }
            tgt_cols: Dict[str, RawColumnInfo] = {
                k.lower(): v for k, v in tgt_raw_schema.items() if k.lower() not in excludes
            }

            diffs: List[ColumnSchemaDiff] = []
            missing_count = 0
            extra_count = 0
            type_mismatch_count = 0
            precision_loss_count = 0
            all_passed = True

            # Match source columns to target columns (applying renames)
            matched_tgt_cols: Set[str] = set()

            for src_name, src_info in src_cols.items():
                expected_tgt_name = col_map.get(src_name, src_name)
                tgt_info = tgt_cols.get(expected_tgt_name)

                src_canon, src_prec, src_scale = self.resolve_canonical_type(source_db.name, src_info.data_type)
                # If raw column info has parsed precision/scale, use them
                if src_info.numeric_precision is not None:
                    src_prec = src_info.numeric_precision
                if src_info.numeric_scale is not None:
                    src_scale = src_info.numeric_scale

                if tgt_info is None:
                    missing_count += 1
                    all_passed = False
                    diffs.append(
                        ColumnSchemaDiff(
                            column_name=expected_tgt_name,
                            source_column_name=src_name,
                            source_raw_type=src_info.data_type,
                            source_canonical_type=src_canon,
                            source_precision=src_prec,
                            source_scale=src_scale,
                            diff_type="MISSING_IN_TARGET",
                            severity="ERROR",
                            message=f"Column '{src_name}' is missing in target table (expected '{expected_tgt_name}').",
                        )
                    )
                    continue

                matched_tgt_cols.add(expected_tgt_name)
                tgt_canon, tgt_prec, tgt_scale = self.resolve_canonical_type(target_db.name, tgt_info.data_type)
                if tgt_info.numeric_precision is not None:
                    tgt_prec = tgt_info.numeric_precision
                if tgt_info.numeric_scale is not None:
                    tgt_scale = tgt_info.numeric_scale

                # Check canonical type match
                if src_canon == tgt_canon:
                    # Check precision/scale for numeric/decimal types
                    if src_canon == "DECIMAL":
                        prec_err = False
                        prec_warn = False
                        msg_parts = []

                        # Scale comparison
                        if src_scale is not None and tgt_scale is not None:
                            if tgt_scale < src_scale:
                                prec_err = True
                                msg_parts.append(f"scale truncated from {src_scale} to {tgt_scale}")
                            elif tgt_scale > src_scale:
                                prec_warn = True
                                msg_parts.append(f"scale widened from {src_scale} to {tgt_scale}")

                        # Precision comparison
                        if src_prec is not None and tgt_prec is not None:
                            if tgt_prec < src_prec:
                                prec_err = True
                                msg_parts.append(f"precision truncated from {src_prec} to {tgt_prec}")
                            elif tgt_prec > src_prec:
                                prec_warn = True
                                msg_parts.append(f"precision widened from {src_prec} to {tgt_prec}")

                        if prec_err:
                            precision_loss_count += 1
                            all_passed = False
                            diffs.append(
                                ColumnSchemaDiff(
                                    column_name=expected_tgt_name,
                                    source_column_name=src_name,
                                    target_column_name=expected_tgt_name,
                                    source_raw_type=src_info.data_type,
                                    target_raw_type=tgt_info.data_type,
                                    source_canonical_type=src_canon,
                                    target_canonical_type=tgt_canon,
                                    source_precision=src_prec,
                                    target_precision=tgt_prec,
                                    source_scale=src_scale,
                                    target_scale=tgt_scale,
                                    diff_type="PRECISION_TRUNCATION",
                                    severity="ERROR",
                                    message=f"Decimal precision loss: {', '.join(msg_parts)}.",
                                )
                            )
                        elif prec_warn:
                            diffs.append(
                                ColumnSchemaDiff(
                                    column_name=expected_tgt_name,
                                    source_column_name=src_name,
                                    target_column_name=expected_tgt_name,
                                    source_raw_type=src_info.data_type,
                                    target_raw_type=tgt_info.data_type,
                                    source_canonical_type=src_canon,
                                    target_canonical_type=tgt_canon,
                                    source_precision=src_prec,
                                    target_precision=tgt_prec,
                                    source_scale=src_scale,
                                    target_scale=tgt_scale,
                                    diff_type="PRECISION_WIDENING",
                                    severity="WARN",
                                    message=f"Decimal precision widened safely: {', '.join(msg_parts)}.",
                                )
                            )
                        else:
                            diffs.append(
                                ColumnSchemaDiff(
                                    column_name=expected_tgt_name,
                                    source_column_name=src_name,
                                    target_column_name=expected_tgt_name,
                                    source_raw_type=src_info.data_type,
                                    target_raw_type=tgt_info.data_type,
                                    source_canonical_type=src_canon,
                                    target_canonical_type=tgt_canon,
                                    source_precision=src_prec,
                                    target_precision=tgt_prec,
                                    source_scale=src_scale,
                                    target_scale=tgt_scale,
                                    diff_type="MATCH",
                                    severity="OK",
                                    message=f"Types match ({src_canon}).",
                                )
                            )
                    else:
                        diffs.append(
                            ColumnSchemaDiff(
                                column_name=expected_tgt_name,
                                source_column_name=src_name,
                                target_column_name=expected_tgt_name,
                                source_raw_type=src_info.data_type,
                                target_raw_type=tgt_info.data_type,
                                source_canonical_type=src_canon,
                                target_canonical_type=tgt_canon,
                                source_precision=src_prec,
                                target_precision=tgt_prec,
                                source_scale=src_scale,
                                target_scale=tgt_scale,
                                diff_type="MATCH",
                                severity="OK",
                                message=f"Types match ({src_canon}).",
                            )
                        )
                else:
                    # Check if safe type widening
                    allowed_widening = self.type_widening.get(tgt_canon, [])
                    if src_canon in allowed_widening:
                        diffs.append(
                            ColumnSchemaDiff(
                                column_name=expected_tgt_name,
                                source_column_name=src_name,
                                target_column_name=expected_tgt_name,
                                source_raw_type=src_info.data_type,
                                target_raw_type=tgt_info.data_type,
                                source_canonical_type=src_canon,
                                target_canonical_type=tgt_canon,
                                source_precision=src_prec,
                                target_precision=tgt_prec,
                                source_scale=src_scale,
                                target_scale=tgt_scale,
                                diff_type="TYPE_WIDENING",
                                severity="WARN",
                                message=f"Type widening from {src_canon} to {tgt_canon} is compatible.",
                            )
                        )
                    else:
                        type_mismatch_count += 1
                        all_passed = False
                        diffs.append(
                            ColumnSchemaDiff(
                                column_name=expected_tgt_name,
                                source_column_name=src_name,
                                target_column_name=expected_tgt_name,
                                source_raw_type=src_info.data_type,
                                target_raw_type=tgt_info.data_type,
                                source_canonical_type=src_canon,
                                target_canonical_type=tgt_canon,
                                source_precision=src_prec,
                                target_precision=tgt_prec,
                                source_scale=src_scale,
                                target_scale=tgt_scale,
                                diff_type="TYPE_MISMATCH",
                                severity="ERROR",
                                message=f"Type mismatch: {src_canon} ('{src_info.data_type}') cannot map to {tgt_canon} ('{tgt_info.data_type}').",
                            )
                        )

            # Check extra columns in target
            for tgt_name, tgt_info in tgt_cols.items():
                if tgt_name not in matched_tgt_cols:
                    extra_count += 1
                    sev = "WARN" if allow_extra_target_columns else "ERROR"
                    if sev == "ERROR":
                        all_passed = False
                    tgt_canon, tgt_prec, tgt_scale = self.resolve_canonical_type(target_db.name, tgt_info.data_type)
                    diffs.append(
                        ColumnSchemaDiff(
                            column_name=tgt_name,
                            target_column_name=tgt_name,
                            target_raw_type=tgt_info.data_type,
                            target_canonical_type=tgt_canon,
                            target_precision=tgt_prec,
                            target_scale=tgt_scale,
                            diff_type="EXTRA_IN_TARGET",
                            severity=sev,
                            message=f"Target table contains extra column '{tgt_name}'.",
                        )
                    )

            elapsed = round(time.time() - start_time, 3)
            return SchemaValidationResult(
                table_name=table_label,
                source_table=source_table,
                target_table=target_table,
                passed=all_passed,
                column_diffs=diffs,
                missing_columns_count=missing_count,
                extra_columns_count=extra_count,
                type_mismatch_count=type_mismatch_count,
                precision_loss_count=precision_loss_count,
                execution_time_seconds=elapsed,
            )

        except Exception as e:
            logger.error(f"Tier 2 schema validation failed for {table_label}: {e}", exc_info=True)
            elapsed = round(time.time() - start_time, 3)
            return SchemaValidationResult(
                table_name=table_label,
                source_table=source_table,
                target_table=target_table,
                passed=False,
                execution_time_seconds=elapsed,
                error=str(e),
            )
