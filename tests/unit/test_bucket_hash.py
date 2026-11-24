from datetime import datetime, timezone
import decimal
import unittest

from data_diff import connect
from data_diff.databases import Athena, DuckDB, MaxCompute, PostgreSQL
from tests.common import TEST_DUCKDB_CONN_STRING
from validator.engine.data_validator import DataValidator, get_bucket_expr, get_concat_row_expr
from validator.engine.normalizers import (
    compare_floats,
    normalize_decimal_val,
    normalize_string_val,
    normalize_timestamp_val,
)


class TestBucketHashAndDataValidator(unittest.TestCase):
    def setUp(self):
        self.db = connect(TEST_DUCKDB_CONN_STRING)
        self.validator = DataValidator(max_concurrency=1)

        # Setup test tables
        self.db.query("DROP TABLE IF EXISTS src_data")
        self.db.query("DROP TABLE IF EXISTS tgt_data")

        self.db.query(
            """
            CREATE TABLE src_data (
                order_id INT,
                item_seq INT,
                amount DOUBLE,
                status VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        self.db.query(
            """
            CREATE TABLE tgt_data (
                order_id INT,
                item_seq INT,
                amount DOUBLE,
                status VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )

        # Insert 50 test rows
        values = []
        for i in range(1, 51):
            values.append(f"({i}, 1, {i * 10.0:.2f}, 'OPEN', TIMESTAMP '2026-10-01 12:00:00')")
        self.db.query(f"INSERT INTO src_data VALUES {', '.join(values)}")
        self.db.query("INSERT INTO tgt_data SELECT * FROM src_data")

    def tearDown(self):
        self.db.query("DROP TABLE IF EXISTS src_data")
        self.db.query("DROP TABLE IF EXISTS tgt_data")

    def test_normalizers(self):
        # Decimal
        self.assertEqual(normalize_decimal_val(decimal.Decimal("123.4500")), "123.45")
        self.assertEqual(normalize_decimal_val(100.0), "100")
        self.assertEqual(normalize_decimal_val("-0.00"), "0")
        self.assertEqual(normalize_decimal_val(123.4, scale=2), "123.40")

        # Timestamp
        dt = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(normalize_timestamp_val(dt), "2026-10-01 14:30:00")
        self.assertEqual(normalize_timestamp_val("2026-10-01 14:30:00.123456"), "2026-10-01 14:30:00")
        self.assertEqual(normalize_timestamp_val("2026-10-01 14:30:00.123456", target_precision=3), "2026-10-01 14:30:00.123")

        # String
        self.assertEqual(normalize_string_val("  hello  "), "hello")
        self.assertIsNone(normalize_string_val("   ", empty_as_null=True))

        # Float comparison
        self.assertTrue(compare_floats(100.00001, 100.00002, abs_tol=0.0001))
        self.assertFalse(compare_floats(100.00, 100.01, abs_tol=0.0001))

    def test_identical_tables_bucket_hash_match(self):
        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=["order_id"],
            mode="bucket_hash",
            bucket_count=16,
        )
        self.assertTrue(res.passed)
        self.assertEqual(res.differing_buckets_count, 0)
        self.assertEqual(res.differing_rows_count, 0)
        self.assertEqual(len(res.mismatch_samples), 0)
        self.assertEqual(res.total_buckets, 16)
        self.assertTrue(all(b.is_match for b in res.bucket_diffs))

    def test_missing_row_detected(self):
        # Delete row 25 on target
        self.db.query("DELETE FROM tgt_data WHERE order_id = 25")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=["order_id"],
            mode="bucket_hash",
            bucket_count=16,
        )
        self.assertFalse(res.passed)
        self.assertGreater(res.differing_buckets_count, 0)
        self.assertGreater(len(res.mismatch_samples), 0)

        missing_sample = next(
            s for s in res.mismatch_samples if s.diff_type == "EXCLUSIVE_SOURCE" and s.key_values["order_id"] == 25
        )
        self.assertIsNotNone(missing_sample)

    def test_extra_row_detected(self):
        # Insert extra row on target
        self.db.query("INSERT INTO tgt_data VALUES (999, 1, 9990.0, 'OPEN', TIMESTAMP '2026-10-01 12:00:00')")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=["order_id"],
            mode="bucket_hash",
            bucket_count=16,
        )
        self.assertFalse(res.passed)
        extra_sample = next(
            s for s in res.mismatch_samples if s.diff_type == "EXCLUSIVE_TARGET" and s.key_values["order_id"] == 999
        )
        self.assertIsNotNone(extra_sample)

    def test_one_cent_decimal_diff_detected(self):
        # Modify amount by 1 cent on target (e.g. 100.00 -> 100.01)
        self.db.query("UPDATE tgt_data SET amount = 100.01 WHERE order_id = 10")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=["order_id"],
            mode="bucket_hash",
            bucket_count=16,
        )
        self.assertFalse(res.passed)
        diff_sample = next(
            s for s in res.mismatch_samples if s.diff_type == "VALUE_DIFF" and s.key_values["order_id"] == 10
        )
        self.assertIn("amount", diff_sample.column_diffs)

    def test_composite_primary_keys(self):
        # Composite primary keys: order_id + item_seq
        self.db.query("UPDATE tgt_data SET status = 'CLOSED' WHERE order_id = 5 AND item_seq = 1")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=["order_id", "item_seq"],
            mode="bucket_hash",
            bucket_count=8,
        )
        self.assertFalse(res.passed)
        diff_sample = res.mismatch_samples[0]
        self.assertEqual(diff_sample.key_values["order_id"], 5)
        self.assertEqual(diff_sample.key_values["item_seq"], 1)
        self.assertEqual(diff_sample.column_diffs["status"], ("OPEN", "CLOSED"))

    def test_keyless_validation(self):
        # Keyless table: no primary keys specified
        res_match = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=[],
            mode="keyless",
        )
        self.assertTrue(res_match.passed)

        # Alter a row on target
        self.db.query("UPDATE tgt_data SET amount = 9999.0 WHERE order_id = 1")
        res_mismatch = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_data",
            target_table="tgt_data",
            primary_keys=[],
            mode="keyless",
        )
        self.assertFalse(res_mismatch.passed)

    def test_mode_routing_logic(self):
        athena_db = Athena.__new__(Athena)
        mc_db = MaxCompute.__new__(MaxCompute)
        pg_db = PostgreSQL.__new__(PostgreSQL)

        # 1. No primary key -> keyless
        mode1 = self.validator._determine_mode(athena_db, mc_db, "auto", [])
        self.assertEqual(mode1, "keyless")

        # 2. Athena/MaxCompute cloud engines -> bucket_hash
        mode2 = self.validator._determine_mode(athena_db, mc_db, "auto", ["id"])
        self.assertEqual(mode2, "bucket_hash")

        mode3 = self.validator._determine_mode(pg_db, athena_db, "auto", ["id"])
        self.assertEqual(mode3, "bucket_hash")

        # 3. Same database instance -> joindiff
        mode4 = self.validator._determine_mode(self.db, self.db, "auto", ["id"])
        self.assertEqual(mode4, "joindiff")

        # 4. Explicit override takes precedence
        mode5 = self.validator._determine_mode(athena_db, mc_db, "hashdiff", ["id"])
        self.assertEqual(mode5, "hashdiff")


if __name__ == "__main__":
    unittest.main()
