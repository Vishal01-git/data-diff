"""Pydantic v2 models for declarative SIT YAML manifests."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class ConnectionConfig(BaseModel):
    """Configuration for a database connection."""

    type: str
    max_concurrency: int = 5

    # Cloud Athena / Glue parameters
    region: Optional[str] = None
    s3_staging_dir: Optional[str] = None
    workgroup: Optional[str] = "primary"
    catalog: Optional[str] = "AwsDataCatalog"
    role_arn: Optional[str] = None
    profile_name: Optional[str] = None
    session_token: Optional[str] = None
    aws_session_token: Optional[str] = None

    # Cloud MaxCompute (ODPS) parameters
    endpoint: Optional[str] = None
    project: Optional[str] = None
    access_id: Optional[str] = None
    access_key: Optional[str] = None
    allow_fullscan: bool = False

    # Standard RDBMS parameters (Postgres, MSSQL, MySQL, Oracle)
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")

    # Direct URI override
    uri: Optional[str] = None

    @property
    def dialect(self) -> str:
        return self.type

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class FailFastConfig(BaseModel):
    """Fail-fast execution thresholds."""

    skip_data_on_schema_fail: bool = True
    skip_data_if_count_diff_pct_gt: float = 50.0


class DataDiffConfig(BaseModel):
    """Tier 3 row validation settings."""

    mode: str = "auto"  # auto | hashdiff | joindiff | bucket_hash | keyless
    bucket_count: int = 64
    float_abs_tol: float = 0.0001
    normalize_timestamps_utc: bool = True
    treat_empty_string_as_null: bool = False
    max_mismatch_samples: int = 100


class ReportingConfig(BaseModel):
    """Report generation and output formats."""

    output_dir: str = "./reports"
    formats: List[str] = Field(default_factory=lambda: ["html", "json", "csv", "junit"])
    include_sample_values: bool = False


class DefaultsConfig(BaseModel):
    """Global defaults inherited by table validations."""

    tiers: List[str] = Field(default_factory=lambda: ["count", "schema", "data"])
    fail_fast: FailFastConfig = Field(default_factory=FailFastConfig)
    data_diff: DataDiffConfig = Field(default_factory=DataDiffConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)


class TableEndpointConfig(BaseModel):
    """Table reference and filter on a specific connection."""

    connection: str
    table: str
    where: Optional[str] = None


class TableConfig(BaseModel):
    """Per-table SIT validation specification."""

    name: str
    source: TableEndpointConfig
    target: TableEndpointConfig
    primary_keys: List[str] = Field(default_factory=list)
    partition_column: Optional[str] = None
    exclude_columns: List[str] = Field(default_factory=list)
    column_mapping: Dict[str, str] = Field(default_factory=dict)
    threshold_pct: float = 0.0
    tiers: Optional[List[str]] = None
    data_diff: Optional[DataDiffConfig] = None
    allow_extra_target_columns: bool = False


class SITManifest(BaseModel):
    """Top-level batch validation manifest."""

    version: str = "1.0"
    run_name: str
    connections: Dict[str, ConnectionConfig]
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    tables: List[TableConfig]

    @model_validator(mode="after")
    def validate_connection_references(self) -> "SITManifest":
        conn_names = set(self.connections.keys())
        for tbl in self.tables:
            if tbl.source.connection not in conn_names:
                raise ValueError(
                    f"Table '{tbl.name}' references undefined source connection: '{tbl.source.connection}'"
                )
            if tbl.target.connection not in conn_names:
                raise ValueError(
                    f"Table '{tbl.name}' references undefined target connection: '{tbl.target.connection}'"
                )
        return self
