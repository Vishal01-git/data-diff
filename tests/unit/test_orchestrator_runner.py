import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from validator.config.manifest import (
    ConnectionConfig,
    DefaultsConfig,
    FailFastConfig,
    SITManifest,
    TableConfig,
    TableEndpointConfig,
)
from validator.engine.count_validator import CountValidationResult
from validator.engine.schema_validator import SchemaValidationResult
from validator.orchestrator.checkpoint import CheckpointTracker
from validator.orchestrator.runner import BatchRunner, retry_with_backoff


class TestOrchestratorRunner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.checkpoint = CheckpointTracker("run_unit_test", state_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_retry_with_backoff_success(self):
        calls = 0

        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ValueError("Temporary failure")
            return "SUCCESS"

        res = retry_with_backoff(flaky, max_retries=3, base_delay=0.01, backoff_factor=1.0)
        self.assertEqual(res, "SUCCESS")
        self.assertEqual(calls, 3)

    def test_retry_with_backoff_exhausted(self):
        def always_fail():
            raise RuntimeError("Permanent failure")

        with self.assertRaises(RuntimeError):
            retry_with_backoff(always_fail, max_retries=2, base_delay=0.01)

    def test_fail_fast_on_schema_error(self):
        # Configure manifest with skip_data_on_schema_fail = True
        manifest = SITManifest(
            run_name="test_fail_fast",
            connections={
                "c1": ConnectionConfig(type="duckdb", database=":memory:"),
                "c2": ConnectionConfig(type="duckdb", database=":memory:"),
            },
            defaults=DefaultsConfig(
                tiers=["count", "schema", "data"],
                fail_fast=FailFastConfig(skip_data_on_schema_fail=True),
            ),
            tables=[
                TableConfig(
                    name="orders",
                    source=TableEndpointConfig(connection="c1", table="orders"),
                    target=TableEndpointConfig(connection="c2", table="orders"),
                    primary_keys=["id"],
                )
            ],
        )

        mock_resolver = MagicMock()
        runner = BatchRunner(manifest, resolver=mock_resolver, checkpoint_tracker=self.checkpoint)

        # Mock Count to pass, but Schema to fail
        runner.count_validator.validate = MagicMock(
            return_value=CountValidationResult(
                source_table="orders",
                target_table="orders",
                source_total_count=100,
                target_total_count=100,
                passed=True,
            )
        )
        runner.schema_validator.validate = MagicMock(
            return_value=SchemaValidationResult(
                source_table="orders",
                target_table="orders",
                passed=False,
                missing_columns_count=1,
            )
        )
        runner.data_validator.validate = MagicMock()

        result = runner.run()
        self.assertEqual(result.overall_status, "FAILED")
        self.assertEqual(len(result.table_results), 1)
        tbl_res = result.table_results[0]
        self.assertFalse(tbl_res.passed)

        # Assert data_validator was SKIPPED due to fail-fast
        runner.data_validator.validate.assert_not_called()
        self.assertIsNone(tbl_res.data_result)


if __name__ == "__main__":
    unittest.main()
