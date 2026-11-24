"""Unit tests for the Typer-based data-validator CLI commands."""

from pathlib import Path
import duckdb
import pytest
from typer.testing import CliRunner
import yaml

from validator.cli import _split_uri_table, app

runner = CliRunner()


@pytest.fixture
def cli_test_env(tmp_path: Path):
    """Set up two DuckDB databases and a valid SIT manifest."""
    src_db_file = tmp_path / "src.duckdb"
    tgt_db_file = tmp_path / "tgt.duckdb"

    # Seed source database
    src_con = duckdb.connect(str(src_db_file))
    src_con.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            age INTEGER,
            dt VARCHAR
        );
        INSERT INTO users VALUES
            (1, 'Alice', 30, '2026-01-01'),
            (2, 'Bob', 25, '2026-01-01'),
            (3, 'Charlie', 35, '2026-01-02');
    """
    )
    src_con.close()

    # Seed target database (matching)
    tgt_con = duckdb.connect(str(tgt_db_file))
    tgt_con.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            age INTEGER,
            dt VARCHAR
        );
        INSERT INTO users VALUES
            (1, 'Alice', 30, '2026-01-01'),
            (2, 'Bob', 25, '2026-01-01'),
            (3, 'Charlie', 35, '2026-01-02');
    """
    )
    tgt_con.close()

    manifest_data = {
        "version": "1.0",
        "run_name": "cli_test_batch",
        "connections": {
            "source_duck": {
                "type": "duckdb",
                "database": str(src_db_file).replace("\\", "/"),
            },
            "target_duck": {
                "type": "duckdb",
                "database": str(tgt_db_file).replace("\\", "/"),
            },
        },
        "defaults": {
            "tiers": ["count", "schema", "data"],
            "reporting": {
                "output_dir": str(tmp_path / "reports").replace("\\", "/"),
                "formats": ["html", "json", "csv", "junit"],
            },
        },
        "tables": [
            {
                "name": "users_test",
                "source": {
                    "connection": "source_duck",
                    "table": "users",
                },
                "target": {
                    "connection": "target_duck",
                    "table": "users",
                },
                "primary_keys": ["id"],
            }
        ],
    }

    manifest_file = tmp_path / "manifest.yaml"
    with open(manifest_file, "w") as f:
        yaml.dump(manifest_data, f)

    return {
        "manifest_path": manifest_file,
        "src_db": src_db_file,
        "tgt_db": tgt_db_file,
        "reports_dir": tmp_path / "reports",
    }


def test_split_uri_table():
    uri, table = _split_uri_table("athena://region/catalog/db/my_table")
    assert table == "my_table"
    assert "athena://" in uri

    uri, table = _split_uri_table("postgresql://user:pass@localhost:5432/db/public/users?sslmode=require")
    assert table == "users"
    assert "?sslmode=require" in uri

    with pytest.raises(ValueError):
        _split_uri_table("invalid_no_slashes")


def test_cli_validate_config(cli_test_env):
    manifest = cli_test_env["manifest_path"]
    result = runner.invoke(app, ["validate-config", "--config", str(manifest)])
    assert result.exit_code == 0
    assert "Configuration manifest is valid!" in result.stdout
    assert "users_test" in result.stdout


def test_cli_validate_config_invalid(tmp_path: Path):
    bad_manifest = tmp_path / "bad.yaml"
    bad_manifest.write_text("invalid: yaml: [}")
    result = runner.invoke(app, ["validate-config", "--config", str(bad_manifest)])
    assert result.exit_code == 2


def test_cli_test_connection(cli_test_env):
    manifest = cli_test_env["manifest_path"]
    result = runner.invoke(app, ["test-connection", "--config", str(manifest)])
    assert result.exit_code == 0
    assert "CONNECTED" in result.stdout
    assert "source_duck" in result.stdout
    assert "target_duck" in result.stdout


def test_cli_test_connection_specific(cli_test_env):
    manifest = cli_test_env["manifest_path"]
    result = runner.invoke(app, ["test-connection", "--config", str(manifest), "--connection", "source_duck"])
    assert result.exit_code == 0
    assert "source_duck" in result.stdout

    # Non-existent connection
    res_bad = runner.invoke(app, ["test-connection", "--config", str(manifest), "--connection", "non_existent"])
    assert res_bad.exit_code == 2


def test_cli_run_and_validate(cli_test_env):
    manifest = cli_test_env["manifest_path"]
    # Test 'run'
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(manifest),
            "--log-format",
            "json",
            "--log-level",
            "INFO",
        ],
    )
    assert result.exit_code == 0
    assert "PASSED" in result.stdout

    # Verify generated report files
    rep_dir = cli_test_env["reports_dir"]
    assert (rep_dir / "sit_report.html").exists()
    assert (rep_dir / "summary.json").exists()
    assert (rep_dir / "mismatches.csv").exists()
    assert (rep_dir / "junit.xml").exists()

    # Test 'validate' command with --resume and --include-sample-values
    res2 = runner.invoke(
        app,
        [
            "validate",
            "--manifest",
            str(manifest),
            "--resume",
            "--include-sample-values",
        ],
    )
    assert res2.exit_code == 0


def test_cli_diff_check_schema_and_counts(cli_test_env, monkeypatch):
    """Test ad-hoc CLI commands diff, check-schema, and check-counts."""
    from data_diff.databases.duckdb import DuckDB

    src_db = DuckDB(filepath=str(cli_test_env["src_db"]))
    tgt_db = DuckDB(filepath=str(cli_test_env["tgt_db"]))

    # Mock connect in validator.cli so it returns the DuckDB instance directly
    monkeypatch.setattr("validator.cli.connect", lambda uri: src_db if "src" in uri else tgt_db)

    # Test diff
    diff_res = runner.invoke(
        app,
        [
            "diff",
            "--source-uri",
            "mock://src/users",
            "--target-uri",
            "mock://tgt/users",
            "--primary-key",
            "id",
            "--mode",
            "bucket_hash",
        ],
    )
    assert diff_res.exit_code == 0
    assert "MATCH" in diff_res.stdout

    # Test check-schema
    schema_res = runner.invoke(
        app,
        [
            "check-schema",
            "--source-uri",
            "mock://src/users",
            "--target-uri",
            "mock://tgt/users",
            "--allow-extra-columns",
        ],
    )
    assert schema_res.exit_code == 0
    assert "Schema Diff" in schema_res.stdout

    # Test check-counts
    counts_res = runner.invoke(
        app,
        [
            "check-counts",
            "--source-uri",
            "mock://src/users",
            "--target-uri",
            "mock://tgt/users",
            "--partition-col",
            "dt",
        ],
    )
    assert counts_res.exit_code == 0
    assert "PASSED" in counts_res.stdout
    assert "Source: 3" in counts_res.stdout
