import shutil
import tempfile
import unittest

from validator.orchestrator.checkpoint import CheckpointTracker


class TestCheckpointTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tracker = CheckpointTracker("test_run_01", state_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_retrieve_checkpoint(self):
        self.assertFalse(self.tracker.is_completed_and_passed("orders"))

        result_data = {"table_name": "orders", "passed": True, "count": 100}
        self.tracker.save_checkpoint("orders", "PASSED", True, result_data)

        self.assertTrue(self.tracker.is_completed_and_passed("orders"))

        retrieved = self.tracker.get_table_result("orders")
        self.assertEqual(retrieved["table_name"], "orders")
        self.assertTrue(retrieved["passed"])
        self.assertEqual(retrieved["count"], 100)

    def test_failed_checkpoint(self):
        self.tracker.save_checkpoint("orders", "FAILED", False, {"table_name": "orders", "passed": False})
        # is_completed_and_passed should be False because passed is False
        self.assertFalse(self.tracker.is_completed_and_passed("orders"))

    def test_get_all_completed_and_clear(self):
        self.tracker.save_checkpoint("t1", "PASSED", True, {"name": "t1"})
        self.tracker.save_checkpoint("t2", "FAILED", False, {"name": "t2"})

        all_completed = self.tracker.get_all_completed()
        self.assertEqual(len(all_completed), 2)
        self.assertTrue(all_completed["t1"][1])
        self.assertFalse(all_completed["t2"][1])

        self.tracker.clear()
        self.assertEqual(len(self.tracker.get_all_completed()), 0)


if __name__ == "__main__":
    unittest.main()
