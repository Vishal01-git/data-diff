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

        # 5. Even if bucket_hash requested, empty PKs force keyless
        mode6 = self.validator._determine_mode(athena_db, athena_db, "bucket_hash", [])
        self.assertEqual(mode6, "keyless")

    def test_athena_binary_null_and_wide_table_concat(self):
        from data_diff.databases.athena import Dialect as AthenaDialect
        from data_diff.schema import RawColumnInfo
        from validator.engine.data_validator import get_concat_row_expr

        athena_db = Athena.__new__(Athena)
        athena_db._dialect = AthenaDialect()

        schema = {
            "id": RawColumnInfo(column_name="id", data_type="bigint"),
            "rowversion": RawColumnInfo(column_name="rowversion", data_type="varbinary"),
            "tags": RawColumnInfo(column_name="tags", data_type="array<string>"),
            "created_at": RawColumnInfo(column_name="created_at", data_type="timestamp"),
            "is_active": RawColumnInfo(column_name="is_active", data_type="boolean"),
        }

        expr = get_concat_row_expr(
            athena_db,
            ["id", "rowversion", "tags", "created_at", "is_active"],
            schema=schema,
        )
        self.assertIn("array_join(ARRAY[", expr)
        self.assertIn('to_hex("rowversion")', expr)
        self.assertIn('json_format(cast("tags" as json))', expr)
        self.assertIn("COALESCE(", expr)
        self.assertIn("'<NULL>'", expr)
        self.assertNotIn('cast("rowversion" as varchar)', expr)

    def test_duckdb_binary_and_null_columns_data_diff(self):
        # Verify real execution with BLOB (binary) and NULL columns in DuckDB
        self.db.query(
            """
            CREATE TABLE src_bin (
                id INTEGER,
                payload BLOB,
                notes VARCHAR
            );
            CREATE TABLE tgt_bin (
                id INTEGER,
                payload BLOB,
                notes VARCHAR
            );
            INSERT INTO src_bin VALUES
                (1, '\\xDEADBEEF'::BLOB, NULL),
                (2, NULL, 'hello');
            INSERT INTO tgt_bin VALUES
                (1, '\\xDEADBEEF'::BLOB, NULL),
                (2, NULL, 'hello');
            """
        )

        # Should pass in both bucket_hash and keyless modes without cast error or NULL poisoning
        res_bh = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_bin",
            target_table="tgt_bin",
            primary_keys=["id"],
            mode="bucket_hash",
        )
        self.assertTrue(res_bh.passed)
        self.assertEqual(res_bh.total_rows_compared, 2)

        res_kl = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_bin",
            target_table="tgt_bin",
            primary_keys=[],
            mode="keyless",
        )
        self.assertTrue(res_kl.passed)
        self.assertEqual(res_kl.total_rows_compared, 2)

        # Now introduce a 1-byte difference in BLOB and a NULL -> non-NULL change
        self.db.query("UPDATE tgt_bin SET notes = 'changed' WHERE id = 1")
        res_diff = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_bin",
            target_table="tgt_bin",
            primary_keys=["id"],
            mode="bucket_hash",
        )
        self.assertFalse(res_diff.passed)
        self.assertEqual(len(res_diff.mismatch_samples), 1)

    def test_subset_mode_bucket_hash_and_keyless(self):
        # Source has 3 rows, Target has 6 rows (all 3 source rows + 3 extra target rows)
        self.db.query(
            """
            CREATE TABLE src_sub (id INTEGER, val VARCHAR);
            CREATE TABLE tgt_sub (id INTEGER, val VARCHAR);
            INSERT INTO src_sub VALUES (1, 'a'), (2, 'b'), (3, 'c');
            INSERT INTO tgt_sub VALUES (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd'), (5, 'e'), (6, 'f');
            """
        )

        # 1. Exact mode fails due to extra target rows
        res_exact = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_sub",
            target_table="tgt_sub",
            primary_keys=["id"],
            mode="bucket_hash",
            match_scope="exact",
        )
        self.assertFalse(res_exact.passed)

        # 2. Subset mode (source_in_target) passes!
        res_sub_bh = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_sub",
            target_table="tgt_sub",
            primary_keys=["id"],
            mode="bucket_hash",
            match_scope="source_in_target",
        )
        self.assertTrue(res_sub_bh.passed)
        self.assertEqual(res_sub_bh.total_rows_compared, 3)
        self.assertGreaterEqual(res_sub_bh.extra_target_rows_count, 3)

        # 3. Subset mode in keyless passes!
        res_sub_kl = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_sub",
            target_table="tgt_sub",
            primary_keys=[],
            mode="keyless",
            match_scope="source_in_target",
        )
        self.assertTrue(res_sub_kl.passed)

        # 4. Corrupt 1 source row in target -> subset mode must fail and extract sample
        self.db.query("UPDATE tgt_sub SET val = 'CORRUPTED' WHERE id = 2")
        res_sub_fail = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_sub",
            target_table="tgt_sub",
            primary_keys=["id"],
            mode="bucket_hash",
            match_scope="source_in_target",
        )
        self.assertFalse(res_sub_fail.passed)
        self.assertEqual(len(res_sub_fail.mismatch_samples), 1)
        self.assertEqual(res_sub_fail.mismatch_samples[0].column_diffs["val"], ("b", "CORRUPTED"))

        # 5. Keyless mismatch sample extraction
        res_kl_fail = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_sub",
            target_table="tgt_sub",
            primary_keys=[],
            mode="keyless",
            match_scope="source_in_target",
        )
        self.assertFalse(res_kl_fail.passed)
        self.assertGreaterEqual(len(res_kl_fail.mismatch_samples), 1)


if __name__ == "__main__":
    unittest.main()
