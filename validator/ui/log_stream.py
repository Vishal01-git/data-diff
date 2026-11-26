"""Real-time thread-safe log streaming and background batch execution for Streamlit UI."""

from datetime import datetime
import logging
import queue
import threading
import time
from typing import Any, Dict, List

import streamlit as st

from validator.orchestrator.runner import BatchRunner, BatchValidationResult
from validator.security import SecretMaskingFilter, mask_secrets_in_text


class UILogHandler(logging.Handler):
    """Thread-safe logging handler that pushes masked log lines into a queue for UI streaming."""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue
        self.addFilter(SecretMaskingFilter())
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = mask_secrets_in_text(self.format(record))
            self.log_queue.put_nowait(msg)
        except Exception:
            pass


def run_batch_with_live_logs(
    runner: BatchRunner,
    total_tables: int,
    progress_bar: Any,
    status_placeholder: Any,
    log_placeholder: Any,
) -> BatchValidationResult:
    """Execute BatchRunner in a background thread while streaming live backend logs and progress to Streamlit."""
    log_queue: "queue.Queue[str]" = queue.Queue()
    handler = UILogHandler(log_queue)
    handler.setLevel(logging.INFO)

    target_loggers = [
        logging.getLogger("validator"),
        logging.getLogger("data_diff"),
        logging.getLogger("hashdiff_tables"),
        logging.getLogger("joindiff_tables"),
    ]
    prev_levels: Dict[str, int] = {}
    for lgr in target_loggers:
        prev_levels[lgr.name] = lgr.level
        if lgr.level == logging.NOTSET or lgr.level > logging.INFO:
            lgr.setLevel(logging.INFO)
        lgr.addHandler(handler)

    table_states: Dict[str, str] = {}
    state_lock = threading.Lock()
    result_holder: Dict[str, Any] = {"result": None, "error": None}

    def on_progress(tbl_name: str, st_val: str) -> None:
        with state_lock:
            table_states[tbl_name] = st_val

    runner.progress_callback = on_progress

    def worker() -> None:
        try:
            result_holder["result"] = runner.run()
        except Exception as exc:
            result_holder["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    logs: List[str] = [f"[{datetime.now().strftime('%H:%M:%S')}] Starting batch validation across {total_tables} table(s)..."]

    try:
        while t.is_alive():
            while not log_queue.empty():
                try:
                    logs.append(log_queue.get_nowait())
                except queue.Empty:
                    break

            with state_lock:
                done_count = sum(1 for v in table_states.values() if v != "RUNNING")
                running_tbls = [k for k, v in table_states.items() if v == "RUNNING"]

            pct = min(1.0, done_count / max(total_tables, 1))
            progress_bar.progress(pct, text=f"Completed {done_count} / {total_tables} tables")
            if running_tbls:
                status_placeholder.info(f"⏳ Currently validating: `{'`, `'.join(running_tbls[:4])}`")
            log_placeholder.code("\n".join(logs[-50:]), language="log")
            time.sleep(0.2)

        t.join()
        while not log_queue.empty():
            try:
                logs.append(log_queue.get_nowait())
            except queue.Empty:
                break

        progress_bar.progress(1.0, text=f"Completed {total_tables} / {total_tables} tables")
        log_placeholder.code("\n".join(logs[-50:]), language="log")
        st.session_state["last_run_logs"] = logs

        if result_holder["error"] is not None:
            raise result_holder["error"]

        return result_holder["result"]
    finally:
        for lgr in target_loggers:
            lgr.removeHandler(handler)
            lgr.setLevel(prev_levels[lgr.name])
