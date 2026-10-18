import time
import unittest

from testutil import isolate_xdg

from microsre.metrics import calculate_metrics


def setUpModule():
    isolate_xdg()


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def snapshot(**overrides):
    snap = {
        "timestamp": "2026-10-15T00:01:00+00:00",
        "hostname": "testhost",
        "cpu.count": 4,
        "procfs_duration": 150_000_000,
        "procfs_pid_duration": 100_000_000,
        "logs_duration": 40_000_000,
        "docker_logs_duration": 30_000_000,
        "oom_duration": 10_000_000,
        "proc.uptime": {"seconds": 100.0, "idle_seconds": 50.0},
        "proc.loadavg": {"1min": 0.0, "5min": 0.0, "15min": 0.0, "runnable": 0, "total_tasks": 1, "last_pid": 100},
        "proc.meminfo": {
            "MemTotal": 1_000_000,
            "MemAvailable": 500_000,
            "MemFree": 1,
            "Buffers": 0,
            "Cached": 0,
            "SwapTotal": 0,
            "SwapFree": 0,
        },
        "proc.stat": {"cpu": dict.fromkeys(CPU_FIELDS, 0), "procs_running": 0},
        "proc.net.dev": {},
        "proc.net.snmp": {},
        "proc.net.netstat": {},
        "proc.mountinfo": {},
        "proc.diskstats": {},
        "oom": [],
        "logs": {},
        "docker-logs": {},
        "self": {
            "cpu_seconds": 0.0,
            "read_bytes": 0,
            "rss_bytes": 0,
            "voluntary_ctxt_switches": 0,
        },
    }
    snap.update(overrides)
    return snap


def diskstats(**kw):
    fields = {
        "reads": 0,
        "sectors_read": 0,
        "time_reading": 0,
        "writes": 0,
        "sectors_written": 0,
        "time_writing": 0,
        "in_progress": 0,
        "time_in_progress": 0,
        "weighted_time_in_progress": 0,
    }
    fields.update(kw)
    return {"sda": fields}


INTERVAL = 60


class TestListenOverflow(unittest.TestCase):
    def test_growth_is_reported(self):
        start = snapshot(**{"proc.net.netstat": {"TcpExtListenDrops": 5, "TcpExtListenOverflows": 1}})
        end = snapshot(**{"proc.net.netstat": {"TcpExtListenDrops": 8, "TcpExtListenOverflows": 2}})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["net.tcp.listen_drops"], 3)
        self.assertEqual(metrics["net.tcp.listen_overflows"], 1)

    def test_counter_reset_clamps_to_zero(self):
        start = snapshot(**{"proc.net.netstat": {"TcpExtListenDrops": 100, "TcpExtListenOverflows": 50}})
        end = snapshot(**{"proc.net.netstat": {"TcpExtListenDrops": 10, "TcpExtListenOverflows": 5}})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["net.tcp.listen_drops"], 0)
        self.assertEqual(metrics["net.tcp.listen_overflows"], 0)

    def test_missing_netstat_yields_zero(self):
        metrics = calculate_metrics(snapshot(), snapshot(), INTERVAL)
        self.assertEqual(metrics["net.tcp.listen_drops"], 0)
        self.assertEqual(metrics["net.tcp.listen_overflows"], 0)


class TestRetransmit(unittest.TestCase):
    def test_ratio_is_percent_of_out_segments(self):
        start = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 1000, "TcpRetransSegs": 10}})
        end = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 2000, "TcpRetransSegs": 30}})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["net.tcp.retrans_per_sec"], 20 / INTERVAL)
        self.assertEqual(metrics["net.tcp.retrans_ratio_pct"], 2)

    def test_counter_reset_clamps_to_zero(self):
        start = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 1000, "TcpRetransSegs": 10}})
        end = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 800, "TcpRetransSegs": 5}})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["net.tcp.retrans_per_sec"], 0)
        self.assertEqual(metrics["net.tcp.retrans_ratio_pct"], 0)

    def test_no_out_segments_yields_zero_ratio(self):
        start = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 0, "TcpRetransSegs": 0}})
        end = snapshot(**{"proc.net.snmp": {"TcpOutSegs": 0, "TcpRetransSegs": 4}})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["net.tcp.retrans_ratio_pct"], 0)


