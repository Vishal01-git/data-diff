# MASTER_PLAN.md — Enterprise Cross-Database Data Validation Tool

## 1. Project Overview
Build a production-ready, enterprise-grade data validation CLI tool by forking and extending [`datafold/data-diff`](https://github.com/datafold/data-diff) (MIT License).

### Core Objectives
1. **Primary Use Case:** Post-migration SIT (System Integration Testing) validation from **AWS Glue / Athena** to **Alibaba Cloud MaxCompute (ODPS)**.
2. **Multi-Source Extensibility:** Support cross-database and same-database validation across:
   - AWS Athena (Glue Catalog) ↔ Alibaba MaxCompute (ODPS)
   - AWS Athena ↔ AWS Athena (e.g., cross-account / cross-environment)
   - AWS Athena ↔ SQL Server (MSSQL)
   - AWS Athena ↔ PostgreSQL
   - PostgreSQL ↔ MSSQL / MaxCompute (any supported source-to-target pair)
3. **3-Tier Validation Pipeline:**
   - **Tier 1:** Count & Volume Validation (total counts, partition-level counts, optional null/min/max/sum checks).
   - **Tier 2:** Cross-Dialect Schema Validation (canonical type mapping, column order, nullability, precision/scale drift).
   - **Tier 3:** Row-Level Data Validation (`hashdiff`, `joindiff`, and a cost-optimized Single-Scan Bucket Checksum mode for pay-per-scan cloud engines).
4. **Enterprise Readiness:** Declarative YAML batch manifests, per-source concurrency limits, resumable checkpoints, and audit-ready SIT reports (HTML, JSON, CSV, JUnit XML).

---

## 2. Agent Execution Rules (Antigravity)
- **Work strictly one phase at a time.** Do not start Phase $N+1$ until Phase $N$'s Verification Gate passes 100%.
- **Preserve core algorithms.** Leverage `data_diff`'s existing `sqeleton`, `hashdiff`, and `joindiff` engines; wrap and extend them rather than rewriting from scratch.
- **No hardcoded credentials.** All secrets must resolve via environment variables (`${ENV_VAR}`) or IAM roles.
- **Keep dependencies modular.** Database drivers must be optional extras in `pyproject.toml`.
- **Write tests alongside code.** Every new dialect, type mapper, and validator must have unit tests before proceeding.

---

## 3. Target Repository Structure
```text
data-validator/
├── docs/
│   └── MASTER_PLAN.md
├── configs/
│   ├── sit_manifest.example.yaml     # Sample batch SIT configuration
│   └── type_mappings.yaml            # Default cross-dialect canonical type matrix
├── data_diff/                        # Forked core engine (sqeleton + hashdiff + joindiff)
│   └── databases/
│       ├── athena.py                 # NEW: AWS Athena (Trino/Presto-based) dialect
│       ├── maxcompute.py             # NEW: Alibaba MaxCompute (ODPS) dialect
│       ├── mssql.py                  # NEW/HARDENED: SQL Server dialect
│       ├── postgresql.py             # Existing (hardened)
│       └── __init__.py               # Updated URI registry
├── validator/                        # NEW: Enterprise Validation Layer
│   ├── __init__.py
│   ├── cli.py                        # Unified CLI (Typer + Rich)
│   ├── config/
│   │   ├── manifest.py               # Pydantic v2 models for YAML manifests
│   │   └── connections.py            # Connection resolver & env-var interpolation
│   ├── engine/
│   │   ├── count_validator.py        # Tier 1: Total, partition, and column profile counts
│   │   ├── schema_validator.py       # Tier 2: Canonical schema & type-map comparator
│   │   ├── data_validator.py         # Tier 3: Wrapper over hashdiff/joindiff + bucket-hash
│   │   └── normalizers.py            # Cross-dialect SQL/value normalization rules
│   ├── orchestrator/
│   │   ├── runner.py                 # Concurrent batch runner with semaphores & retries
│   │   └── checkpoint.py             # SQLite/JSON state tracker for resumable SIT runs
│   └── reporting/
│       ├── templates/
│       │   └── sit_report.html.j2    # Self-contained Jinja2 HTML SIT report
│       ├── html_report.py            # HTML report generator
│       └── exporters.py              # JSON, CSV mismatch samples, JUnit XML
├── tests/
│   ├── unit/
│   │   ├── test_athena_dialect.py
│   │   ├── test_maxcompute_dialect.py
│   │   ├── test_mssql_dialect.py
│   │   ├── test_schema_validator.py
│   │   ├── test_count_validator.py
│   │   └── test_bucket_hash.py
│   └── integration/
│       └── test_e2e_validation.py    # Dockerized Postgres/MSSQL + mocked cloud engines
├── Dockerfile
└── pyproject.toml
```

---

## 4. Declarative Manifest Specification (`configs/sit_manifest.example.yaml`)
The tool must accept a YAML manifest structured as follows:

```yaml
version: "1.0"
run_name: "glue_to_maxcompute_sit_batch_01"

connections:
  aws_glue_prod:
    type: athena
    region: "${AWS_REGION}"
    s3_staging_dir: "${ATHENA_S3_STAGING}"
    workgroup: "primary"
    catalog: "AwsDataCatalog"
    max_concurrency: 5

  ali_mc_sit:
    type: maxcompute
    endpoint: "${ODPS_ENDPOINT}"
    project: "${ODPS_PROJECT}"
    access_id: "${ODPS_ACCESS_ID}"
    access_key: "${ODPS_ACCESS_KEY}"
    allow_fullscan: false
    max_concurrency: 5

defaults:
  tiers: ["count", "schema", "data"]
  fail_fast:
    skip_data_on_schema_fail: true
    skip_data_if_count_diff_pct_gt: 50.0
  data_diff:
    mode: "auto"                      # auto | hashdiff | joindiff | bucket_hash
    bucket_count: 64                  # Used when mode == bucket_hash
    float_abs_tol: 0.0001
    normalize_timestamps_utc: true
    treat_empty_string_as_null: false
    max_mismatch_samples: 100
  reporting:
    output_dir: "./reports"
    formats: ["html", "json", "csv", "junit"]
    include_sample_values: false      # PII safety default; override via CLI flag

tables:
  - name: "orders_fact"
    source:
      connection: aws_glue_prod
      table: "sales_db.orders"
      where: "dt >= '2026-10-01'"
    target:
      connection: ali_mc_sit
      table: "sales_sit.orders"
      where: "dt >= '2026-10-01'"
    primary_keys: ["order_id"]
    partition_column: "dt"
    exclude_columns: ["etl_loaded_at"]
    column_mapping:
      order_amt: "order_amount"       # Optional explicit rename mapping
```

---

## 5. Phased Implementation Plan

### Phase 1: Fork, Modernize & Baseline Harness
**Goal:** Establish a clean, modern Python 3.11+ codebase from `datafold/data-diff` with passing baseline tests.

**Tasks:**
1. Audit `pyproject.toml` and remove deprecated Datafold Cloud upload logic, dbt-cloud hooks, and telemetry/analytics modules.
2. Upgrade packaging to modern `pyproject.toml` (PEP 621) compatible with `uv` and `pip`, targeting Python `>=3.11`.
3. Define optional dependency groups:
   - `athena`: `pyathena`, `boto3`
   - `maxcompute`: `pyodps`
   - `mssql`: `pyodbc`
   - `postgres`: `psycopg2-binary`
   - `dev`: `pytest`, `pytest-cov`, `ruff`, `mypy`
4. Add core runtime dependencies for the new `validator/` layer: `pydantic>=2.0`, `pyyaml`, `typer`, `rich`, `jinja2`.
5. Fix any Python 3.11+ deprecation or dependency compatibility issues in `data_diff` / `sqeleton`.

**Verification Gate (Phase 1):**
- `pytest` runs cleanly on existing `data_diff` unit tests with 0 failures.
- `ruff check .` passes with no fatal errors.

---

### Phase 2: Production Database Dialects
**Goal:** Implement and register production-grade dialects for AWS Athena, Alibaba MaxCompute, and SQL Server.

**Tasks:**
1. **AWS Athena Dialect (`data_diff/databases/athena.py`):**
   - Subclass/adapt the existing `Trino`/`Presto` dialect (Athena Engine v3 is Trino-based).
   - Connect via `pyathena.connect()` supporting `s3_staging_dir`, `region_name`, `work_group`, `catalog_name`, and IAM role/profile credentials.
   - Implement `query_table_schema()` using `information_schema.columns` with an optional `boto3` Glue `get_table` fast-path to avoid Athena query scan charges for metadata.
   - Implement `md5_as_int(s)`:
     ```sql
     from_base(substr(to_hex(md5(to_utf8(CAST(s AS VARCHAR)))), 1, 15), 16)
     ```
   - Capture Athena query execution stats (`data_scanned_in_bytes`) from `pyathena` cursor where available.

2. **Alibaba MaxCompute Dialect (`data_diff/databases/maxcompute.py`):**
   - Implement using `pyodps` (`odps.ODPS` / `odps.dbapi`).
   - Handle ODPS SQL syntax:
     - Identifier quoting with backticks (`` `col` ``).
     - `query_table_schema()` using `o.get_table(table_name).table_schema` (fast metadata API, zero SQL scan cost).
     - Configurable `odps.sql.allow.fullscan` hint per connection.
     - Implement `md5_as_int(s)` using ODPS `MD5()` + `SUBSTR()` + `CONV(..., 16, 10)` (or `CAST(CONV(SUBSTR(MD5(CAST(s AS STRING)), 1, 15), 16, 10) AS DECIMAL(38, 0))`).
     - Normalize `DATETIME` and `TIMESTAMP` formatting to canonical `YYYY-MM-DD HH:MM:SS` in UTC.

3. **SQL Server Dialect (`data_diff/databases/mssql.py`):**
   - Implement via `pyodbc` with proper `[bracket]` identifier quoting.
   - Implement `md5_as_int(s)` using `HASHBYTES('MD5', CAST(s AS VARCHAR(MAX)))` converted to hex substring and bigint.
   - Support `information_schema.columns` for schema introspection.

4. **Dialect Registry (`data_diff/databases/__init__.py`):**
   - Register URI schemes: `athena://`, `odps://` (alias `maxcompute://`), `mssql://`, alongside existing `postgresql://`.

**Verification Gate (Phase 2):**
- Unit tests in `tests/unit/test_*_dialect.py` verifying:
  - URI parsing and connection config mapping.
  - Exact SQL generation for quoting, `CAST`, `md5_as_int`, timestamp formatting, and decimal formatting across all 4 dialects.
  - Mocked schema introspection returning standardized column metadata objects.

---

### Phase 3: 3-Tier Validation Engine
**Goal:** Build the core Count, Schema, and Data validation modules with cross-dialect normalization.

**Tasks:**
1. **Tier 1 — Count & Volume Validator (`validator/engine/count_validator.py`):**
   - Execute `SELECT COUNT(*) FROM <table> WHERE <filter>` on both source and target.
   - If `partition_column` is specified, execute `SELECT <partition_col>, COUNT(*) ... GROUP BY <partition_col>` on both sides and produce a partition-by-partition diff (missing partitions, extra partitions, row-count delta per partition).
   - Optional quick profile mode: compare `COUNT(col)` (null count) and `MIN/MAX/SUM` for specified numeric columns.

2. **Tier 2 — Cross-Dialect Schema Validator (`validator/engine/schema_validator.py`):**
   - Load default canonical type mapping from `configs/type_mappings.yaml` (overridable per manifest).
   - Map dialect-specific types to canonical logical types:
     - `STRING`: Athena `varchar`/`string`/`char`, MaxCompute `STRING`/`VARCHAR`/`CHAR`, Postgres `text`/`varchar`, MSSQL `varchar`/`nvarchar`
     - `INT64`: `bigint`, `int8`
     - `INT32`: `int`, `integer`, `int4`
     - `DECIMAL`: `decimal(p,s)`, `numeric(p,s)` (compare precision & scale; flag truncation as ERROR, widening as WARN)
     - `TIMESTAMP`: Athena `timestamp`, MaxCompute `DATETIME`/`TIMESTAMP`, Postgres `timestamptz`/`timestamp`, MSSQL `datetime2`
     - `BOOLEAN`: `boolean`, `bool`, `bit`
   - Apply `exclude_columns` and `column_mapping` (renames) before comparison.
   - Output structured diffs: missing columns, extra columns, type mismatches, precision/scale drift, nullability differences.

3. **Tier 3 — Data Diff Engine (`validator/engine/data_validator.py`):**
   - **Routing Logic (`mode: auto`):**
     - Same connection/database → `joindiff`.
     - Cross-database with Primary Key → `hashdiff` (or `bucket_hash` when source/target is Athena or MaxCompute and cost optimization is preferred).
   - **Single-Scan Bucket Checksum (`bucket_hash`):**
     - Run 1 query per side: group rows into $N$ deterministic buckets (`MOD(md5_as_int(pk), N)`), computing `COUNT(*)` and `SUM(md5_as_int(concat_all_cols))` per bucket.
     - Compare bucket checksums in Python.
     - Only fetch/bisect rows for buckets whose checksum or count differs (reduces Athena/MaxCompute query count from 50+ to 2 queries for matching tables).
   - **Composite Keys & Keyless Tables:**
     - Support multi-column `primary_keys` via deterministic delimiter concatenation.
     - For tables with no `primary_keys`, run a keyless fallback comparing full-row hash frequency distribution + numeric column aggregates.
   - **Value Normalization (`validator/engine/normalizers.py`):**
     - Decimal canonicalization: strip trailing zeros or format to fixed scale before hashing.
     - Timestamp canonicalization: force UTC and truncate to common precision (e.g., seconds for Athena `timestamp` ↔ MaxCompute `DATETIME`).
     - Configurable `float_abs_tol`, string trimming, and `NULL` vs `""` equivalence.

**Verification Gate (Phase 3):**
- Unit and integration tests seeding synthetic discrepancies (missing row, extra row, 1-cent decimal diff, timestamp precision shift, dropped column, type narrowing) and asserting 100% accurate detection across all 3 tiers.

---

### Phase 4: Batch Orchestrator & SIT Reporting
**Goal:** Automate multi-table SIT runs with concurrency control, resumability, and audit-ready reports.

**Tasks:**
1. **Manifest & Connection Loader (`validator/config/`):**
   - Pydantic v2 validation for YAML manifests with clear error messages.
   - Recursive `${ENV_VAR}` substitution (fail fast if a required env var is unset).
2. **Batch Runner (`validator/orchestrator/runner.py`):**
   - Execute table validations concurrently using `ThreadPoolExecutor`.
   - Enforce per-connection `threading.Semaphore(max_concurrency)` to prevent cloud API throttling (`TooManyRequestsException`).
   - Implement exponential backoff retries for transient network/cloud errors.
   - Implement fail-fast rules (skip Tier 3 if Tier 2 fails or Tier 1 count diff exceeds threshold).
3. **Checkpointing (`validator/orchestrator/checkpoint.py`):**
   - Persist per-table run status and results to a local SQLite database (`.validator_state/<run_name>.db`).
   - Support `--resume` flag to skip tables that already passed in an interrupted run.
4. **SIT Reporting (`validator/reporting/`):**
   - **HTML Report (`sit_report.html.j2`):** Single self-contained HTML file (inline CSS/JS) with:
     - Executive KPI summary (Total Tables, Passed, Failed, Warned, Total Elapsed Time, Athena Bytes Scanned).
     - Filterable table-by-table matrix (Count / Schema / Data status badges).
     - Expandable mismatch details showing partition deltas, schema diffs, and top $K$ differing PKs (plus Source vs. Target values only if `include_sample_values: true`).
   - **Exporters (`exporters.py`):**
     - `summary.json` (full structured results).
     - `mismatches/<table_name>.csv` (mismatched PKs/rows).
     - `junit.xml` (CI/CD pipeline integration).
5. **Unified CLI (`validator/cli.py`):**
   - `data-validator run --config configs/sit_manifest.yaml [--resume] [--include-sample-values]`
   - `data-validator test-connection --config configs/sit_manifest.yaml`
   - `data-validator validate-config --config configs/sit_manifest.yaml`
   - Return exit code `0` on full pass, `1` on validation failure, `2` on config/runtime error.

**Verification Gate (Phase 4):**
- End-to-end CLI test running a multi-table YAML manifest against local/mocked sources, verifying checkpoint resume behavior, exit codes, and valid generation of HTML, JSON, CSV, and JUnit XML reports.

---

### Phase 5: Production Hardening & Packaging
**Goal:** Prepare the tool for team-wide deployment and CI/CD execution.

**Tasks:**
1. **Security & PII Hygiene:**
   - Redact passwords, access keys, and tokens from all logs and error tracebacks.
   - Enforce `include_sample_values: false` by default so raw customer data is never written to HTML/CSV reports unintentionally.
2. **Observability:**
   - Structured logging (`--log-format json|console`) with per-table timing, query counts, and Athena bytes scanned.
3. **Containerization:**
   - Multi-stage `Dockerfile` (Python 3.11-slim + `unixodbc` for MSSQL support) with `data-validator` as entrypoint.
4. **Documentation:**
   - Update `README.md` with quickstart, dialect URI examples, and SIT workflow guide.

**Verification Gate (Phase 5):**
- `docker build` succeeds cleanly.
- Full `pytest` suite passes with `>=80%` coverage on `validator/`.

---

## 6. Critical Edge Cases (Athena ↔ MaxCompute)
The implementation **must** explicitly handle these known migration pitfalls:
1. **Timestamp Precision:** Athena `timestamp` (ms/µs) vs. MaxCompute `DATETIME` (seconds). Default normalizer must cast both to `YYYY-MM-DD HH:MM:SS` in UTC before hashing unless overridden.
2. **Decimal Formatting:** `10.50` vs. `10.5` produces different MD5 hashes. Always normalize decimals to a fixed scale or strip trailing zeros consistently on both engines before string concatenation.
3. **Complex Types (`ARRAY`, `MAP`, `STRUCT`):** Serialization whitespace and map key ordering differ between Athena and MaxCompute. Warn when hashing complex columns and support excluding them or canonicalizing via JSON serialization.
4. **Partition Pruning:** Always inject `WHERE` / `partition_column` filters into every Tier 1 and Tier 3 query to prevent full-table scans on S3/Athena and respect MaxCompute's `allow_fullscan=false` guardrail.