"""Unit tests for structured logging and metrics tracking."""

import json
import logging

from validator.observability import (
    JSONLogFormatter,
    ValidationMetricsTracker,
    setup_logging,
)


def test_json_log_formatter():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="validator.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Table %s validation completed",
        args=("orders_fact",),
        exc_info=None,
    )
    record.table_name = "orders_fact"
    record.duration_seconds = 1.25
    record.query_count = 6
    record.bytes_scanned = 1048576

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "validator.test"
    assert "orders_fact validation completed" in data["message"]
    assert data["table_name"] == "orders_fact"
    assert data["duration_seconds"] == 1.25
    assert data["query_count"] == 6
    assert data["bytes_scanned"] == 1048576
    assert "timestamp" in data


def test_setup_logging_console_and_json():
    setup_logging(log_format="console", log_level="DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1

    setup_logging(log_format="json", log_level="WARNING")
    assert root.level == logging.WARNING
    assert isinstance(root.handlers[0].formatter, JSONLogFormatter)


def test_metrics_tracker():
    tracker = ValidationMetricsTracker()
    tracker.record_query(bytes_scanned=5000)
    tracker.record_query(bytes_scanned=2500)

    tracker.record_table_metrics(
        table_name="orders",
        duration_seconds=2.5,
        query_count=4,
        bytes_scanned=10000,
    )

    summary = tracker.get_summary()
    assert summary["total_queries"] == 6  # 2 direct + 4 table
    assert summary["total_bytes_scanned"] == 17500  # 7500 + 10000
    assert "orders" in summary["table_metrics"]
    assert summary["table_metrics"]["orders"]["duration_seconds"] == 2.5