class TestCpuCounters(unittest.TestCase):
    def test_utilization_is_sum_of_per_field_deltas(self):
        start = snapshot(
            **{"proc.stat": {"cpu": dict(zip(CPU_FIELDS, (100, 0, 100, 700, 0, 0, 0, 0))), "procs_running": 0}}
        )
        end = snapshot(
            **{"proc.stat": {"cpu": dict(zip(CPU_FIELDS, (200, 0, 200, 900, 0, 0, 0, 0))), "procs_running": 0}}
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["cpu.utilization_pct"], 50)

    def test_regressed_field_counts_as_zero_not_negative(self):
        # A kernel race can make one counter move backwards; that field contributes
        # 0 to the interval instead of poisoning the whole sum.
        start = snapshot(
            **{"proc.stat": {"cpu": dict(zip(CPU_FIELDS, (100, 0, 100, 700, 0, 0, 0, 0))), "procs_running": 0}}
        )
        end = snapshot(
            **{"proc.stat": {"cpu": dict(zip(CPU_FIELDS, (150, 0, 50, 900, 0, 0, 0, 0))), "procs_running": 0}}
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["cpu.utilization_pct"], 20)

    def test_per_core_utilization_is_flattened_with_cpu_list(self):
        start = snapshot(
            **{
                "proc.stat": {
                    "cpu": dict.fromkeys(CPU_FIELDS, 0),
                    "cpu0": dict.fromkeys(CPU_FIELDS, 0),
                    "cpu1": dict.fromkeys(CPU_FIELDS, 0),
                    "procs_running": 0,
                }
            }
        )
        end = snapshot(
            **{
                "proc.stat": {
                    "cpu": dict.fromkeys(CPU_FIELDS, 0),
                    # 50% and 10% utilization over the interval
                    "cpu0": dict(zip(CPU_FIELDS, (100, 0, 0, 100, 0, 0, 0, 0))),
                    "cpu1": dict(zip(CPU_FIELDS, (10, 0, 0, 90, 0, 0, 0, 0))),
                    "procs_running": 0,
                }
            }
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["cpu.cpu0.utilization_pct"], 50)
        self.assertEqual(metrics["cpu.cpu1.utilization_pct"], 10)
        self.assertEqual(metrics["cpu.list"], "cpu0,cpu1")
        self.assertEqual(metrics["cpu.max_utilization_pct"], 50)
        self.assertEqual(metrics["cpu.max_utilization_core"], "cpu0")


class TestMetricShape(unittest.TestCase):
    def test_empty_lists_are_empty_strings(self):
        metrics = calculate_metrics(snapshot(), snapshot(), INTERVAL)
        for name in ("cpu.list", "disk.list", "net.list", "logs.list", "docker-logs.list"):
            self.assertEqual(metrics[name], "")

    def test_all_values_are_scalars(self):
        # metrics is a flat map: numbers, strings, and None for unknown gauges;
        # no lists or dicts
        end = snapshot(**{"proc.diskstats": diskstats(reads=1, writes=1, time_reading=1, time_writing=1)})
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertNotIn("oom", metrics)
        for name, value in metrics.items():
            self.assertIsInstance(value, (int, float, str, type(None)), name)


class TestDiskCounters(unittest.TestCase):
    def test_await_and_utilization(self):
        start = snapshot(**{"proc.diskstats": diskstats()})
        end = snapshot(
            **{
                "proc.diskstats": diskstats(
                    reads=100,
                    writes=100,
                    time_reading=3000,
                    time_writing=1000,
                    time_in_progress=60000,
                    weighted_time_in_progress=120000,
                )
            }
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["disk.sda.await_ms"], 20)
        self.assertEqual(metrics["disk.max.await_ms"], 20)
        self.assertEqual(metrics["disk.sda.utilization_pct"], 100)
        self.assertEqual(metrics["disk.sda.weighted_utilization_pct"], 200)
        self.assertEqual(metrics["disk.max.device"], "sda")

    def test_counter_reset_clamps_to_zero(self):
        start = snapshot(
            **{
                "proc.diskstats": diskstats(
                    reads=100,
                    sectors_read=1000,
                    writes=100,
                    sectors_written=1000,
                    time_reading=3000,
                    time_writing=1000,
                    time_in_progress=60000,
                    weighted_time_in_progress=120000,
                )
            }
        )
        end = snapshot(**{"proc.diskstats": diskstats()})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["disk.sda.await_ms"], 0)
        self.assertEqual(metrics["disk.sda.utilization_pct"], 0)
        self.assertEqual(metrics["disk.sda.read_bytes_per_sec"], 0)
        self.assertEqual(metrics["disk.total.write_iops"], 0)
        self.assertEqual(metrics["disk.max.utilization_pct"], 0)


