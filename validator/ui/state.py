"""Session state initialization and persistence for Data Validator UI."""

from typing import Any, Dict, List, Optional
import streamlit as st

from validator.config.manifest import ConnectionConfig, TableConfig


def init_session_state() -> None:
    """Initialize persistent Streamlit session state keys with default values."""
    if "connections" not in st.session_state:
        st.session_state["connections"] = {}

    if "connection_status" not in st.session_state:
        st.session_state["connection_status"] = {}

    if "table_queue" not in st.session_state:
        st.session_state["table_queue"] = []

    if "current_result" not in st.session_state:
        st.session_state["current_result"] = None

    if "selected_table_idx" not in st.session_state:
        st.session_state["selected_table_idx"] = 0

    if "active_manifest_path" not in st.session_state:
        st.session_state["active_manifest_path"] = None


def get_connections() -> Dict[str, ConnectionConfig]:
    """Retrieve all configured connections in the current session."""
    init_session_state()
    return st.session_state["connections"]


def save_connection(name: str, config: ConnectionConfig) -> None:
    """Save or update a named connection in session state."""
    init_session_state()
    st.session_state["connections"][name] = config


def delete_connection(name: str) -> None:
    """Delete a named connection from session state."""
    init_session_state()
    if name in st.session_state["connections"]:
        del st.session_state["connections"][name]
    if name in st.session_state["connection_status"]:
        del st.session_state["connection_status"][name]


def add_table_to_queue(table_config: TableConfig) -> None:
    """Add a table reconciliation task to the active batch queue."""
    init_session_state()
    # Replace if table name exists, else append
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
