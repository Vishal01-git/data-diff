"""Connection resolver and recursive environment variable interpolation."""

import os
from pathlib import Path
import re
from typing import Any, Dict, Optional, Union

import yaml

from data_diff import connect
from data_diff.databases.base import Database
from validator.config.manifest import ConnectionConfig, SITManifest

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}|\$([A-Za-z0-9_]+)")


def interpolate_env_vars(obj: Any) -> Any:
    """Recursively interpolate ${ENV_VAR} or ${ENV_VAR:-default} into strings.

    Fails fast if a required environment variable is not set.
    """
    if isinstance(obj, str):

        def replace_match(match: re.Match) -> str:
            var_name = match.group(1) or match.group(3)
            default_val = match.group(2)

            val = os.environ.get(var_name)
            if val is not None:
                return val
            if default_val is not None:
                return default_val

            raise ValueError(f"Required environment variable '{var_name}' is not set.")

        return ENV_VAR_PATTERN.sub(replace_match, obj)

    elif isinstance(obj, dict):
        return {k: interpolate_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [interpolate_env_vars(v) for v in obj]
    return obj


def load_manifest(manifest_path: Union[str, Path]) -> SITManifest:
    """Load and validate an SIT manifest YAML file with environment variable interpolation."""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    if not isinstance(raw_data, dict):
        raise ValueError(f"Invalid YAML structure in {path}: expected a dictionary root.")

    interpolated_data = interpolate_env_vars(raw_data)
    return SITManifest.model_validate(interpolated_data)


class ConnectionResolver:
    """Resolves ConnectionConfig into cached Database instances."""

    def __init__(self, connections: Dict[str, ConnectionConfig]) -> None:
        self.configs = connections
        self._cache: Dict[str, Database] = {}

    def get_database(self, connection_name: str) -> Database:
        """Get or initialize a Database instance for the named connection."""
        if connection_name in self._cache:
            return self._cache[connection_name]

        if connection_name not in self.configs:
            raise KeyError(f"Unknown connection: '{connection_name}'")

        cfg = self.configs[connection_name]
        db = self._instantiate_database(cfg)
        self._cache[connection_name] = db
        return db

    def _instantiate_database(self, cfg: ConnectionConfig) -> Database:
        # If direct URI provided, use it
        if cfg.uri:
            return connect(cfg.uri)

        conn_type = cfg.type.lower()

        if conn_type == "athena":
            from data_diff.databases.athena import Athena

            return Athena(
                region_name=cfg.region,
                s3_staging_dir=cfg.s3_staging_dir,
                work_group=cfg.workgroup or "primary",
                catalog=cfg.catalog or "AwsDataCatalog",
                aws_access_key_id=cfg.user,
                aws_secret_access_key=cfg.password,
                role_arn=cfg.role_arn,
                profile_name=cfg.profile_name,
            )

        elif conn_type in ("maxcompute", "odps"):
            from data_diff.databases.maxcompute import MaxCompute

            return MaxCompute(
                endpoint=cfg.endpoint,
                project=cfg.project,
                access_id=cfg.access_id or cfg.user,
                access_key=cfg.access_key or cfg.password,
                allow_fullscan=cfg.allow_fullscan,
            )

        elif conn_type in ("duckdb", "sqlite"):
            from data_diff.databases.duckdb import DuckDB

            fp = getattr(cfg, "filepath", None) or cfg.database or ":memory:"
            return DuckDB(filepath=str(fp))

        elif conn_type in ("mssql", "sqlserver"):
            from data_diff.databases.mssql import MsSQL

            return MsSQL(
                host=cfg.host,
                port=cfg.port or 1433,
                user=cfg.user,
                password=cfg.password,
                database=cfg.database,
                schema=cfg.schema_name or "dbo",
                thread_count=cfg.max_concurrency,
            )

        elif conn_type in ("postgres", "postgresql"):
            from data_diff.databases.postgresql import PostgreSQL

            return PostgreSQL(
                host=cfg.host,
                port=cfg.port or 5432,
                user=cfg.user,
                password=cfg.password,
                database=cfg.database,
                schema=cfg.schema_name or "public",
                thread_count=cfg.max_concurrency,
            )

        else:
            # Fallback to standard connect
            conn_dict = {
                "driver": conn_type,
                "host": cfg.host,
                "port": cfg.port,
                "user": cfg.user,
                "password": cfg.password,
                "database": cfg.database,
                "schema": cfg.schema_name,
            }
            filtered = {k: v for k, v in conn_dict.items() if v is not None}
            return connect(filtered)

    def close_all(self) -> None:
        """Close all cached database connections."""
        for name, db in self._cache.items():
            try:
                db.close()
            except Exception:
                pass
        self._cache.clear()
