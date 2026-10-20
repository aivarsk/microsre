import unittest

from testutil import isolate_xdg

from microsre.burst import BurstSampler
from microsre.core import BackgroundWorker
from microsre.metrics import calculate_burst_metrics


def setUpModule():
    isolate_xdg()


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def counters(cpu=(0,) * 8, cores=None, ctxt=0, running=0, blocked=0, vmstat=None):
    lines = {"cpu0": cpu}
    if cores:
        lines.update(cores)
    stat = {name: dict(zip(CPU_FIELDS, fields)) for name, fields in lines.items()}
    stat["ctxt"] = ctxt
    stat["procs_running"] = running
    stat["procs_blocked"] = blocked
    return {
        "stat": stat,
        "diskstats": {},
        "net.dev": {},
        "net.snmp": {},
        "net.netstat": {},
        "vmstat": vmstat or {},
    }


class TestWorkerTimers(unittest.TestCase):
    def test_due_timer_runs_once_and_advances_by_one_interval(self):
        worker = BackgroundWorker()
        try:
            calls = []
            worker._timers.append([10.0, 1.0, calls.append])
            worker._run_timers(now=10.5)
            self.assertEqual(calls, [10.5])
            self.assertEqual(worker._timers[0][0], 11.0)
            worker._run_timers(now=10.9)
            self.assertEqual(len(calls), 1)
        finally:
            worker.shutdown()

    def test_missed_ticks_are_skipped_not_backfilled(self):
        worker = BackgroundWorker()
        try:
            calls = []
            worker._timers.append([10.0, 1.0, calls.append])
            worker._run_timers(now=25.0)
            self.assertEqual(calls, [25.0])
            self.assertEqual(worker._timers[0][0], 26.0)
        finally:
            worker.shutdown()


class TestSamplerLifecycle(unittest.TestCase):
    def sampler(self, readings, interval=60):
        queue = iter(readings)
        return BurstSampler(interval, read=lambda: next(queue))

    def test_first_tick_seeds_without_pushing_a_sample(self):
        sampler = self.sampler([counters()])
        sampler._tick(0)
        drained = sampler.drain()
        self.assertEqual(drained["burst.samples"], 0)
        self.assertNotIn("burst.cpu.max_utilization_pct", drained)

    def test_second_tick_pushes_per_second_sample(self):
        sampler = self.sampler([counters(), counters(cpu=(50, 0, 0, 50, 0, 0, 0, 0), running=4)])
        sampler._tick(0)
        sampler._tick(1.0)
        drained = sampler.drain()
        self.assertEqual(drained["burst.samples"], 1)
        self.assertEqual(drained["burst.cpu.max_utilization_pct"], 50)
        self.assertEqual(drained["burst.cpu.max_procs_running"], 3)

    def test_drain_reports_core_of_the_hottest_second(self):
        sampler = self.sampler(
            [
                counters(cores={"cpu1": (0,) * 8}),
                counters(cores={"cpu0": (80, 0, 0, 20, 0, 0, 0, 0), "cpu1": (0,) * 8}),
                counters(cores={"cpu0": (90, 0, 0, 30, 0, 0, 0, 0), "cpu1": (95, 0, 0, 5, 0, 0, 0, 0)}),
            ]
        )
        sampler._tick(0)
        sampler._tick(1.0)
        sampler._tick(2.0)
        drained = sampler.drain()
        self.assertEqual(drained["burst.cpu.max_utilization_pct"], 95)
        self.assertEqual(drained["burst.cpu.max_utilization_core"], "cpu1")

    def test_gap_drops_the_spanning_sample(self):
        sampler = self.sampler([counters(), counters(ctxt=30_000), counters(ctxt=30_100, running=2)])
        sampler._tick(0)
        sampler._tick(30.0)
        sampler._tick(31.0)
        drained = sampler.drain()
        self.assertEqual(drained["burst.samples"], 1)
        self.assertAlmostEqual(drained["burst.cpu.max_ctxt_per_sec"], 100)

    def test_drain_reports_max_over_window_and_dropped_count(self):
        readings = (counters(ctxt=step * 100) for step in range(31))
        sampler = BurstSampler(5, read=lambda: next(readings))
        for step in range(31):
            sampler._tick(float(step))
        drained = sampler.drain()
        self.assertEqual(drained["burst.samples"], 20)
        self.assertEqual(drained["burst.dropped"], 10)
        self.assertAlmostEqual(drained["burst.cpu.max_ctxt_per_sec"], 100)

    def test_empty_window_reports_no_burst_keys(self):
        sampler = self.sampler([counters()])
        drained = sampler.drain()
        self.assertEqual(drained["burst.samples"], 0)
        self.assertFalse(
            any(key.startswith("burst.") and key not in ("burst.samples", "burst.dropped") for key in drained)
        )


class TestSampleMath(unittest.TestCase):
    def test_regressed_field_is_clamped_per_field(self):
        start = counters(cpu=(100, 0, 100, 700, 0, 0, 0, 0))
        end = counters(cpu=(150, 0, 50, 900, 0, 0, 0, 0))
        sample = calculate_burst_metrics(start, end, 60)
        self.assertEqual(sample["cpu.max_utilization_pct"], 20)

    def test_tcp_retransmit_burst_survives_as_per_second_rate(self):
        start = {
            "stat": {},
            "diskstats": {},
            "net.dev": {},
            "net.snmp": {"TcpRetransSegs": 10},
            "net.netstat": {},
            "vmstat": {},
        }
        end = {
            "stat": {},
            "diskstats": {},
            "net.dev": {},
            "net.snmp": {"TcpRetransSegs": 30},
            "net.netstat": {},
            "vmstat": {},
        }
        sample = calculate_burst_metrics(start, end, 1.0)
        self.assertAlmostEqual(sample["net.tcp.max_retrans_per_sec"], 20)

    def test_disk_and_net_per_second_maxima(self):
        start = {
            "stat": {},
            "diskstats": {
                "sda": dict(time_in_progress=0, in_progress=0, sectors_read=0, sectors_written=0),
                "nvme0n1": dict(time_in_progress=0, in_progress=0, sectors_read=0, sectors_written=0),
            },
            "net.dev": {"eth0": dict(rx_bytes=0, tx_bytes=0)},
            "net.snmp": {},
            "net.netstat": {},
            "vmstat": {},
        }
        end = {
            "stat": {},
            "diskstats": {
                "sda": dict(time_in_progress=1000, in_progress=9, sectors_read=2048, sectors_written=0),
                "nvme0n1": dict(time_in_progress=0, in_progress=0, sectors_read=0, sectors_written=0),
            },
            "net.dev": {"eth0": dict(rx_bytes=3000, tx_bytes=1000)},
            "net.snmp": {},
            "net.netstat": {},
            "vmstat": {},
        }
        sample = calculate_burst_metrics(start, end, 1.0)
        self.assertEqual(sample["disk.max_utilization_pct"], 100)
        self.assertEqual(sample["disk.max_utilization_device"], "sda")
        self.assertEqual(sample["disk.sda.read_bytes_per_sec"], 2048 * 512)
        self.assertEqual(sample["net.eth0.rx_bytes_per_sec"], 3000)

    def test_missing_counter_family_yields_no_keys(self):
        start = counters()
        end = counters()
        self.assertNotIn("net.tcp.max_retrans_per_sec", calculate_burst_metrics(start, end, 1.0))
        self.assertNotIn("vm.max_major_faults_per_sec", calculate_burst_metrics(start, end, 1.0))


if __name__ == "__main__":
    unittest.main()