class TestQueueDepth(unittest.TestCase):
    def test_gauge_reports_current_value_not_a_diff(self):
        start = snapshot(**{"proc.diskstats": diskstats(in_progress=1)})
        end = snapshot(**{"proc.diskstats": diskstats(in_progress=9)})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["disk.sda.queue_depth"], 9)
        # utilization 0 - the max-util device was never attributed
        self.assertEqual(metrics["disk.max.queue_depth"], 0)

    def test_max_attributes_to_busiest_device(self):
        start = snapshot(**{"proc.diskstats": diskstats()})
        end = snapshot(**{"proc.diskstats": diskstats(in_progress=9, time_in_progress=30000)})
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["disk.sda.utilization_pct"], 50)
        self.assertEqual(metrics["disk.max.queue_depth"], 9)
        self.assertEqual(metrics["disk.max.device"], "sda")


class TestStackedDevices(unittest.TestCase):
    def test_mapper_over_whole_disk_counts_once(self):
        from microsre.procfs import _is_stacked

        self.assertTrue(_is_stacked(["sda"], {"sda", "dm-0"}))

    def test_mapper_over_partition_counts_once(self):
        from microsre.procfs import _is_stacked

        self.assertTrue(_is_stacked(["sda2"], {"sda", "dm-0"}))
        self.assertTrue(_is_stacked(["nvme0n1p1"], {"nvme0n1", "dm-1"}))

    def test_raid_over_mappers_counts_once(self):
        from microsre.procfs import _is_stacked

        devices = {"sda", "sdb", "dm-0", "dm-1", "md0"}
        self.assertTrue(_is_stacked(["dm-0", "dm-1"], devices))

    def test_plain_disks_are_kept(self):
        from microsre.procfs import _is_stacked

        self.assertFalse(_is_stacked([], {"sda"}))
        self.assertFalse(_is_stacked(["unrelated2"], {"nvme0n1"}))


class TestRunQueue(unittest.TestCase):
    def test_raw_count_and_pct_of_cpu_count(self):
        end = snapshot(
            **{
                "proc.stat": {"cpu": dict.fromkeys(CPU_FIELDS, 0), "procs_running": 17},
                "cpu.count": 8,
            }
        )
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertEqual(metrics["cpu.run_queue"], 17)
        self.assertEqual(metrics["cpu.run_queue_per_cpu"], 212)


class TestMemory(unittest.TestCase):
    def test_used_is_total_minus_available(self):
        metrics = calculate_metrics(snapshot(), snapshot(), INTERVAL)
        self.assertEqual(metrics["mem.utilization_pct"], 50)

    def test_available_falls_back_to_free_on_old_kernels(self):
        end = snapshot()
        del end["proc.meminfo"]["MemAvailable"]
        end["proc.meminfo"]["MemFree"] = 250_000
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertEqual(metrics["mem.utilization_pct"], 75)


class TestCollectionDurations(unittest.TestCase):
    def test_raw_nanoseconds_become_seconds(self):
        metrics = calculate_metrics(snapshot(), snapshot(), INTERVAL)
        self.assertEqual(metrics["collect.procfs_seconds"], 0.15)
        self.assertEqual(metrics["collect.proc_pid_seconds"], 0.1)
        self.assertEqual(metrics["collect.logs_seconds"], 0.04)
        self.assertEqual(metrics["collect.docker_logs_seconds"], 0.03)
        self.assertEqual(metrics["collect.oom_seconds"], 0.01)


