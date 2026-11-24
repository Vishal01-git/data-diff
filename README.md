# Enterprise Data-Validator & Diff Engine

[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

An enterprise-grade, high-performance cross-database data validation and reconciliation engine. Engineered specifically for mission-critical **System Integration Testing (SIT)**, cloud database migrations (e.g., **AWS Glue/Athena &rarr; Alibaba Cloud MaxCompute/ODPS**, **SQL Server &rarr; PostgreSQL**), and ongoing automated data parity auditing.

---

## 🚀 Key Highlights & Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Declarative SIT Manifest                        │
│                     (YAML + Recursive ${ENV_VAR})                      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                       ┌────────────▼───────────┐
                       │   Batch Orchestrator   │
                       │   (Worker Semaphores)  │
                       └────────────┬───────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────┐           ┌───────────────┐           ┌───────────────┐
│    Tier 1     │           │    Tier 2     │           │    Tier 3     │
│ Count & Volume│ ────────> │ Canonical     │ ────────> │ Single-Scan   │
│ Validation    │           │ Schema Diff   │           │ Bucket Hash   │
└───────────────┘           └───────────────┘           └───────────────┘
  (Total counts &             (13 Canonical types,        (2 queries total;
   partition deltas)           precision & scale)          50+ query savings)
        │                           │                           │
        └───────────────────────────┼───────────────────────────┘
                                    │
                       ┌────────────▼───────────┐
                       │   Audit-Ready Reports  │
                       │   HTML / JSON / CSV    │
                       │   JUnit XML for CI/CD  │
                       └────────────────────────┘
```

- **3-Tier Cascading Validation Engine:**
  1. **Tier 1 (Count & Volume):** Total table counts, partition breakdowns, and numeric/null profiling across arbitrary filter slices.
  2. **Tier 2 (Schema Validation):** Maps disparate database types to 13 canonical logical types (`boolean`, `int`, `decimal`, `float`, `text`, `timestamp_tz`, `timestamp_no_tz`, `date`, `time`, `binary`, `json`, `array`, `uuid`). Distinguishes safe type widening from critical type narrowing and detects decimal scale/precision drift.
  3. **Tier 3 (Single-Scan Bucket Hash Data Diff):** Verifies row parity using deterministic bucket checksums (`MOD(ABS(md5_as_int(pk)), N)`). Verifies matching tables in **exactly 2 queries** instead of 50+ recursive bisections, dramatically reducing AWS Athena and MaxCompute scan costs.
- **Enterprise Cloud Dialects:**
  - **AWS Athena / AWS Glue:** Fast-path schema introspection via Glue Data Catalog API with zero S3 query scans, deterministic 15-character hex MD5 decimal hashing, and byte scan metric capture.
  - **Alibaba Cloud MaxCompute (ODPS):** Zero-scan metadata introspection via `get_table().table_schema`, backtick identifier quoting, fullscan partition guardrails, and matching 15-character MD5 decimal hashing.
  - **Microsoft SQL Server (MSSQL):** Normalized MD5 `HashBytes` with explicit `CAST(s AS VARCHAR(MAX))` preventing truncation, boolean integer mapping, and timezone offsets.
  - **PostgreSQL / DuckDB / Snowflake / BigQuery / Oracle:** Full cross-engine support.
- **Fail-Fast & Concurrency Control:**
  - Independent `threading.Semaphore(max_concurrency)` per connection prevents cloud rate-limiting (`TooManyRequestsException`).
  - Deadlock-free semaphore acquisition using lexicographical connection sorting.
  - Automatic fail-fast cascading: skips Tier 3 queries if schema validation fails or count deltas exceed thresholds.
- **SQLite Checkpointing & Resumability:**
  - Persists intermediate per-table results in `.validator_state/<run_name>.db`.
  - Re-run interrupted batches with `--resume` to skip previously passed tables.
- **Security & PII Hygiene:**
  - Automatic masking of passwords, access keys, and tokens across all console logs, structured logs, and error tracebacks.
  - `include_sample_values: false` by default ensures raw customer data is never written to reports or logs.

---

## 📦 Installation

Requirements: Python `>=3.11`.

```bash
# Clone the repository
git clone https://github.com/visha/data-diff.git
cd data-diff

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install base package
pip install -e .

# Install with production database dialect extras
pip install -e ".[athena,maxcompute,mssql,postgres]"

# Install development & test dependencies
pip install -e ".[dev]"
```

---

## 🛠️ Unified CLI Reference (`data-validator`)

The unified CLI provides command-line control for single table ad-hoc diffs and multi-table batch runs:

```
Usage: data-validator [OPTIONS] COMMAND [ARGS]...

Commands:
  run              Run batch SIT reconciliation from declarative YAML manifest.
  validate         Execute full multi-table batch validation (alias for run).
  test-connection  Test connectivity to databases declared in manifest.
  validate-config  Validate SIT manifest syntax and environment variable resolution.
  diff             Run ad-hoc row-level data diff on a single table pair.
  check-schema     Run schema comparison between source and target tables.
  check-counts     Run row count and volume comparison between source and target tables.
```

### 1. Batch SIT Validation (`run` or `validate`)

```bash
# Execute batch validation using a declarative manifest
data-validator run --config configs/sit_manifest.yaml

# Resume an interrupted run, skipping already-passed tables
data-validator run --config configs/sit_manifest.yaml --resume

# Run with structured JSON logging for Datadog / CloudWatch ingestion
data-validator run --config configs/sit_manifest.yaml --log-format json --log-level INFO

# Include raw sample values for debugging (PII sensitive)
data-validator run --config configs/sit_manifest.yaml --include-sample-values
```

### 2. Pre-Flight Configuration & Connection Verification

```bash
# Validate manifest YAML, environment variables, and table mappings without running queries
data-validator validate-config --config configs/sit_manifest.yaml

# Test connectivity, latency, and credentials across all declared endpoints
data-validator test-connection --config configs/sit_manifest.yaml

# Test a single connection endpoint
data-validator test-connection --config configs/sit_manifest.yaml --connection aws_glue_prod
```

### 3. Ad-Hoc Single Table Commands

```bash
# Row-Level Data Diff (Single-Scan Bucket Hash)
data-validator diff \
  --source-uri "athena://.../sales_db/orders" \
  --target-uri "odps://.../sales_sit/orders" \
  --primary-key order_id \
  --mode bucket_hash \
  --buckets 64 \
  --where "dt >= '2026-10-01'"

# Cross-Dialect Schema Diff
data-validator check-schema \
  --source-uri "athena://.../sales_db/orders" \
  --target-uri "odps://.../sales_sit/orders" \
  --allow-extra-columns

# Row Count & Partition Volume Check
data-validator check-counts \
  --source-uri "athena://.../sales_db/orders" \
  --target-uri "odps://.../sales_sit/orders" \
  --partition-col dt \
  --threshold 0.05
```

---

## 📑 Declarative SIT Manifest (`sit_manifest.yaml`)

Manifests support recursive environment variable interpolation (`${ENV_VAR}` or `${ENV_VAR:-default}`):

```yaml
version: "1.0"
run_name: "glue_to_maxcompute_sit_batch_01"

connections:
  aws_glue_prod:
    type: athena
    region: "${AWS_REGION:-us-east-1}"
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
    mode: "auto"                      # auto | bucket_hash | hashdiff | joindiff
    bucket_count: 64                  # 64 deterministic buckets
    float_abs_tol: 0.0001
    normalize_timestamps_utc: true
    treat_empty_string_as_null: false
    max_mismatch_samples: 100
  reporting:
    output_dir: "./reports"
    formats: ["html", "json", "csv", "junit"]
    include_sample_values: false      # PII safety default

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
      order_amt: "order_amount"
```

---

## 📊 SIT Reporting & CI/CD Integration

Every validation run automatically outputs four audit-ready reporting artifacts in `./reports`:

| File | Description | Purpose |
| :--- | :--- | :--- |
| `sit_report.html` | Self-contained, zero-external-dependency HTML dashboard | Executive review, filterable table matrices, expandable diff accordions |
| `summary.json` | Full structured execution result | Downstream metrics ingestion, webhooks, alerting |
| `mismatches.csv` | Flattened CSV of mismatched primary keys and column deltas | Data engineering defect triage |
| `junit.xml` | Standard JUnit XML test report | Native GitLab CI / GitHub Actions / Jenkins test pass/fail integration |

### Exit Codes
- `0`: All tables and tiers PASSED.
- `1`: Validation completed, but one or more tables FAILED.
- `2`: Configuration syntax error, unresolvable environment variable, or fatal connection failure.

---

## 🐳 Docker Deployment

A multi-stage, hardened Dockerfile is provided:

```bash
# Build the production container image
docker build -t data-validator:latest .

# Run validation via Docker
docker run --rm \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/reports:/app/reports \
  -e AWS_REGION=us-east-1 \
  -e ATHENA_S3_STAGING=s3://my-bucket/staging/ \
  -e ODPS_ENDPOINT=https://service.odps.aliyun.com/api \
  -e ODPS_PROJECT=my_project \
  -e ODPS_ACCESS_ID=my_access_id \
  -e ODPS_ACCESS_KEY=my_secret_key \
  data-validator:latest run --config /app/configs/sit_manifest.yaml
```

### Local Testing with Docker Compose
A local integration environment with PostgreSQL instances can be launched via:
```bash
docker-compose up -d
```

---

## 🧪 Testing & Verification

Run the comprehensive unit and integration test suite:

```bash
# Run linting check
ruff check .

# Run full test suite with test coverage
pytest tests -v --cov=validator --cov-report=term-missing
```

---

## 📄 License
This project is licensed under the terms of the [MIT License](LICENSE).
