import unittest

from data_diff import connect
from tests.common import TEST_DUCKDB_CONN_STRING
from validator.engine.count_validator import CountValidator


class TestCountValidator(unittest.TestCase):
    def setUp(self):
        self.db = connect(TEST_DUCKDB_CONN_STRING)
        self.validator = CountValidator(max_concurrency=1)

        # Create source and target tables
        self.db.query("DROP TABLE IF EXISTS src_orders")
        self.db.query("DROP TABLE IF EXISTS tgt_orders")

        self.db.query(
            """
            CREATE TABLE src_orders (
                order_id INT,
                dt VARCHAR,
                amount DOUBLE,
                notes VARCHAR
            )
            """
        )
        self.db.query(
            """
            CREATE TABLE tgt_orders (
                order_id INT,
                dt VARCHAR,
                amount DOUBLE,
                notes VARCHAR
            )
            """
        )

        # Insert 10 rows into src
        self.db.query(
            """
            INSERT INTO src_orders VALUES
                (1, '2026-10-01', 10.5, 'first'),
                (2, '2026-10-01', 20.0, NULL),
                (3, '2026-10-01', 30.0, 'third'),
                (4, '2026-10-02', 40.0, 'fourth'),
                (5, '2026-10-02', 50.0, NULL),
                (6, '2026-10-02', 60.0, 'sixth'),
                (7, '2026-10-03', 70.0, 'seventh'),
                (8, '2026-10-03', 80.0, 'eighth'),
                (9, '2026-10-03', 90.0, 'ninth'),
                (10, '2026-10-03', 100.0, 'tenth')
            """
        )

        # Insert identical 10 rows into tgt
        self.db.query("INSERT INTO tgt_orders SELECT * FROM src_orders")

    def tearDown(self):
        self.db.query("DROP TABLE IF EXISTS src_orders")
        self.db.query("DROP TABLE IF EXISTS tgt_orders")

    def test_exact_count_match(self):
        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
        )
        self.assertTrue(res.passed)
        self.assertEqual(res.source_total_count, 10)
        self.assertEqual(res.target_total_count, 10)
        self.assertEqual(res.count_diff, 0)
        self.assertEqual(res.count_diff_pct, 0.0)

    def test_count_mismatch_detected(self):
        # Delete 1 row from target
        self.db.query("DELETE FROM tgt_orders WHERE order_id = 10")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            threshold_pct=0.0,
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.source_total_count, 10)
        self.assertEqual(res.target_total_count, 9)
        self.assertEqual(res.count_diff, -1)
        self.assertAlmostEqual(res.count_diff_pct, 10.0, places=2)

    def test_threshold_tolerance(self):
        # Delete 1 row from target (10% diff)
        self.db.query("DELETE FROM tgt_orders WHERE order_id = 10")

        # Threshold 15% -> passes
        res_pass = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            threshold_pct=15.0,
        )
        self.assertTrue(res_pass.passed)

        # Threshold 5% -> fails
        res_fail = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            threshold_pct=5.0,
        )
        self.assertFalse(res_fail.passed)

    def test_partition_level_breakdown(self):
        # Modify partitions: delete from 2026-10-02, add new partition 2026-10-04 on target
        self.db.query("DELETE FROM tgt_orders WHERE dt = '2026-10-02'")
        self.db.query("INSERT INTO tgt_orders VALUES (11, '2026-10-04', 110.0, 'extra')")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            partition_column="dt",
        )
        self.assertFalse(res.passed)
        part_map = {p.partition_key: p for p in res.partition_diffs}

        self.assertEqual(part_map["2026-10-01"].status, "MATCH")
        self.assertEqual(part_map["2026-10-03"].status, "MATCH")
        self.assertEqual(part_map["2026-10-02"].status, "MISSING_IN_TARGET")
        self.assertEqual(part_map["2026-10-04"].status, "MISSING_IN_SOURCE")

    def test_column_profiles(self):
        # Alter null values on target for notes column
        self.db.query("UPDATE tgt_orders SET notes = 'filled' WHERE order_id = 2")

        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            profile_columns=["amount", "notes"],
        )
        prof_map = {p.column_name: p for p in res.column_profiles}

        self.assertEqual(prof_map["amount"].status, "MATCH")
        self.assertEqual(prof_map["notes"].status, "MISMATCH")
        self.assertEqual(prof_map["notes"].source_null_count, 2)
        self.assertEqual(prof_map["notes"].target_null_count, 1)
        self.assertEqual(prof_map["notes"].null_diff, -1)

    def test_where_clause_filter(self):
        res = self.validator.validate(
            source_db=self.db,
            target_db=self.db,
            source_table="src_orders",
            target_table="tgt_orders",
            source_where="dt = '2026-10-01'",
            target_where="dt = '2026-10-01'",
        )
        self.assertTrue(res.passed)
        self.assertEqual(res.source_total_count, 3)
        self.assertEqual(res.target_total_count, 3)


if __name__ == "__main__":
    unittest.main()
