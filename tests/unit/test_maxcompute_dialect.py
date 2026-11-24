import unittest
from unittest.mock import MagicMock

import dsnparse

from data_diff.abcs.database_types import Boolean, Decimal, Timestamp
from data_diff.databases import MaxCompute
from data_diff.databases._connect import DATABASE_BY_SCHEME, MatchUriPath
from data_diff.databases.maxcompute import Dialect as MaxComputeDialect
from data_diff.schema import RawColumnInfo


class TestMaxComputeDialect(unittest.TestCase):
    def setUp(self):
        self.dialect = MaxComputeDialect()

    def test_uri_scheme_registration(self):
        self.assertIn("odps", DATABASE_BY_SCHEME)
        self.assertIn("maxcompute", DATABASE_BY_SCHEME)
        self.assertIs(DATABASE_BY_SCHEME["odps"], MaxCompute)
        self.assertIs(DATABASE_BY_SCHEME["maxcompute"], MaxCompute)

    def test_uri_parsing(self):
        uri = (
            "odps://MY_ACCESS_ID:MY_SECRET_KEY@"
            "service.cn-hangzhou.maxcompute.aliyun.com/my_project"
            "?allow_fullscan=true"
        )
        dsn = dsnparse.parse(uri)
        matcher = MatchUriPath(MaxCompute)
        matches = matcher.match_path(dsn)

        self.assertEqual(matches["project"], "my_project")
        self.assertEqual(matches["allow_fullscan"], "true")

    def test_quoting(self):
        self.assertEqual(self.dialect.quote("order_id"), "`order_id`")
        self.assertEqual(self.dialect.quote("col name"), "`col name`")

    def test_cast_to_string(self):
        self.assertEqual(self.dialect.to_string("order_id"), "cast(order_id as string)")

    def test_md5_as_int_sql(self):
        sql = self.dialect.md5_as_int("order_id")
        self.assertEqual(
            sql,
            "cast(conv(substr(md5(cast(order_id as string)), 1, 15), 16, 10) as decimal(38, 0))",
        )

    def test_md5_as_hex_sql(self):
        sql = self.dialect.md5_as_hex("order_id")
        self.assertEqual(sql, "md5(cast(order_id as string))")

    def test_timestamp_formatting(self):
        ts = Timestamp(precision=0)
        formatted = self.dialect.normalize_timestamp("created_at", ts)
        self.assertEqual(formatted, "to_char(created_at, 'yyyy-mm-dd hh:mi:ss')")

    def test_decimal_formatting(self):
        dec_type = Decimal(precision=4)
        formatted = self.dialect.normalize_number("amount", dec_type)
        self.assertEqual(formatted, "cast(cast(amount as decimal(38, 4)) as string)")

    def test_boolean_formatting(self):
        formatted = self.dialect.normalize_boolean("is_active", Boolean())
        self.assertEqual(formatted, "cast(case when is_active then 1 else 0 end as string)")

    def test_timezone_setting(self):
        self.assertEqual(self.dialect.set_timezone_to_utc(), "SET odps.sql.timezone=UTC")

    def test_select_table_schema_sql(self):
        db = MaxCompute.__new__(MaxCompute)
        db.project = "my_project"
        sql = db.select_table_schema(("orders",))
        self.assertEqual(sql, "DESCRIBE `my_project`.`orders`")

    def test_odps_metadata_schema_introspection(self):
        db = MaxCompute.__new__(MaxCompute)
        db.project = "my_project"

        mock_col1 = MagicMock()
        mock_col1.name = "order_id"
        mock_col1.type.name = "BIGINT"
        mock_col1.type.precision = None
        mock_col1.type.scale = None

        mock_col2 = MagicMock()
        mock_col2.name = "amount"
        mock_col2.type.name = "DECIMAL"
        mock_col2.type.precision = 18
        mock_col2.type.scale = 2

        mock_part = MagicMock()
        mock_part.name = "dt"
        mock_part.type.name = "STRING"

        mock_table = MagicMock()
        mock_table.table_schema.columns = [mock_col1, mock_col2]
        mock_table.table_schema.partitions = [mock_part]

        mock_odps = MagicMock()
        mock_odps.get_table.return_value = mock_table
        db._odps = mock_odps

        schema = db.query_table_schema(("my_project", "orders"))
        self.assertIn("order_id", schema)
        self.assertIn("amount", schema)
        self.assertIn("dt", schema)

        self.assertEqual(schema["order_id"].data_type, "bigint")
        self.assertEqual(schema["amount"].data_type, "decimal")
        self.assertEqual(schema["amount"].numeric_precision, 18)
        self.assertEqual(schema["amount"].numeric_scale, 2)
        self.assertEqual(schema["dt"].data_type, "string")


if __name__ == "__main__":
    unittest.main()
