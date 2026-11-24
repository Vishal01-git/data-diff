# 📖 Data Validator — Practical User Guide (`uses.md`)

Welcome to **Data Validator**! This guide is written for engineers and analysts who want clear, straightforward answers and practical examples without needing to understand the underlying Python internals.

---

## 📌 Quick Answers to Common Doubts

### 1. "Do I ever need to edit the Python code files to configure tables or connections?"
> **Answer: NEVER.** 
> You never touch or edit any files inside `validator/`, `data_diff/`, or any `.py` files. The tool is already built and installed as a command-line program (`data-validator`). You only interact with it in your terminal either by **running simple CLI commands directly** or by **editing YAML configuration files**.

---

### 2. "Do I have to create a new YAML file every single time I want to compare tables?"
> **Answer: NO.**
> You have two flexible ways to use the tool:
> 
> - **Way 1: The Zero-File CLI Method (No YAML needed):** If you just want to quickly compare one table right now, you run a single one-line command directly in your terminal. No files created, no YAML written.
> - **Way 2: The Master Manifest Method (One YAML file for everything):** You can keep a single master configuration file (e.g. `configs/sit_master.yaml`) where all your databases (Athena, PostgreSQL, SQL Server, etc.) are declared once. Whenever you want to test new tables, you just add them to the list in that same file!

---

### 3. "What if I have to configure multiple tables (e.g. 5, 20, or 100 tables)?"
> **Answer: You list them all in one YAML file.**
> The tool is built as a **batch runner**. You can list 50 tables under the `tables:` section. When you run `data-validator run`, it will:
> - Validate all 50 tables **in parallel** across background worker threads.
> - Display a live progress overview.
> - Automatically produce **one single interactive HTML report** containing all 50 tables with a search bar and status filters.
> - If 48 tables pass and 2 fail, you can fix the issue and re-run with `--resume` to skip the 48 that already passed!

---

## 🚀 Two Ways to Use the Tool

```
                        ┌───────────────────────────────────────────────┐
                        │             HOW DO YOU WANT TO WORK?           │
                        └───────┬───────────────────────────────┬───────┘
                                │                               │
                     Quick Single-Table Check         Batch Multi-Table SIT Run
                                │                               │
                                ▼                               ▼
                     METHOD 1: Direct CLI            METHOD 2: YAML Manifest
                     (No files or YAML needed)       (Audit-ready HTML dashboard)
```

---

## ⚡ Method 1: The Zero-File CLI Method (Instant Checks)

Use this method when you want to compare two tables on the fly without creating or editing any files.

### 1.1 Check Row Counts (Tier 1)
Checks if the total number of rows match between two tables:

```powershell
# Athena to Athena:
data-validator check-counts `
  --source-uri "athena://sales_db/orders?s3_staging_dir=s3://my-bucket/staging/&profile_name=default" `
  --target-uri "athena://analytics_db/orders?s3_staging_dir=s3://my-bucket/staging/&profile_name=default"

# Athena to PostgreSQL:
data-validator check-counts `
  --source-uri "athena://sales_db/orders?s3_staging_dir=s3://my-bucket/staging/&profile_name=default" `
  --target-uri "postgresql://postgres_user:my_secret_pass@localhost:5432/my_db/public/orders"
```

### 1.2 Check Schema & Column Types (Tier 2)
Checks if columns exist on both sides and verifies that their data types are compatible:

```powershell
data-validator check-schema `
  --source-uri "athena://sales_db/orders?s3_staging_dir=s3://my-bucket/staging/&profile_name=default" `
  --target-uri "postgresql://postgres_user:my_secret_pass@localhost:5432/my_db/public/orders"
```

### 1.3 Row-Level Data Diff (Tier 3)
Calculates deterministic checksums across all columns to find exact differing rows:

```powershell
data-validator diff `
  --source-uri "athena://sales_db/orders?s3_staging_dir=s3://my-bucket/staging/&profile_name=default" `
  --target-uri "postgresql://postgres_user:my_secret_pass@localhost:5432/my_db/public/orders" `
  --primary-key id `
  --mode bucket_hash
```

---

