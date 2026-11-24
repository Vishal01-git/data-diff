import os
from pathlib import Path
import shutil
import tempfile
import unittest

from data_diff import connect
from validator.config.connections import ConnectionResolver
from validator.config.manifest import (
    ConnectionConfig,
    DefaultsConfig,
    FailFastConfig,
    SITManifest,
    TableConfig,
    TableEndpointConfig,
)
from validator.orchestrator.checkpoint import CheckpointTracker
from validator.orchestrator.runner import BatchRunner
from validator.reporting.exporters import export_junit_xml, export_mismatches_csv, export_summary_json
from validator.reporting.html_report import generate_html_report


class TestE2EValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.out_dir = Path(self.temp_dir)
        self.state_dir = self.out_dir / "state"

        # Create 2 DuckDB database files: source.duckdb and target.duckdb
        self.src_db_file = self.out_dir / "source.duckdb"
        self.tgt_db_file = self.out_dir / "target.duckdb"

        from data_diff.databases.duckdb import DuckDB

        self.src_db = DuckDB(filepath=str(self.src_db_file))
        self.tgt_db = DuckDB(filepath=str(self.tgt_db_file))

        # Setup 3 table pairs:
        # 1. orders: 100% identical data
        self.src_db.query("CREATE TABLE orders (id INT, amount DOUBLE, status VARCHAR)")
        self.tgt_db.query("CREATE TABLE orders (id INT, amount DOUBLE, status VARCHAR)")
        for i in range(1, 21):
            self.src_db.query(f"INSERT INTO orders VALUES ({i}, {i * 10.0:.2f}, 'SHIPPED')")
            self.tgt_db.query(f"INSERT INTO orders VALUES ({i}, {i * 10.0:.2f}, 'SHIPPED')")

        # 2. customers: Schema mismatch (source has address, target lacks address)
        self.src_db.query("CREATE TABLE customers (id INT, name VARCHAR, address VARCHAR)")
        self.tgt_db.query("CREATE TABLE customers (id INT, name VARCHAR)")  # missing address
        self.src_db.query("INSERT INTO customers VALUES (1, 'Alice', '123 Main St')")
        self.tgt_db.query("INSERT INTO customers VALUES (1, 'Alice')")

        # 3. payments: Data mismatch (row 5 amount is 50.0 on src, 50.01 on tgt)
        self.src_db.query("CREATE TABLE payments (id INT, amount DOUBLE)")
        self.tgt_db.query("CREATE TABLE payments (id INT, amount DOUBLE)")
        for i in range(1, 11):
            amt = 50.01 if i == 5 else i * 10.0
            self.tgt_db.query(f"INSERT INTO payments VALUES ({i}, {amt:.2f})")
            self.src_db.query(f"INSERT INTO payments VALUES ({i}, {i * 10.0:.2f})")

        # Construct SIT manifest
        self.manifest = SITManifest(
            run_name="e2e_sit_test_run",
            connections={
                "src_conn": ConnectionConfig(type="duckdb", filepath=str(self.src_db_file)),
                "tgt_conn": ConnectionConfig(type="duckdb", filepath=str(self.tgt_db_file)),
            },
            defaults=DefaultsConfig(
                tiers=["count", "schema", "data"],
                fail_fast=FailFastConfig(skip_data_on_schema_fail=True),
            ),
            tables=[
                TableConfig(
                    name="orders",
                    source=TableEndpointConfig(connection="src_conn", table="orders"),
                    target=TableEndpointConfig(connection="tgt_conn", table="orders"),
                    primary_keys=["id"],
                ),
                TableConfig(
                    name="customers",
                    source=TableEndpointConfig(connection="src_conn", table="customers"),
                    target=TableEndpointConfig(connection="tgt_conn", table="customers"),
                    primary_keys=["id"],
                ),
                TableConfig(
                    name="payments",
                    source=TableEndpointConfig(connection="src_conn", table="payments"),
                    target=TableEndpointConfig(connection="tgt_conn", table="payments"),
                    primary_keys=["id"],
                ),
            ],
        )

    def tearDown(self):
        self.src_db.close()
        self.tgt_db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_e2e_batch_run_and_reporting(self):
        checkpoint = CheckpointTracker(self.manifest.run_name, state_dir=self.state_dir)
        runner = BatchRunner(self.manifest, checkpoint_tracker=checkpoint, max_workers=2)

        # Run 1: Full batch
        result = runner.run()
        self.assertEqual(result.total_tables, 3)
        self.assertEqual(result.passed_tables, 1)  # orders
        self.assertEqual(result.failed_tables, 2)  # customers (schema), payments (data)
        self.assertEqual(result.overall_status, "FAILED")

        table_map = {t.table_name: t for t in result.table_results}
        self.assertTrue(table_map["orders"].passed)
        self.assertFalse(table_map["customers"].passed)
        self.assertFalse(table_map["payments"].passed)

        # Assert fail-fast on customers: data_result is None because schema failed
        self.assertIsNone(table_map["customers"].data_result)

        # Assert data mismatch on payments
        self.assertIsNotNone(table_map["payments"].data_result)
        self.assertFalse(table_map["payments"].data_result.passed)
        self.assertGreater(len(table_map["payments"].data_result.mismatch_samples), 0)

        # Run 2: Test Checkpoint Resume
        # orders passed in Run 1 -> in Run 2 with resume=True it should be SKIPPED
        resume_runner = BatchRunner(self.manifest, checkpoint_tracker=checkpoint, max_workers=2, resume=True)
        resume_result = resume_runner.run()

        resume_table_map = {t.table_name: t for t in resume_result.table_results}
        self.assertEqual(resume_table_map["orders"].status, "SKIPPED")

        # Test report generation across all 4 formats
        reports_dir = self.out_dir / "reports"
        html_p = generate_html_report(result, reports_dir / "sit_report.html")
        json_p = export_summary_json(result, reports_dir / "summary.json")
        csv_p = export_mismatches_csv(result, reports_dir / "mismatches.csv")
        junit_p = export_junit_xml(result, reports_dir / "junit.xml")

        self.assertTrue(html_p.exists() and html_p.stat().st_size > 0)
        self.assertTrue(json_p.exists() and json_p.stat().st_size > 0)
        self.assertTrue(csv_p.exists() and csv_p.stat().st_size > 0)
        self.assertTrue(junit_p.exists() and junit_p.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
