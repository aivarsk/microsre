import time
import unittest

from microsre import procfs


class TestProcfsSampling(unittest.TestCase):
    def test_burst_and_merge_take_max(self):
        merge, ready = procfs.sample_procfs()
        self.assertIsNone(ready.result(timeout=5))

        raw = {"proc.stat": {}}
        merge(raw)
        self.assertGreaterEqual(raw["proc.stat"]["procs_running"], 0)

        merge, ready = procfs.sample_procfs()
        ready.result(timeout=5)
        raw = {"proc.stat": {"procs_running": 10**9}}
        merge(raw)
        self.assertLess(raw["proc.stat"]["procs_running"], 1000)

    def test_burst_samples_in_background(self):
        merge, ready = procfs.sample_procfs()
        time.sleep(0.6)
        self.assertTrue(ready.done())
        merge({"proc.stat": {}})

    def test_collect_procfs_reports_procs_running_after_merge(self):
        raw = procfs.collect_procfs(time.time())
        merge, ready = procfs.sample_procfs()
        ready.result(timeout=5)
        merge(raw)
        self.assertGreaterEqual(raw["proc.stat"]["procs_running"], 0)


if __name__ == "__main__":
    unittest.main()
