"""Unit tests for the Streamlit UI state management and introspection services."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from validator.config.manifest import ConnectionConfig, TableConfig, TableEndpointConfig
from validator.ui.introspect import (
    check_db_connectivity,
    get_available_okta_profiles,
    get_columns_and_types,
    get_schemas,
    get_tables,
)
from validator.ui.state import (
    add_table_to_queue,
    clear_table_queue,
    delete_connection,
    get_connections,
    init_session_state,
    remove_table_from_queue,
    save_connection,
)


@pytest.fixture(autouse=True)
def mock_streamlit_session():
    """Mock streamlit session_state dictionary."""
    state = {}
    with patch("streamlit.session_state", state):
        yield state


def test_ui_session_state_lifecycle():
    init_session_state()
    conns = get_connections()
    assert isinstance(conns, dict)

    # Save connection
    cfg = ConnectionConfig(type="duckdb", database=":memory:")
    save_connection("test_duck", cfg)
    assert "test_duck" in get_connections()

    # Add table to queue
    tbl = TableConfig(
        name="test_task",
        source=TableEndpointConfig(connection="test_duck", table="src_tbl"),
        target=TableEndpointConfig(connection="test_duck", table="tgt_tbl"),
        primary_keys=["id"],
    )
    add_table_to_queue(tbl)

    # Update existing table in queue
    tbl_updated = TableConfig(
        name="test_task",
        source=TableEndpointConfig(connection="test_duck", table="src_tbl_2"),
        target=TableEndpointConfig(connection="test_duck", table="tgt_tbl_2"),
        primary_keys=["id"],
    )
    add_table_to_queue(tbl_updated)

    # Remove and clear
    remove_table_from_queue(0)
    clear_table_queue()

    # Delete connection
    delete_connection("test_duck")
    assert "test_duck" not in get_connections()


def test_get_available_okta_profiles(tmp_path: Path):
    with patch("pathlib.Path.home", return_value=tmp_path):
        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir(parents=True)
        (aws_dir / "credentials").write_text("[default]\naws_access_key_id=1\n[okta-analytics]\naws_access_key_id=2")
        (aws_dir / "config").write_text("[profile okta-prod]\nregion=us-east-1")

        profiles = get_available_okta_profiles()
        assert "default" in profiles
        assert "okta-analytics" in profiles
        assert "okta-prod" in profiles
        assert profiles[0] == "default"


def test_check_db_connectivity_success_and_failure():
    # Success with DuckDB
    cfg_ok = ConnectionConfig(type="duckdb", database=":memory:")
    ok, latency, err = check_db_connectivity(cfg_ok)
    assert ok is True
    assert latency >= 0
    assert err is None

    # Failure with invalid host
    cfg_fail = ConnectionConfig(type="postgres", host="nonexistent.host.invalid", database="db", user="u", password="p")
    ok, latency, err = check_db_connectivity(cfg_fail)
    assert ok is False
    assert err is not None


def test_schema_and_table_introspection():
    mock_db = MagicMock()
    mock_db.name = "PostgreSQL"
    mock_db.query.return_value = [("public",), ("analytics",)]

    schemas = get_schemas(mock_db)
    assert "public" in schemas
    assert "analytics" in schemas

    mock_db.query.return_value = [("users",), ("orders",)]
    tables = get_tables(mock_db, "public")
    assert "users" in tables
    assert "orders" in tables

    mock_col = MagicMock()
    mock_col.data_type = "integer"
    mock_db.query_table_schema.return_value = {"id": mock_col}
    cols = get_columns_and_types(mock_db, "public", "users")
    assert cols["id"] == "integer"
