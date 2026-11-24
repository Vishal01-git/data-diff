"""Observability, structured logging, and query metrics tracking."""

from datetime import datetime, timezone
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, Optional

from validator.security import SecretMaskingFilter


class JSONLogFormatter(logging.Formatter):
    """Formats log records as structured JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include extra context if present
        if hasattr(record, "table_name"):
            log_entry["table_name"] = getattr(record, "table_name")
        if hasattr(record, "duration_seconds"):
            log_entry["duration_seconds"] = getattr(record, "duration_seconds")
        if hasattr(record, "query_count"):
            log_entry["query_count"] = getattr(record, "query_count")
        if hasattr(record, "bytes_scanned"):
            log_entry["bytes_scanned"] = getattr(record, "bytes_scanned")

        if record.exc_info and record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry, default=str)


def setup_logging(
    log_format: str = "console",
    log_level: str = "INFO",
) -> None:
    """Configure enterprise structured logging.

    Args:
        log_format: 'console' for human-readable color logs, 'json' for machine-readable JSON lines.
        log_level: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    """
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Clear existing handlers
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(numeric_level)

    # Attach security masking filter
    masking_filter = SecretMaskingFilter()
    handler.addFilter(masking_filter)

    if log_format.lower() == "json":
        handler.setFormatter(JSONLogFormatter())
    else:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        datefmt = "%Y-%m-%d %H:%M:%S"
        handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))

    root_logger.addHandler(handler)


class ValidationMetricsTracker:
    """Thread-safe collector for query metrics, timing, and cloud scan bytes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_queries: int = 0
        self.total_bytes_scanned: int = 0
        self.table_metrics: Dict[str, Dict[str, Any]] = {}

    def record_query(self, bytes_scanned: int = 0) -> None:
        with self._lock:
            self.total_queries += 1
            if bytes_scanned > 0:
                self.total_bytes_scanned += bytes_scanned

    def record_table_metrics(
        self,
        table_name: str,
        duration_seconds: float,
        query_count: int = 0,
        bytes_scanned: int = 0,
    ) -> None:
        with self._lock:
            self.table_metrics[table_name] = {
                "duration_seconds": duration_seconds,
                "query_count": query_count,
                "bytes_scanned": bytes_scanned,
            }
            self.total_queries += query_count
            self.total_bytes_scanned += bytes_scanned

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_queries": self.total_queries,
                "total_bytes_scanned": self.total_bytes_scanned,
                "table_metrics": dict(self.table_metrics),
            }


# Global metrics tracker instance
global_metrics = ValidationMetricsTracker()
