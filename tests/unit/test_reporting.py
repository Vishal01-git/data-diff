import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from xml.etree import ElementTree as ET

from validator.engine.count_validator import CountValidationResult
from validator.engine.data_validator import DataValidationResult, MismatchSample
from validator.engine.schema_validator import ColumnSchemaDiff, SchemaValidationResult
from validator.orchestrator.runner import BatchValidationResult, TableValidationResult
from validator.reporting.exporters import export_junit_xml, export_mismatches_csv, export_summary_json
from validator.reporting.html_report import generate_html_report


class TestReporting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.out_dir = Path(self.temp_dir)

        # Mock results
        tbl_pass = TableValidationResult(
            table_name="orders",
            source_table="sales.orders",
            target_table="sales.orders",
            status="PASSED",
            passed=True,
            count_result=CountValidationResult(
                source_table="orders",
                target_table="orders",
                source_total_count=1000,
                target_total_count=1000,
                passed=True,
            ),
            schema_result=SchemaValidationResult(
                source_table="orders",
                target_table="orders",
                passed=True,
                column_diffs=[
                    ColumnSchemaDiff(
                        column_name="id",
                        diff_type="MATCH",
                        severity="OK",
                        message="Types match (INT64).",
                    )
                ],
            ),
            data_result=DataValidationResult(
                source_table="orders",
                target_table="orders",
                mode_used="bucket_hash",
                passed=True,
                total_rows_compared=1000,
            ),
            execution_time_seconds=1.23,
        )

        tbl_fail = TableValidationResult(
            table_name="payments",
            source_table="sales.payments",
            target_table="sales.payments",
            status="FAILED",
            passed=False,
            count_result=CountValidationResult(
                source_table="payments",
                target_table="payments",
                source_total_count=500,
                target_total_count=500,
                passed=True,
            ),
            schema_result=SchemaValidationResult(
                source_table="payments",
                target_table="payments",
                passed=True,
            ),
            data_result=DataValidationResult(
                source_table="payments",
                target_table="payments",
                mode_used="bucket_hash",
                passed=False,
                total_rows_compared=500,
                differing_rows_count=1,
                mismatch_samples=[
                    MismatchSample(
                        key_values={"id": 42},
                        diff_type="VALUE_DIFF",
                        column_diffs={"amount": (100.0, 100.01)},
                    )
                ],
            ),
            execution_time_seconds=0.85,
        )

        self.batch_res = BatchValidationResult(
            run_name="sit_reporting_test",
            total_tables=2,
            passed_tables=1,
            failed_tables=1,
            skipped_tables=0,
            total_rows_compared=1500,
            total_duration_seconds=2.08,
            overall_status="FAILED",
            table_results=[tbl_pass, tbl_fail],
            start_time_iso="2026-11-24T00:00:00Z",
            end_time_iso="2026-11-24T00:00:02Z",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_html_report_generation(self):
        html_file = self.out_dir / "sit_report.html"
        generated = generate_html_report(self.batch_res, html_file)
        self.assertTrue(generated.exists())

        content = generated.read_text(encoding="utf-8")
        self.assertIn("SIT Data Reconciliation Report", content)
        self.assertIn("sit_reporting_test", content)
        self.assertIn("orders", content)
        self.assertIn("payments", content)
        self.assertIn("VALUE DIFF", content)
        # Default PII hygiene masks raw sample values
        self.assertIn("[VALUE DIFF - REDACTED]", content)

        # When include_sample_values is True
        self.batch_res.include_sample_values = True
        generate_html_report(self.batch_res, html_file)
        content_unmasked = html_file.read_text(encoding="utf-8")
        self.assertIn("100.01", content_unmasked)

    def test_export_summary_json(self):
        json_file = self.out_dir / "summary.json"
        export_summary_json(self.batch_res, json_file)
        self.assertTrue(json_file.exists())

        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["run_name"], "sit_reporting_test")
        self.assertEqual(data["total_tables"], 2)
        self.assertEqual(data["failed_tables"], 1)

    def test_export_mismatches_csv(self):
        csv_file = self.out_dir / "mismatches.csv"
        # Test default PII masking
        export_mismatches_csv(self.batch_res, csv_file, include_sample_values=False)
        self.assertTrue(csv_file.exists())

        with open(csv_file, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        self.assertEqual(len(reader), 1)
        row = reader[0]
        self.assertEqual(row["table_name"], "payments")
        self.assertEqual(row["diff_type"], "VALUE_DIFF")
        self.assertEqual(row["column_name"], "amount")
        self.assertEqual(row["source_value"], "[REDACTED]")
        self.assertEqual(row["target_value"], "[REDACTED]")

        # Test with include_sample_values=True
        export_mismatches_csv(self.batch_res, csv_file, include_sample_values=True)
        with open(csv_file, "r", encoding="utf-8") as f:
            reader_unmasked = list(csv.DictReader(f))
        self.assertEqual(reader_unmasked[0]["source_value"], "100.0")
        self.assertEqual(reader_unmasked[0]["target_value"], "100.01")

    def test_export_junit_xml(self):
        xml_file = self.out_dir / "junit.xml"
        export_junit_xml(self.batch_res, xml_file)
        self.assertTrue(xml_file.exists())

        tree = ET.parse(str(xml_file))
        root = tree.getroot()
        self.assertEqual(root.tag, "testsuites")
        suite = root.find("testsuite")
        self.assertIsNotNone(suite)
        self.assertEqual(suite.attrib["tests"], "2")
        self.assertEqual(suite.attrib["failures"], "1")

        testcases = suite.findall("testcase")
        self.assertEqual(len(testcases), 2)
        fail_tc = next(t for t in testcases if t.attrib["name"] == "payments")
        self.assertIsNotNone(fail_tc.find("failure"))


if __name__ == "__main__":
    unittest.main()
