import unittest
from unittest.mock import MagicMock

from data_diff.databases import Athena, MaxCompute, PostgreSQL
from data_diff.schema import RawColumnInfo
from validator.engine.schema_validator import SchemaValidator


class TestSchemaValidator(unittest.TestCase):
    def setUp(self):
        self.validator = SchemaValidator()

    def test_canonical_type_resolution(self):
        # Athena
        self.assertEqual(self.validator.resolve_canonical_type("Athena", "varchar")[0], "STRING")
        self.assertEqual(self.validator.resolve_canonical_type("Athena", "bigint")[0], "INT64")
        self.assertEqual(self.validator.resolve_canonical_type("Athena", "timestamp")[0], "TIMESTAMP")
        dec_type, p, s = self.validator.resolve_canonical_type("Athena", "decimal(12, 4)")
        self.assertEqual(dec_type, "DECIMAL")
        self.assertEqual(p, 12)
        self.assertEqual(s, 4)

        # MaxCompute
        self.assertEqual(self.validator.resolve_canonical_type("MaxCompute", "string")[0], "STRING")
        self.assertEqual(self.validator.resolve_canonical_type("MaxCompute", "bigint")[0], "INT64")
        self.assertEqual(self.validator.resolve_canonical_type("MaxCompute", "datetime")[0], "TIMESTAMP")

        # PostgreSQL
        self.assertEqual(self.validator.resolve_canonical_type("PostgreSQL", "text")[0], "STRING")
        self.assertEqual(self.validator.resolve_canonical_type("PostgreSQL", "int8")[0], "INT64")
        self.assertEqual(self.validator.resolve_canonical_type("PostgreSQL", "timestamptz")[0], "TIMESTAMP")

        # MSSQL
        self.assertEqual(self.validator.resolve_canonical_type("MsSQL", "nvarchar")[0], "STRING")
        self.assertEqual(self.validator.resolve_canonical_type("MsSQL", "datetime2")[0], "TIMESTAMP")
        self.assertEqual(self.validator.resolve_canonical_type("MsSQL", "bit")[0], "BOOLEAN")

    def _create_mock_db(self, name: str, schema_dict: dict) -> MagicMock:
        db = MagicMock()
        db.name = name
        raw_schema = {
            col: RawColumnInfo(
                column_name=col,
                data_type=info["type"],
                numeric_precision=info.get("precision"),
                numeric_scale=info.get("scale"),
            )
            for col, info in schema_dict.items()
        }
        db.query_table_schema.return_value = raw_schema
        return db

    def test_cross_dialect_schema_match(self):
        # Athena source vs MaxCompute target
        src_schema = {
            "order_id": {"type": "bigint"},
            "amount": {"type": "decimal(10,2)", "precision": 10, "scale": 2},
            "created_at": {"type": "timestamp"},
            "customer_name": {"type": "varchar"},
        }
        tgt_schema = {
            "order_id": {"type": "bigint"},
            "amount": {"type": "decimal(10,2)", "precision": 10, "scale": 2},
            "created_at": {"type": "datetime"},
            "customer_name": {"type": "string"},
        }

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "sales.orders", "sales.orders")
        self.assertTrue(res.passed)
        self.assertEqual(res.missing_columns_count, 0)
        self.assertEqual(res.extra_columns_count, 0)
        self.assertEqual(res.type_mismatch_count, 0)
        self.assertEqual(res.precision_loss_count, 0)

    def test_missing_column_in_target(self):
        src_schema = {
            "order_id": {"type": "bigint"},
            "deleted_col": {"type": "varchar"},
        }
        tgt_schema = {
            "order_id": {"type": "bigint"},
        }
        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertFalse(res.passed)
        self.assertEqual(res.missing_columns_count, 1)
        diff = next(d for d in res.column_diffs if d.column_name == "deleted_col")
        self.assertEqual(diff.diff_type, "MISSING_IN_TARGET")
        self.assertEqual(diff.severity, "ERROR")

    def test_extra_column_in_target(self):
        src_schema = {"order_id": {"type": "bigint"}}
        tgt_schema = {"order_id": {"type": "bigint"}, "audit_col": {"type": "varchar"}}

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        # By default extra columns fail with ERROR
        res_default = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertFalse(res_default.passed)
        self.assertEqual(res_default.extra_columns_count, 1)

        # When allowed, extra columns trigger WARN but pass
        res_allowed = self.validator.validate(src_db, tgt_db, "src", "tgt", allow_extra_target_columns=True)
        self.assertTrue(res_allowed.passed)
        diff = next(d for d in res_allowed.column_diffs if d.column_name == "audit_col")
        self.assertEqual(diff.severity, "WARN")

    def test_type_narrowing_fails(self):
        # Source has bigint (INT64), target has int (INT32) -> data truncation risk
        src_schema = {"order_id": {"type": "bigint"}}
        tgt_schema = {"order_id": {"type": "int"}}

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertFalse(res.passed)
        self.assertEqual(res.type_mismatch_count, 1)
        diff = res.column_diffs[0]
        self.assertEqual(diff.diff_type, "TYPE_MISMATCH")
        self.assertEqual(diff.severity, "ERROR")

    def test_type_widening_warns_and_passes(self):
        # Source has int (INT32), target has bigint (INT64) -> safe widening
        src_schema = {"order_id": {"type": "int"}}
        tgt_schema = {"order_id": {"type": "bigint"}}

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertTrue(res.passed)
        diff = res.column_diffs[0]
        self.assertEqual(diff.diff_type, "TYPE_WIDENING")
        self.assertEqual(diff.severity, "WARN")

    def test_decimal_precision_scale_truncation_fails(self):
        # Source decimal(10,4) -> Target decimal(10,2) (scale truncation)
        src_schema = {"amount": {"type": "decimal(10,4)", "precision": 10, "scale": 4}}
        tgt_schema = {"amount": {"type": "decimal(10,2)", "precision": 10, "scale": 2}}

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertFalse(res.passed)
        self.assertEqual(res.precision_loss_count, 1)
        diff = res.column_diffs[0]
        self.assertEqual(diff.diff_type, "PRECISION_TRUNCATION")
        self.assertEqual(diff.severity, "ERROR")

    def test_decimal_precision_widening_passes(self):
        # Source decimal(10,2) -> Target decimal(12,2)
        src_schema = {"amount": {"type": "decimal(10,2)", "precision": 10, "scale": 2}}
        tgt_schema = {"amount": {"type": "decimal(12,2)", "precision": 12, "scale": 2}}

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(src_db, tgt_db, "src", "tgt")
        self.assertTrue(res.passed)
        diff = res.column_diffs[0]
        self.assertEqual(diff.diff_type, "PRECISION_WIDENING")
        self.assertEqual(diff.severity, "WARN")

    def test_column_exclusions_and_renames(self):
        src_schema = {
            "order_id": {"type": "bigint"},
            "order_amt": {"type": "decimal(10,2)", "precision": 10, "scale": 2},
            "etl_loaded_at": {"type": "timestamp"},
        }
        tgt_schema = {
            "order_id": {"type": "bigint"},
            "order_amount": {"type": "decimal(10,2)", "precision": 10, "scale": 2},
            "target_internal": {"type": "varchar"},
        }

        src_db = self._create_mock_db("Athena", src_schema)
        tgt_db = self._create_mock_db("MaxCompute", tgt_schema)

        res = self.validator.validate(
            src_db,
            tgt_db,
            "src",
            "tgt",
            exclude_columns=["etl_loaded_at", "target_internal"],
            column_mapping={"order_amt": "order_amount"},
        )
        self.assertTrue(res.passed)
        self.assertEqual(len(res.column_diffs), 2)  # order_id, order_amount
        amt_diff = next(d for d in res.column_diffs if d.column_name == "order_amount")
        self.assertEqual(amt_diff.source_column_name, "order_amt")
        self.assertEqual(amt_diff.diff_type, "MATCH")


if __name__ == "__main__":
    unittest.main()
