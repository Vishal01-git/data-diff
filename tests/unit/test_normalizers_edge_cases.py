"""Unit tests for edge cases in normalizers and connection resolution."""

from datetime import datetime, timezone
import decimal
import math
from unittest.mock import MagicMock, patch

from validator.config.connections import ConnectionResolver
from validator.config.manifest import ConnectionConfig
from validator.engine.normalizers import (
    compare_floats,
    normalize_decimal_val,
    normalize_string_val,
    normalize_timestamp_val,
    sql_normalize_decimal,
    sql_normalize_timestamp,
)


def test_normalize_decimal_val():
    assert normalize_decimal_val(None) is None
    assert normalize_decimal_val("invalid_num") == "invalid_num"
    assert normalize_decimal_val("-0.00") == "0"
    assert normalize_decimal_val("123.45000", strip_trailing_zeros=True) == "123.45"
    assert normalize_decimal_val("123.456", scale=2) == "123.46"
    assert normalize_decimal_val("123", scale=2) == "123.00"
    assert normalize_decimal_val("123.4500", scale=None, strip_trailing_zeros=False) == "123.4500"
    assert normalize_decimal_val(decimal.Decimal("50.00")) == "50"


def test_normalize_timestamp_val():
    assert normalize_timestamp_val(None) is None

    # Datetime object
    dt = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc)
    assert normalize_timestamp_val(dt) == "2026-05-10 14:30:00"

    # Datetime with microsecond precision
    dt_micro = datetime(2026, 5, 10, 14, 30, 0, 123456, tzinfo=timezone.utc)
    assert normalize_timestamp_val(dt_micro, target_precision=3) == "2026-05-10 14:30:00.123"

    # String parsing
    assert normalize_timestamp_val("2026-05-10 14:30:00") == "2026-05-10 14:30:00"
    assert normalize_timestamp_val("2026-05-10T14:30:00Z") == "2026-05-10 14:30:00"
    assert normalize_timestamp_val("2026-05-10") == "2026-05-10 00:00:00"
    assert normalize_timestamp_val("unparseable_string") == "unparseable_string"


def test_normalize_string_val():
    assert normalize_string_val(None) is None
    assert normalize_string_val("  hello  ") == "hello"
    assert normalize_string_val("  hello  ", trim=False) == "  hello  "
    assert normalize_string_val("", empty_as_null=True) is None
    assert normalize_string_val("   ", trim=True, empty_as_null=True) is None
    assert normalize_string_val("", empty_as_null=False) == ""


def test_compare_floats():
    assert compare_floats(None, None) is True
    assert compare_floats(1.0, None) is False
    assert compare_floats(None, 1.0) is False
    assert compare_floats(float("nan"), float("nan")) is True
    assert compare_floats(1.00001, 1.00002, abs_tol=1e-4) is True
    assert compare_floats(1.01, 1.02, abs_tol=1e-4) is False
    assert compare_floats("not_a_number", 1.0) is False


def test_sql_normalize_timestamp():
    assert "date_format" in sql_normalize_timestamp("athena", "col", precision=0)
    assert "date_format" in sql_normalize_timestamp("athena", "col", precision=3)
    assert "to_char" in sql_normalize_timestamp("maxcompute", "col")
    assert "FORMAT" in sql_normalize_timestamp("mssql", "col", precision=0)
    assert "FORMAT" in sql_normalize_timestamp("mssql", "col", precision=3)
    assert "to_char" in sql_normalize_timestamp("postgresql", "col", precision=0)
    assert "to_char" in sql_normalize_timestamp("postgresql", "col", precision=6)
    assert "DATE_FORMAT" in sql_normalize_timestamp("mysql", "col")
    assert "strftime" in sql_normalize_timestamp("duckdb", "col")
    assert "cast(col as varchar)" == sql_normalize_timestamp("unknown_dialect", "col")


def test_sql_normalize_decimal():
    assert "decimal(38, 2)" in sql_normalize_decimal("athena", "col", scale=2)
    assert "string" in sql_normalize_decimal("maxcompute", "col", scale=2)
    assert "FORMAT" in sql_normalize_decimal("mssql", "col", scale=2)
    assert "varchar" in sql_normalize_decimal("postgresql", "col", scale=2)
    assert "cast(col as varchar)" == sql_normalize_decimal("unknown_dialect", "col", scale=2)


def test_connection_resolver_all_dialects():
    conns = {
        "duck": ConnectionConfig(type="duckdb", database=":memory:"),
        "ath": ConnectionConfig(
            type="athena",
            region="us-east-1",
            s3_staging_dir="s3://test/staging",
            user="key",
            password="secret",
        ),
        "odps": ConnectionConfig(
            type="maxcompute",
            endpoint="https://odps.aliyun.com",
            project="test_proj",
            access_id="id",
            access_key="key",
        ),
        "ms": ConnectionConfig(
            type="mssql",
            host="localhost",
            user="sa",
            password="pwd",
            database="master",
        ),
        "pg": ConnectionConfig(
            type="postgres",
            host="localhost",
            user="user",
            password="pwd",
            database="postgres",
        ),
    }

    resolver = ConnectionResolver(conns)

    # DuckDB connects live
    duck_db = resolver.get_database("duck")
    assert duck_db is not None

    # Athena mock
    with patch("data_diff.databases.athena.Athena") as mock_athena:
        mock_athena.return_value = MagicMock()
        ath_db = resolver.get_database("ath")
        assert ath_db is not None
        mock_athena.assert_called_once()

    # MaxCompute mock
    with patch("data_diff.databases.maxcompute.MaxCompute") as mock_mc:
        mock_mc.return_value = MagicMock()
        odps_db = resolver.get_database("odps")
        assert odps_db is not None
        mock_mc.assert_called_once()

    # MSSQL mock
    with patch("data_diff.databases.mssql.MsSQL") as mock_ms:
        mock_ms.return_value = MagicMock()
        ms_db = resolver.get_database("ms")
        assert ms_db is not None
        mock_ms.assert_called_once()

    # Postgres mock
    with patch("data_diff.databases.postgresql.PostgreSQL") as mock_pg:
        mock_pg.return_value = MagicMock()
        pg_db = resolver.get_database("pg")
        assert pg_db is not None
        mock_pg.assert_called_once()

    # Close all
    resolver.close_all()
    assert len(resolver._cache) == 0