class TestSelfMetrics(unittest.TestCase):
    def test_totals_and_per_interval_rates(self):
        start = snapshot(
            **{
                "self": {
                    "cpu_seconds": 10.0,
                    "read_bytes": 1_000,
                    "rss_bytes": 50_000_000,
                    "voluntary_ctxt_switches": 60,
                }
            }
        )
        end = snapshot(
            **{
                "self": {
                    "cpu_seconds": 10.5,
                    "read_bytes": 61_000,
                    "rss_bytes": 52_000_000,
                    "voluntary_ctxt_switches": 63,
                }
            }
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["self.cpu_seconds"], 10.5)
        self.assertEqual(metrics["self.cpu_pct"], 0.83)
        self.assertEqual(metrics["self.rss_bytes"], 52_000_000)
        self.assertEqual(metrics["self.read_bytes_per_sec"], 1000)
        self.assertEqual(metrics["self.ctx_switches_per_sec"], 0.05)

    def test_counter_restart_clamps_rates_to_zero(self):
        start = snapshot(
            **{
                "self": {
                    "cpu_seconds": 100.0,
                    "read_bytes": 500_000,
                    "rss_bytes": 50_000_000,
                    "voluntary_ctxt_switches": 600,
                }
            }
        )
        end = snapshot(
            **{
                "self": {
                    "cpu_seconds": 1.0,
                    "read_bytes": 1_000,
                    "rss_bytes": 50_000_000,
                    "voluntary_ctxt_switches": 6,
                }
            }
        )
        metrics = calculate_metrics(start, end, INTERVAL)
        self.assertEqual(metrics["self.cpu_seconds"], 1.0)
        self.assertEqual(metrics["self.cpu_pct"], 0)
        self.assertEqual(metrics["self.read_bytes_per_sec"], 0)
        self.assertEqual(metrics["self.ctx_switches_per_sec"], 0)


class TestHeartbeatAge(unittest.TestCase):
    def row(self, **kw):
        row = {"name": "batch", "line_count": 1, "error_count": 0, "errors": []}
        row.update(kw)
        return {"logs": {"batch": row}}

    def test_age_is_seconds_since_last_match(self):
        end = snapshot(**self.row(heartbeat_last_seen=time.time() - 100))
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertAlmostEqual(metrics["logs.batch.heartbeat_age_seconds"], 100, delta=2)
        self.assertAlmostEqual(metrics["logs.batch.heartbeat_last_seen"], time.time() - 100, delta=2)
        self.assertEqual(
            metrics["logs.batch.heartbeat_status"],
            f"not seen for {int(metrics['logs.batch.heartbeat_age_seconds'])} seconds",
        )

    def test_never_seen_reports_huge_age_and_plain_status(self):
        end = snapshot(**self.row(heartbeat_last_seen=0))
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertAlmostEqual(metrics["logs.batch.heartbeat_age_seconds"], time.time(), delta=2)
        self.assertEqual(metrics["logs.batch.heartbeat_last_seen"], 0)
        self.assertEqual(metrics["logs.batch.heartbeat_status"], "not seen yet")

    def test_future_timestamp_clamps_to_zero(self):
        end = snapshot(**self.row(heartbeat_last_seen=time.time() + 500))
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertEqual(metrics["logs.batch.heartbeat_age_seconds"], 0)
        self.assertEqual(metrics["logs.batch.heartbeat_status"], "not seen for 0 seconds")

    def test_source_without_heartbeat_has_no_metrics(self):
        end = snapshot(**self.row())
        metrics = calculate_metrics(snapshot(), end, INTERVAL)
        self.assertNotIn("logs.batch.heartbeat_age_seconds", metrics)
        self.assertNotIn("logs.batch.heartbeat_last_seen", metrics)
        self.assertNotIn("logs.batch.heartbeat_status", metrics)


class TestAlertable(unittest.TestCase):
    def test_listen_metrics_validate_against_known_metrics(self):
        from microsre.alert import AlertState, _known_metrics

        template = {"title": "{net.tcp.listen_overflows} on {hostname}", "body": ""}
        alert = AlertState("net.tcp.listen_overflows", None, 1, 1, 0, template)
        alert.validate(_known_metrics())

    def test_self_metrics_validate_against_known_metrics(self):
        from microsre.alert import AlertState, _known_metrics

        template = {"title": "microsre RSS {self.rss_bytes} on {hostname}", "body": ""}
        alert = AlertState("self.rss_bytes", None, 500_000_000, 0, 0, template)
        alert.validate(_known_metrics())


if __name__ == "__main__":
    unittest.main()