## 📁 Method 2: The Batch Manifest Method (Multi-Table SIT)

Use this method when you have multiple tables to validate together or when you need audit-ready reports (`.html`, `.json`, `.csv`, `.xml`).

### 2.1 How Multi-Table Configuration Works
In your YAML file, you define your databases **once** under `connections:`, and then list as many tables as you want under `tables:`.

Here is an example configuring **4 different tables at the same time** (including cross-database validation):

```yaml
version: "1.0"
run_name: "enterprise_batch_sit_run"

# ------------------------------------------------------------------------------
# 1. DEFINE YOUR CONNECTIONS ONCE
# ------------------------------------------------------------------------------
connections:
  # Athena with Okta Profile
  athena_prod:
    type: athena
    region: "us-east-1"
    s3_staging_dir: "s3://my-athena-staging-bucket/staging/"
    profile_name: "default"          # Your Okta AWS Profile name

  # PostgreSQL Database
  postgres_sit:
    type: postgres
    host: "postgres-host.internal"
    port: 5432
    user: "db_user"
    password: "${PG_PASSWORD}"       # Pulled securely from environment variable
    database: "analytics"
    schema: "public"

  # SQL Server Database
  mssql_dw:
    type: mssql
    host: "sqlserver.internal"
    user: "sa"
    password: "${MSSQL_PASSWORD}"
    database: "DataWarehouse"
    schema: "dbo"

# ------------------------------------------------------------------------------
# 2. GLOBAL DEFAULT SETTINGS
# ------------------------------------------------------------------------------
defaults:
  tiers: ["count", "schema", "data"]  # Runs Count, Schema, and Data parity checks
  reporting:
    output_dir: "./reports"
    formats: ["html", "json", "csv", "junit"]
    include_sample_values: false     # Masks PII customer data in reports

# ------------------------------------------------------------------------------
# 3. LIST AS MANY TABLES AS YOU WANT
# ------------------------------------------------------------------------------
tables:
  # Table 1: Athena -> Athena (e.g. Raw to Curated)
  - name: "orders_reconciliation"
    source:
      connection: athena_prod
      table: "raw_db.orders"
    target:
      connection: athena_prod
      table: "curated_db.orders"
    primary_keys: ["order_id"]

  # Table 2: Athena -> PostgreSQL (Cross-Database)
  - name: "customers_reconciliation"
    source:
      connection: athena_prod
      table: "curated_db.customers"
    target:
      connection: postgres_sit
      table: "public.customers"
    primary_keys: ["customer_id"]

  # Table 3: SQL Server -> PostgreSQL (Cross-Database)
  - name: "products_reconciliation"
    source:
      connection: mssql_dw
      table: "dbo.dim_product"
    target:
      connection: postgres_sit
      table: "public.dim_product"
    primary_keys: ["product_sku"]

  # Table 4: Table with a Partition Filter and Column Exclusions
  - name: "transactions_filtered"
    source:
      connection: athena_prod
      table: "finance_db.transactions"
      where: "dt >= '2026-10-01'"     # Only compare recent transactions
    target:
      connection: postgres_sit
      table: "public.transactions"
      where: "dt >= '2026-10-01'"
    primary_keys: ["tx_id"]
    partition_column: "dt"            # Shows row counts grouped by date
    exclude_columns: ["etl_updated_at"] # Ignore columns created by ETL pipelines
```

---

## 🛠️ Step-by-Step Commands to Run a Manifest

### Step A: Check your YAML file for mistakes
```powershell
data-validator validate-config --config configs/my_manifest.yaml
```
*Why run this?* It verifies your YAML formatting, checks that all connection names match, and ensures no required environment variables are missing.

### Step B: Test if your databases connect
```powershell
data-validator test-connection --config configs/my_manifest.yaml
```
*Why run this?* It tests your Okta authentication, database passwords, network firewalls, and displays connection latency.

### Step C: Execute the multi-table run
```powershell
data-validator run --config configs/my_manifest.yaml
```

### Step D: What if a large run gets interrupted?
If you are validating 30 tables and your machine reboots or internet drops after 20 tables:
```powershell
data-validator run --config configs/my_manifest.yaml --resume
```
The `--resume` flag automatically reads the checkpoint database (`.validator_state/`) and **skips all tables that already passed**, saving you time and Athena scan costs!

