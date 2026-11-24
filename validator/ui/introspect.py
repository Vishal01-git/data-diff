"""Dynamic schema, table, and Okta profile introspection services."""

import configparser
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple

from data_diff.databases.base import Database
from validator.config.connections import ConnectionResolver
from validator.config.manifest import ConnectionConfig
from validator.security import sanitize_error_message


def get_available_okta_profiles() -> List[str]:
    """Discover available AWS / Okta profiles from ~/.aws/credentials and ~/.aws/config."""
    profiles = set()
    aws_dir = Path.home() / ".aws"

    # Read ~/.aws/credentials
    creds_file = aws_dir / "credentials"
    if creds_file.exists():
        try:
            parser = configparser.ConfigParser()
            parser.read(str(creds_file))
            for section in parser.sections():
                profiles.add(section.strip())
        except Exception:
            pass

    # Read ~/.aws/config
    config_file = aws_dir / "config"
    if config_file.exists():
        try:
            parser = configparser.ConfigParser()
            parser.read(str(config_file))
            for section in parser.sections():
                # Profile sections in config are often "profile <name>"
                sec = section.strip()
                if sec.startswith("profile "):
                    sec = sec[len("profile ") :].strip()
                profiles.add(sec)
        except Exception:
            pass

    if not profiles:
        profiles.add("default")

    sorted_profiles = sorted(list(profiles))
    # Keep "default" first if present
    if "default" in sorted_profiles:
        sorted_profiles.remove("default")
        sorted_profiles.insert(0, "default")

    return sorted_profiles


def check_db_connectivity(cfg: ConnectionConfig) -> Tuple[bool, float, Optional[str]]:
    """Test connectivity and measure latency to a configured database."""
    resolver = ConnectionResolver({"temp_test": cfg})
    start_t = time.time()
    try:
        db = resolver.get_database("temp_test")
        # Lightweight query to verify live connection
        if hasattr(db, "query"):
            db.query("SELECT 1")
        elapsed_ms = round((time.time() - start_t) * 1000, 1)
        resolver.close_all()
        return True, elapsed_ms, None
    except Exception as e:
        elapsed_ms = round((time.time() - start_t) * 1000, 1)
        resolver.close_all()
        return False, elapsed_ms, sanitize_error_message(e)


def get_schemas(db: Database) -> List[str]:
    """Retrieve available schemas or databases for the connected engine."""
    dialect_name = getattr(db, "name", "").lower()
    try:
        if "athena" in dialect_name:
            rows = db.query("SHOW DATABASES")
            return [str(r[0]) for r in rows if r]
        elif "postgres" in dialect_name:
            rows = db.query(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'pg_catalog') "
                "ORDER BY schema_name"
            )
            return [str(r[0]) for r in rows if r]
        elif "mssql" in dialect_name or "sqlserver" in dialect_name:
            rows = db.query("SELECT name FROM sys.schemas ORDER BY name")
            return [str(r[0]) for r in rows if r]
        elif "duckdb" in dialect_name:
            rows = db.query(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'pg_catalog') "
                "ORDER BY schema_name"
            )
            return [str(r[0]) for r in rows if r]
    except Exception:
        pass

    # Default fallback
    default_schema = getattr(db, "default_schema", None)
    return [default_schema] if default_schema else ["public", "default"]


def get_tables(db: Database, schema: str) -> List[str]:
    """Retrieve available table names in the specified schema."""
    dialect_name = getattr(db, "name", "").lower()
    try:
        if "athena" in dialect_name:
            rows = db.query(f"SHOW TABLES IN {schema}")
            return [str(r[0]) for r in rows if r]
        elif "postgres" in dialect_name or "duckdb" in dialect_name:
            rows = db.query(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{schema}' "
                f"ORDER BY table_name"
            )
            return [str(r[0]) for r in rows if r]
        elif "mssql" in dialect_name or "sqlserver" in dialect_name:
            rows = db.query(
                f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_SCHEMA = '{schema}' "
                f"ORDER BY TABLE_NAME"
            )
            return [str(r[0]) for r in rows if r]
    except Exception:
        pass

    return []


def get_columns_and_types(db: Database, schema: str, table: str) -> Dict[str, str]:
    """Introspect table columns and data types."""
    try:
        col_info = db.query_table_schema((schema, table))
        return {name: info.data_type for name, info in col_info.items()}
    except Exception:
        return {}
