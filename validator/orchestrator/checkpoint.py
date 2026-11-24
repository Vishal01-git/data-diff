"""SQLite-backed checkpoint tracker for resumable SIT batch validation runs."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger("validator.checkpoint")


class CheckpointTracker:
    """Manages persistent SQLite run state to support resuming interrupted batch runs."""

    def __init__(self, run_name: str, state_dir: Union[str, Path] = ".validator_state") -> None:
        self.run_name = run_name
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / f"{run_name}.db"
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (run_name, table_name)
                );
                """
            )
            conn.commit()

    def save_checkpoint(
        self,
        table_name: str,
        status: str,
        passed: bool,
        result_data: Dict[str, Any],
    ) -> None:
        """Persist a completed table validation result."""
        now_iso = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(result_data, default=str)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                    (run_name, table_name, status, passed, completed_at, result_json)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (self.run_name, table_name, status, 1 if passed else 0, now_iso, serialized),
            )
            conn.commit()
        logger.debug(f"Saved checkpoint for table '{table_name}' in run '{self.run_name}'.")

    def is_completed_and_passed(self, table_name: str) -> bool:
        """Check whether a table previously ran and passed in this run."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT passed FROM checkpoints
                WHERE run_name = ? AND table_name = ?;
                """,
                (self.run_name, table_name),
            )
            row = cursor.fetchone()
            if row is not None:
                return bool(row["passed"])
        return False

    def get_table_result(self, table_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored result for a table."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT result_json FROM checkpoints
                WHERE run_name = ? AND table_name = ?;
                """,
                (self.run_name, table_name),
            )
            row = cursor.fetchone()
            if row is not None:
                return json.loads(row["result_json"])
        return None

    def get_all_completed(self) -> Dict[str, Tuple[str, bool, Dict[str, Any]]]:
        """Return all checkpoints for this run as {table_name: (status, passed, result_data)}."""
        result = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT table_name, status, passed, result_json FROM checkpoints
                WHERE run_name = ?;
                """,
                (self.run_name,),
            )
            for row in cursor.fetchall():
                result[row["table_name"]] = (
                    row["status"],
                    bool(row["passed"]),
                    json.loads(row["result_json"]),
                )
        return result

    def clear(self) -> None:
        """Remove all checkpoint records for this run."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM checkpoints WHERE run_name = ?;", (self.run_name,))
            conn.commit()
