import unittest
from unittest.mock import MagicMock, patch

import dsnparse

from data_diff.abcs.database_types import Boolean, Decimal, TemporalType, Timestamp, TimestampTZ
from data_diff.databases import Athena, connect
from data_diff.databases._connect import DATABASE_BY_SCHEME, MatchUriPath
from data_diff.databases.athena import Dialect as AthenaDialect
from data_diff.schema import RawColumnInfo


class TestAthenaDialect(unittest.TestCase):
    def setUp(self):
        self.dialect = AthenaDialect()

    def test_uri_scheme_registration(self):
        self.assertIn("athena", DATABASE_BY_SCHEME)
        self.assertIs(DATABASE_BY_SCHEME["athena"], Athena)

    def test_uri_parsing(self):
        uri = (
            "athena://AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY@"
            "athena.us-east-1.amazonaws.com/AwsDataCatalog/sales_db"
            "?s3_staging_dir=s3://my-bucket/staging&work_group=primary"
        )
        dsn = dsnparse.parse(uri)
        matcher = MatchUriPath(Athena)
        matches = matcher.match_path(dsn)

        self.assertEqual(matches["catalog"], "AwsDataCatalog")
        self.assertEqual(matches["schema"], "sales_db")
        self.assertEqual(matches["s3_staging_dir"], "s3://my-bucket/staging")
        self.assertEqual(matches["work_group"], "primary")

    def test_quoting(self):
        self.assertEqual(self.dialect.quote("order_id"), '"order_id"')
        self.assertEqual(self.dialect.quote("col with space"), '"col with space"')

    def test_cast_to_string(self):
        self.assertEqual(self.dialect.to_string("order_id"), "cast(order_id as varchar)")

    def test_md5_as_int_sql(self):
        sql = self.dialect.md5_as_int("order_id")
        self.assertEqual(
            sql,
            "from_base(substr(to_hex(md5(to_utf8(cast(order_id as varchar)))), 1, 15), 16)",
        )

    def test_md5_as_hex_sql(self):
        sql = self.dialect.md5_as_hex("order_id")
        self.assertEqual(sql, "to_hex(md5(to_utf8(cast(order_id as varchar))))")

    def test_timestamp_formatting(self):
        ts_prec6 = Timestamp(precision=6)
        formatted_prec6 = self.dialect.normalize_timestamp("created_at", ts_prec6)
        self.assertIn("date_format(cast(created_at as timestamp(6)), '%Y-%m-%d %H:%i:%S.%f')", formatted_prec6)
        self.assertIn("RPAD", formatted_prec6)

        ts_prec0 = Timestamp(precision=0)
        formatted_prec0 = self.dialect.normalize_timestamp("created_at", ts_prec0)
        self.assertEqual(formatted_prec0, "date_format(cast(created_at as timestamp), '%Y-%m-%d %H:%i:%S')")

    def test_decimal_formatting(self):
        dec_type = Decimal(precision=2)
        formatted = self.dialect.normalize_number("amount", dec_type)
        self.assertEqual(formatted, "cast(cast(amount as decimal(38,2)) as varchar)")

    def test_boolean_formatting(self):
        formatted = self.dialect.normalize_boolean("is_active", Boolean())
        self.assertEqual(formatted, "cast(case when is_active then 1 else 0 end as varchar)")

    def test_parse_type(self):
        raw_ts = RawColumnInfo(column_name="created_at", data_type="timestamp(3)", datetime_precision=3)
        parsed_ts = self.dialect.parse_type(("sales_db", "orders"), raw_ts)
        self.assertIsInstance(parsed_ts, Timestamp)
        self.assertEqual(parsed_ts.precision, 3)

        raw_dec = RawColumnInfo(column_name="amount", data_type="decimal(12,4)", numeric_precision=12, numeric_scale=4)
        parsed_dec = self.dialect.parse_type(("sales_db", "orders"), raw_dec)
        self.assertIsInstance(parsed_dec, Decimal)
        self.assertEqual(parsed_dec.precision, 4)

    def test_select_table_schema_sql(self):
        db = Athena.__new__(Athena)
        db.default_schema = "sales_db"
        sql = db.select_table_schema(("orders",))
        self.assertIn("FROM information_schema.columns", sql)
        self.assertIn("WHERE table_name = 'orders' AND table_schema = 'sales_db'", sql)

    @patch("data_diff.databases.athena.import_boto3")
    def test_glue_schema_introspection(self, mock_import_boto3):
        mock_glue = MagicMock()
        mock_import_boto3.return_value.client.return_value = mock_glue
        mock_glue.get_table.return_value = {
            "Table": {
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "order_id", "Type": "bigint"},
                        {"Name": "amount", "Type": "decimal(10,2)"},
                        {"Name": "created_at", "Type": "timestamp"},
                    ]
                },
                "PartitionKeys": [{"Name": "dt", "Type": "string"}],
            }
        }

        db = Athena.__new__(Athena)
        db.default_schema = "sales_db"
        db.default_catalog = "AwsDataCatalog"
        db.region_name = "us-east-1"
        db.use_glue_schema = True

        schema = db.query_table_schema(("sales_db", "orders"))
        self.assertIn("order_id", schema)
        self.assertIn("amount", schema)
        self.assertIn("created_at", schema)
        self.assertIn("dt", schema)

        self.assertEqual(schema["order_id"].data_type, "bigint")
        self.assertEqual(schema["amount"].data_type, "decimal(10,2)")
        self.assertEqual(schema["amount"].numeric_precision, 10)
        self.assertEqual(schema["amount"].numeric_scale, 2)
        self.assertEqual(schema["created_at"].datetime_precision, 3)
        self.assertEqual(schema["dt"].data_type, "string")

    def test_query_stats_capture(self):
        db = Athena.__new__(Athena)
        mock_cursor = MagicMock()
        mock_cursor.data_scanned_in_bytes = 1048576
        mock_cursor.query_id = "test-query-id-123"
        mock_cursor.execution_time_in_millis = 450
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [(1,)]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        db._conn = mock_conn

        res = db._query("SELECT 1")
        self.assertEqual(res, [(1,)])
        self.assertEqual(db.last_query_stats["data_scanned_in_bytes"], 1048576)
        self.assertEqual(db.last_query_stats["query_id"], "test-query-id-123")
        self.assertEqual(db.last_query_stats["execution_time_in_millis"], 450)


if __name__ == "__main__":
    unittest.main()