---

## 🔐 How Okta Authentication Works with Athena

When using Okta with AWS, you do **not** type your Okta password into the YAML file. Instead:

1. You authenticate via your company's Okta CLI tool in PowerShell:
   ```powershell
   # Typical enterprise commands (use the one your company provided):
   saml2aws login
   # OR
   gimme-aws-creds
   # OR
   aws sso login --profile my-okta-profile
   ```
2. Approve the Okta Push notification on your phone.
3. This creates temporary AWS tokens in your `~/.aws/credentials` file.
4. In your YAML file, simply set:
   ```yaml
   profile_name: "my-okta-profile"  # Or "default"
   ```
   The validator automatically uses your authenticated Okta session!

> [!TIP]
> **Check your login status anytime:**
> Run `aws sts get-caller-identity` in PowerShell. If it prints your AWS Account and Role ARN, you are logged in and ready to validate Athena tables.

---

## 📊 Where Are the Reports and What Do They Mean?

After running `data-validator run`, open the `./reports` directory:

```powershell
# Open the interactive HTML dashboard on Windows:
start reports/sit_report.html
```

### What's Inside the Reports:
1. **`sit_report.html` (The Dashboard):**
   - **Summary Cards:** Shows Total Tables, Passed, Failed, Total Rows Compared, Total Run Time, and S3 Bytes Scanned.
   - **Search & Filters:** Search for any table name or filter by `PASSED` / `FAILED`.
   - **Accordions:** Click on any table to see:
     - **Tier 1:** Row counts on Source vs Target and count differences.
     - **Tier 2:** Column types compared side-by-side with warnings for type widening or errors for missing columns.
     - **Tier 3:** Differing row counts and sample mismatched primary keys.
2. **`summary.json`:** Full structured JSON for automated pipelines.
3. **`mismatches.csv`:** A spreadsheet listing each mismatched primary key and which columns differed.
4. **`junit.xml`:** For integration with Jenkins, GitHub Actions, or GitLab CI test dashboards.

---

## 📋 Connection Reference Cheat Sheet

Copy-paste these connection snippets into your YAML files for any database type:

### AWS Athena (Okta / IAM)
```yaml
my_athena:
  type: athena
  region: "us-east-1"
  s3_staging_dir: "s3://my-query-results-bucket/staging/"
  workgroup: "primary"
  catalog: "AwsDataCatalog"
  profile_name: "${AWS_PROFILE:-default}"
```

### PostgreSQL
```yaml
my_postgres:
  type: postgres
  host: "localhost"
  port: 5432
  user: "postgres"
  password: "${PG_PASSWORD}"
  database: "my_database"
  schema: "public"
```

### Microsoft SQL Server (MSSQL)
```yaml
my_mssql:
  type: mssql
  host: "sqlserver.corp.local"
  port: 1433
  user: "sa"
  password: "${MSSQL_PASSWORD}"
  database: "WarehouseDB"
  schema: "dbo"
```

### Alibaba Cloud MaxCompute (ODPS)
```yaml
my_maxcompute:
  type: maxcompute
  endpoint: "https://service.odps.aliyun.com/api"
  project: "my_sit_project"
  access_id: "${ODPS_ACCESS_ID}"
  access_key: "${ODPS_ACCESS_KEY}"
  allow_fullscan: false
```

### DuckDB / SQLite (Local Fast Testing)
```yaml
my_local_db:
  type: duckdb
  database: "C:/path/to/my_database.duckdb"
```

---

## 💡 Summary Workflow for Daily Use

```powershell
# 1. Activate environment
cd c:\Users\visha\Downloads\data-diff
.\.venv\Scripts\Activate.ps1

# 2. Login to Okta (if using Athena)
saml2aws login  # (or your company's login command)

# 3. Test connections
data-validator test-connection --config configs/athena_to_athena.yaml

# 4. Run reconciliation
data-validator run --config configs/athena_to_athena.yaml

# 5. Open report
start reports/sit_report.html
```
