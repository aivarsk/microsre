import socket
import threading
import unittest

from microsre import prom
from microsre.core import BackgroundWorker


def http_get(port, path):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        response = b""
        while chunk := sock.recv(4096):
            response += chunk
    return response


class TestParseListen(unittest.TestCase):
    def test_missing_key_disables_endpoint(self):
        self.assertIsNone(prom.parse_listen({}))

    def test_valid_address(self):
        self.assertEqual(prom.parse_listen({"prometheus_listen": "127.0.0.1:9100"}), ("127.0.0.1", 9100))

    def test_rejects_garbage(self):
        for bad in ("9100", "host:", ":9100", "host:port"):
            with self.assertRaises(ValueError):
                prom.parse_listen({"prometheus_listen": bad})


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self.worker = BackgroundWorker()
        self.sock = prom.listener({"prometheus_listen": "127.0.0.1:0"})
        self.worker.add_listener(self.sock, prom.handle)
        self.port = self.sock.getsockname()[1]

    def tearDown(self):
        self.worker.shutdown()
        self.sock.close()

    def test_metrics_serves_last_published_snapshot(self):
        response = http_get(self.port, "/metrics")
        self.assertIn(b"# no metrics collected yet", response)

        prom.publish({"mem.utilization_pct": 42})
        response = http_get(self.port, "/metrics")
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"microsre_mem_utilization_pct 42", response)

    def test_unknown_path_is_404(self):
        response = http_get(self.port, "/")
        self.assertTrue(response.startswith(b"HTTP/1.1 404"))

    def test_submitted_tasks_still_run(self):
        done = threading.Event()
        self.worker.submit(done.set)
        self.assertTrue(done.wait(timeout=5))


class TestRender(unittest.TestCase):
    def test_flat_metrics_get_prefix_and_underscores(self):
        text = prom.render({"mem.utilization_pct": 50, "cpu.loadavg.1min.previous": 1.5})
        self.assertIn("microsre_mem_utilization_pct 50", text)
        self.assertIn("microsre_cpu_loadavg_1min_previous 1.5", text)

    def test_disk_and_net_families_become_labels(self):
        metrics = {
            "disk.list": "sda",
            "disk.sda.await_ms": 20,
            "disk.sda.utilization_pct": 40.5,
            "disk.sda.queue_depth": 3,
            "net.list": "eth0",
            "net.eth0.rx_bytes_per_sec": 100,
        }
        text = prom.render(metrics)
        self.assertIn('microsre_disk_await_ms{device="sda"} 20', text)
        self.assertIn('microsre_disk_utilization_pct{device="sda"} 40.5', text)
        self.assertIn('microsre_disk_queue_depth{device="sda"} 3', text)
        self.assertIn('microsre_net_rx_bytes_per_sec{iface="eth0"} 100', text)
        # labeled families are not duplicated as flat names
        self.assertNotIn("microsre_disk_sda_await_ms", text)

    def test_per_core_becomes_label(self):
        metrics = {
            "cpu.list": "cpu0,cpu1",
            "cpu.cpu0.utilization_pct": 10,
            "cpu.cpu1.utilization_pct": 5,
        }
        text = prom.render(metrics)
        self.assertIn('microsre_cpu_utilization_per_core{core="cpu0"} 10', text)
        self.assertIn('microsre_cpu_utilization_per_core{core="cpu1"} 5', text)
        # labeled families are not duplicated as flat names
        self.assertNotIn("microsre_cpu_cpu0_utilization_pct", text)

    def test_empty_lists_emit_no_labeled_lines(self):
        text = prom.render({"cpu.list": "", "disk.list": "", "net.list": ""})
        self.assertEqual(text, "\n")

    def test_non_numeric_fields_are_skipped(self):
        metrics = {"hostname": "h", "disk.max.device": None, "net.max.rx_iface": "eth0", "oom.victims": "PID=1 x"}
        self.assertEqual(prom.render(metrics), "\n")

    def test_log_source_names_are_sanitized(self):
        text = prom.render({"logs.my-app.error_count": 3, "docker-logs.my_app.error_pct": 2})
        self.assertIn("microsre_logs_my_app_error_count 3", text)
        self.assertIn("microsre_docker_logs_my_app_error_pct 2", text)

    def test_type_line_emitted_once_per_name(self):
        metrics = {"disk.list": "sda,sdb", "disk.sda.await_ms": 1, "disk.sdb.await_ms": 2}
        text = prom.render(metrics)
        self.assertEqual(text.count("# TYPE microsre_disk_await_ms gauge"), 1)

    def test_help_lines_come_from_registry(self):
        metrics = {"mem.utilization_pct": 50, "disk.list": "sda", "disk.sda.await_ms": 20}
        text = prom.render(metrics)
        self.assertIn("# HELP microsre_mem_utilization_pct MemTotal, MemAvailable", text)
        self.assertIn("(source: /proc/meminfo)", text)
        self.assertIn("# HELP microsre_disk_await_ms", text)
        self.assertLess(
            text.index("# HELP microsre_mem_utilization_pct"), text.index("# TYPE microsre_mem_utilization_pct")
        )
