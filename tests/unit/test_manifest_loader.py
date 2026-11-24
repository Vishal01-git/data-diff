import os
import tempfile
import unittest

from validator.config.connections import interpolate_env_vars, load_manifest
from validator.config.manifest import SITManifest


class TestManifestLoader(unittest.TestCase):
    def test_env_var_interpolation(self):
        os.environ["TEST_ENV_VAR"] = "my_custom_region"
        res = interpolate_env_vars("region is ${TEST_ENV_VAR}")
        self.assertEqual(res, "region is my_custom_region")

        res_default = interpolate_env_vars("region is ${UNSET_VAR:-us-west-2}")
        self.assertEqual(res_default, "region is us-west-2")

        with self.assertRaises(ValueError) as ctx:
            interpolate_env_vars("${REQUIRED_BUT_MISSING_VAR}")
        self.assertIn("REQUIRED_BUT_MISSING_VAR", str(ctx.exception))

    def test_load_valid_manifest(self):
        yaml_content = """
version: "1.0"
run_name: "test_sit_run"

connections:
  src_db:
    type: duckdb
    database: ":memory:"
    max_concurrency: 2

  tgt_db:
    type: duckdb
    database: ":memory:"
    max_concurrency: 2

defaults:
  tiers: ["count", "schema", "data"]
  fail_fast:
    skip_data_on_schema_fail: true
    skip_data_if_count_diff_pct_gt: 50.0

tables:
  - name: "orders"
    source:
      connection: src_db
      table: "sales.orders"
    target:
      connection: tgt_db
      table: "sales.orders"
    primary_keys: ["id"]
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            manifest = load_manifest(temp_path)
            self.assertIsInstance(manifest, SITManifest)
            self.assertEqual(manifest.run_name, "test_sit_run")
            self.assertIn("src_db", manifest.connections)
            self.assertEqual(len(manifest.tables), 1)
            self.assertEqual(manifest.tables[0].name, "orders")
        finally:
            os.remove(temp_path)

    def test_undefined_connection_reference_fails(self):
        yaml_content = """
version: "1.0"
run_name: "bad_sit_run"

connections:
  src_db:
    type: duckdb

tables:
  - name: "orders"
    source:
      connection: src_db
      table: "sales.orders"
    target:
      connection: missing_target_db
      table: "sales.orders"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            with self.assertRaises(Exception):
                load_manifest(temp_path)
        finally:
            os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
