import unittest

from data_diff.abcs.database_types import Boolean, Decimal, TimestampTZ
from data_diff.databases import PostgreSQL
from data_diff.databases._connect import DATABASE_BY_SCHEME
from data_diff.databases.postgresql import PostgresqlDialect


class TestPostgreSQLDialect(unittest.TestCase):
    def setUp(self):
        self.dialect = PostgresqlDialect()

    def test_uri_scheme_registration(self):
        self.assertIn("postgresql", DATABASE_BY_SCHEME)
        self.assertIs(DATABASE_BY_SCHEME["postgresql"], PostgreSQL)

    def test_quoting(self):
        self.assertEqual(self.dialect.quote("order_id"), '"order_id"')

    def test_cast_to_string(self):
        self.assertEqual(self.dialect.to_string("order_id"), "order_id::varchar")

    def test_md5_as_int_sql(self):
        sql = self.dialect.md5_as_int("order_id")
        self.assertIn("md5(order_id)", sql)
        self.assertIn("::bit(48)::bigint", sql)

    def test_timestamp_formatting(self):
        tstz = TimestampTZ(precision=6)
        formatted = self.dialect.normalize_timestamp("created_at", tstz)
        self.assertIn("to_char", formatted)
        self.assertIn("YYYY-mm-dd HH24:MI:SS.US", formatted)

    def test_decimal_formatting(self):
        dec_type = Decimal(precision=2)
        formatted = self.dialect.normalize_number("amount", dec_type)
        self.assertEqual(formatted, "amount::decimal(38, 2)::varchar")

    def test_select_table_schema_sql(self):
        db = PostgreSQL.__new__(PostgreSQL)
        db.default_schema = "public"
        db._dialect = self.dialect
        sql = db.select_table_schema(("orders",))
        self.assertIn("FROM information_schema.columns", sql)
        self.assertIn("WHERE table_name = 'orders' AND table_schema = 'public'", sql)


if __name__ == "__main__":
    unittest.main()
