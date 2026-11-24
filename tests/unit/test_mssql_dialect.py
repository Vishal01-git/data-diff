import unittest

import dsnparse

from data_diff.abcs.database_types import Boolean, Decimal, Timestamp
from data_diff.databases import MsSQL
from data_diff.databases._connect import DATABASE_BY_SCHEME, MatchUriPath
from data_diff.databases.mssql import Dialect as MsSQLDialect


class TestMsSQLDialect(unittest.TestCase):
    def setUp(self):
        self.dialect = MsSQLDialect()

    def test_uri_scheme_registration(self):
        self.assertIn("mssql", DATABASE_BY_SCHEME)
        self.assertIn("sqlserver", DATABASE_BY_SCHEME)
        self.assertIs(DATABASE_BY_SCHEME["mssql"], MsSQL)
        self.assertIs(DATABASE_BY_SCHEME["sqlserver"], MsSQL)

    def test_uri_parsing(self):
        uri = "mssql://sa:SecretPass123@localhost:1433/sales_db/dbo"
        dsn = dsnparse.parse(uri)
        matcher = MatchUriPath(MsSQL)
        matches = matcher.match_path(dsn)

        self.assertEqual(matches["database"], "sales_db")
        self.assertEqual(matches["schema"], "dbo")

    def test_quoting(self):
        self.assertEqual(self.dialect.quote("order_id"), "[order_id]")
        self.assertEqual(self.dialect.quote("table name"), "[table name]")

    def test_cast_to_string(self):
        self.assertEqual(self.dialect.to_string("order_id"), "CONVERT(VARCHAR(MAX), order_id)")

    def test_md5_as_int_sql(self):
        sql = self.dialect.md5_as_int("order_id")
        self.assertIn("HashBytes('MD5', CAST(order_id AS VARCHAR(MAX)))", sql)
        self.assertIn("convert(bigint", sql)
        self.assertIn("convert(varbinary", sql)

    def test_md5_as_hex_sql(self):
        sql = self.dialect.md5_as_hex("order_id")
        self.assertEqual(sql, "CONVERT(NVARCHAR(32), HashBytes('MD5', CAST(order_id AS VARCHAR(MAX))), 2)")

    def test_timestamp_formatting(self):
        ts_prec3 = Timestamp(precision=3)
        formatted = self.dialect.normalize_timestamp("created_at", ts_prec3)
        self.assertIn("FORMAT(created_at, 'yyyy-MM-dd HH:mm:ss')", formatted)
        self.assertIn("SUBSTRING(FORMAT(created_at, 'fffffff'), 1, 3)", formatted)

        ts_prec0 = Timestamp(precision=0)
        formatted0 = self.dialect.normalize_timestamp("created_at", ts_prec0)
        self.assertEqual(formatted0, "FORMAT(created_at, 'yyyy-MM-dd HH:mm:ss')")

    def test_decimal_formatting(self):
        dec_type = Decimal(precision=2)
        formatted = self.dialect.normalize_number("amount", dec_type)
        self.assertEqual(formatted, "FORMAT(amount, 'N2')")

    def test_boolean_formatting(self):
        formatted = self.dialect.normalize_boolean("is_active", Boolean())
        self.assertEqual(formatted, "CASE WHEN is_active = 1 THEN '1' ELSE '0' END")

    def test_select_table_schema_sql(self):
        db = MsSQL.__new__(MsSQL)
        db.default_database = "sales_db"
        db.default_schema = "dbo"
        db._dialect = self.dialect
        sql = db.select_table_schema(("orders",))
        self.assertIn("FROM [sales_db].information_schema.columns", sql)
        self.assertIn("WHERE table_name = 'orders' AND table_schema = 'dbo'", sql)


if __name__ == "__main__":
    unittest.main()
