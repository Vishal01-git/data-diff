"""Cross-dialect value and SQL normalization rules."""

from datetime import datetime, timezone
import decimal
import math
import re
from typing import Any, Optional, Union


def normalize_decimal_val(
    val: Any,
    scale: Optional[int] = None,
    strip_trailing_zeros: bool = True,
) -> Optional[str]:
    """Canonicalize decimal and numeric values.

    Strips trailing zeros after decimal point or rounds/formats to fixed scale.
    """
    if val is None:
        return None

    if isinstance(val, (int, float, str, decimal.Decimal)):
        try:
            d = decimal.Decimal(str(val).strip())
        except decimal.InvalidOperation:
            return str(val)

        # Handle negative zero (-0, -0.00)
        if d.is_zero():
            d = decimal.Decimal(0)

        if scale is not None:
            # Quantize to specified scale
            fmt = f"0.{'0' * scale}" if scale > 0 else "0"
            d = d.quantize(decimal.Decimal(fmt), rounding=decimal.ROUND_HALF_UP)
            return str(d)

        if strip_trailing_zeros:
            s = str(d)
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s if s else "0"

        return str(d)

    return str(val)


def normalize_timestamp_val(
    val: Any,
    target_precision: int = 0,
    force_utc: bool = True,
) -> Optional[str]:
    """Canonicalize timestamp values to standard ISO YYYY-MM-DD HH:MM:SS[.ffffff].

    Defaults to seconds precision (target_precision=0) to enable seamless
    Athena timestamp ↔ MaxCompute DATETIME cross-dialect comparison.
    """
    if val is None:
        return None

    dt: Optional[datetime] = None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        val_clean = val.strip()
        # Common ISO and DB formats
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(val_clean, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            # Fallback to ISO format parser
            try:
                dt = datetime.fromisoformat(val_clean)
            except Exception:
                return val_clean

    if dt is None:
        return str(val)

    if force_utc:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        else:
            # Naive datetime assumed to be UTC in cross-database SIT
            dt = dt.replace(tzinfo=timezone.utc)

    if target_precision > 0:
        micro = dt.microsecond
        sec_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        frac_str = f"{micro:06d}"[:target_precision]
        return f"{sec_str}.{frac_str}"

    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_string_val(
    val: Optional[str],
    trim: bool = True,
    empty_as_null: bool = False,
) -> Optional[str]:
    """Canonicalize string values with optional trimming and empty-as-null equivalence."""
    if val is None:
        return None

    s = str(val)
    if trim:
        s = s.strip()

    if empty_as_null and s == "":
        return None

    return s


def compare_floats(
    a: Optional[Union[float, decimal.Decimal, int]],
    b: Optional[Union[float, decimal.Decimal, int]],
    abs_tol: float = 1e-4,
) -> bool:
    """Compare two floating-point or numeric values within an absolute tolerance."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False

    try:
        fa = float(a)
        fb = float(b)
        if math.isnan(fa) and math.isnan(fb):
            return True
        return math.isclose(fa, fb, abs_tol=abs_tol, rel_tol=0.0)
    except (ValueError, TypeError):
        return False


def sql_normalize_timestamp(dialect_name: str, col_expr: str, precision: int = 0) -> str:
    """Generate dialect-specific SQL expression to normalize a timestamp column to canonical format."""
    name = dialect_name.lower()
    if name in ("athena", "trino", "presto"):
        if precision > 0:
            return f"date_format(cast({col_expr} as timestamp({precision})), '%Y-%m-%d %H:%i:%S.%f')"
        return f"date_format(cast({col_expr} as timestamp), '%Y-%m-%d %H:%i:%S')"

    elif name in ("maxcompute", "odps"):
        return f"to_char({col_expr}, 'yyyy-mm-dd hh:mi:ss')"

    elif name in ("mssql", "sqlserver"):
        if precision > 0:
            return (
                f"FORMAT({col_expr}, 'yyyy-MM-dd HH:mm:ss') + '.' + "
                f"SUBSTRING(FORMAT({col_expr}, 'fffffff'), 1, {precision})"
            )
        return f"FORMAT({col_expr}, 'yyyy-MM-dd HH:mm:ss')"

    elif name in ("postgresql", "postgres"):
        if precision > 0:
            return f"to_char({col_expr} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')"
        return f"to_char({col_expr} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"

    elif name == "mysql":
        return f"DATE_FORMAT({col_expr}, '%Y-%m-%d %H:%i:%S')"

    elif name == "duckdb":
        return f"strftime({col_expr}, '%Y-%m-%d %H:%M:%S')"

    # Generic fallback
    return f"cast({col_expr} as varchar)"


def sql_normalize_decimal(dialect_name: str, col_expr: str, scale: int = 2) -> str:
    """Generate dialect-specific SQL expression to normalize decimal to fixed scale."""
    name = dialect_name.lower()
    if name in ("athena", "trino", "presto"):
        return f"cast(cast({col_expr} as decimal(38, {scale})) as varchar)"
    elif name in ("maxcompute", "odps"):
        return f"cast(cast({col_expr} as decimal(38, {scale})) as string)"
    elif name in ("mssql", "sqlserver"):
        return f"FORMAT({col_expr}, 'N{scale}')"
    elif name in ("postgresql", "postgres"):
        return f"cast({col_expr} as decimal(38, {scale}))::varchar"
    return f"cast({col_expr} as varchar)"
