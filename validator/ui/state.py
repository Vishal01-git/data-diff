"""Session state initialization and persistence for Data Validator UI."""

from typing import Dict
import uuid
import streamlit as st

from validator.config.manifest import ConnectionConfig, TableConfig
from validator.ui.storage import (
    load_saved_connections,
    save_connections_to_disk,
)


def init_session_state() -> None:
    """Initialize persistent Streamlit session state keys with per-session isolation."""
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = uuid.uuid4().hex[:8]

    if "connections" not in st.session_state:
        # Auto-load saved connections from ~/.data-validator/connections.json
        st.session_state["connections"] = load_saved_connections()

    if "connection_status" not in st.session_state:
        st.session_state["connection_status"] = {}

    if "table_queue" not in st.session_state:
        st.session_state["table_queue"] = []

    if "current_result" not in st.session_state:
        st.session_state["current_result"] = None

    if "last_run_logs" not in st.session_state:
        st.session_state["last_run_logs"] = []

    if "selected_table_idx" not in st.session_state:
        st.session_state["selected_table_idx"] = 0

    if "active_manifest_path" not in st.session_state:
        st.session_state["active_manifest_path"] = None


def get_connections() -> Dict[str, ConnectionConfig]:
    """Retrieve all configured connections in the current session."""
    init_session_state()
    return st.session_state["connections"]


def save_connection(name: str, config: ConnectionConfig) -> None:
    """Save or update a named connection in session state and persist to local disk."""
    init_session_state()
    st.session_state["connections"][name] = config
    save_connections_to_disk(st.session_state["connections"])


def delete_connection(name: str) -> None:
    """Delete a named connection from session state and local disk."""
    init_session_state()
    if name in st.session_state["connections"]:
        del st.session_state["connections"][name]
        save_connections_to_disk(st.session_state["connections"])
    if name in st.session_state["connection_status"]:
        del st.session_state["connection_status"][name]


def add_table_to_queue(table_config: TableConfig) -> None:
    """Add a table reconciliation task to the active batch queue."""
    init_session_state()
    existing = [i for i, t in enumerate(st.session_state["table_queue"]) if t.name == table_config.name]
    if existing:
        st.session_state["table_queue"][existing[0]] = table_config
    else:
        st.session_state["table_queue"].append(table_config)


def remove_table_from_queue(idx: int) -> None:
    """Remove a table from the batch queue by index."""
    init_session_state()
    if 0 <= idx < len(st.session_state["table_queue"]):
        st.session_state["table_queue"].pop(idx)


def clear_table_queue() -> None:
    """Clear all queued tables."""
    init_session_state()
    st.session_state["table_queue"] = []
