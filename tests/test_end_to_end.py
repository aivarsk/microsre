import gzip
import json
import os
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from testutil import Receiver

SRC = str(Path(__file__).resolve().parent.parent / "src")


def read_until(proc, needle, buf="", timeout=20):
    """Accumulate stdout until needle appears or the deadline passes.

    Waits for the first tick so a log write happens after the tail is positioned,
    instead of racing a slow startup with a fixed sleep.
    """
    deadline = time.time() + timeout
    while needle not in buf and time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(deadline - time.time(), 0))
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 4096).decode(errors="replace")
        if not chunk:
            break
        buf += chunk
    return buf


def microsre_env(tmp):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
    env["XDG_STATE_HOME"] = str(Path(tmp) / "state")
    return env


def write_config(tmp, text):
    config_dir = Path(tmp) / "config" / "microsre"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(text)


def start_microsre(tmp, args, stdout=subprocess.PIPE):
    return subprocess.Popen(
        [sys.executable, "-m", "microsre", *args],
        env=microsre_env(tmp),
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_microsre(args, tmp, timeout=15):
    return subprocess.run(
        [sys.executable, "-m", "microsre", *args],
        env=microsre_env(tmp),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )


def decode(body, headers):
    if headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)


def kind_of(request, wanted):
    path, headers, body = request
    return path.startswith("/ingest") and decode(body, headers).get("kind") == wanted


class TestFirstRun(unittest.TestCase):
    def test_version_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_microsre(["--version"], tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("State file:", result.stdout)
            self.assertIn(str(Path(tmp) / "state" / "microsre" / "state.sqlite"), result.stdout)
            self.assertFalse((Path(tmp) / "config" / "microsre" / "config.toml").exists())
            self.assertFalse((Path(tmp) / "state" / "microsre" / "machine-id").exists())
            self.assertFalse((Path(tmp) / "state" / "microsre" / "state.sqlite").exists())

    def test_first_real_run_materializes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_microsre(["--verify"], tmp)
            self.assertEqual(result.returncode, 0, result.stderr)

            config_path = Path(tmp) / "config" / "microsre" / "config.toml"
            self.assertTrue(config_path.exists())
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

            # machine-id is created on first ingest, i.e. in daemon mode
            machine_id_path = Path(tmp) / "state" / "microsre" / "machine-id"
            self.assertFalse(machine_id_path.exists())


class TestVerify(unittest.TestCase):
    def verify(self, tmp, config_text):
        write_config(tmp, config_text)
        return run_microsre(["--verify"], tmp)

    def test_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.verify(tmp, "templates = []\n\n[alerts]\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Configuration file valid", result.stdout)

    def test_unknown_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.verify(
                tmp,
                'templates = []\n\n[alerts]\n"cpu.utilization_pct" = { max = 80, template = "missing" }\n',
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Invalid configuration file", result.stdout)

    def test_broken_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.verify(
                tmp,
                'templates = []\n\n[alerts]\n\n[[logs]]\nname = "x"\npath = "/tmp/x.log"\nerror_pattern = "["\n',
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Invalid configuration file", result.stdout)

    def test_broken_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.verify(tmp, "this is [ not toml\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn("Invalid configuration file", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_heartbeat_alert_config_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.verify(
                tmp,
                "[alerts]\n"
                '"logs.batch.heartbeat_age_seconds" = { max = 87000, duration = 0, template = "heartbeat" }\n\n'
                "[[templates]]\n"
                'name = "heartbeat"\n'
                'title = "{scope.name} on {hostname}"\n'
                'body = "age {metric.value}"\n\n'
                '[[logs]]\nname = "batch"\npath = "/tmp/microsre-batch.log"\nheartbeat_pattern = "done"\n',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Configuration file valid", result.stdout)


class TestSnapshot(unittest.TestCase):
    def test_dashboard_counts_errors_in_watched_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            log_path.touch()
            write_config(
                tmp,
                f'templates = []\n\n[alerts]\n\n[[logs]]\nname = "app"\npath = "{log_path}"\nerror_pattern = "ERROR"\n',
            )

            proc = start_microsre(tmp, ["--snapshot", "2"])
            buf = ""
            try:
                buf = read_until(proc, "Alerting:", buf)  # first tick: tail positioned at end of file
                with open(log_path, "a") as f:
                    f.write("INFO started\nERROR boom\nERROR bang\nERROR kaput\n")
                    f.flush()
                buf = read_until(proc, "err 3", buf, timeout=10)  # second tick picks the lines up
            finally:
                proc.send_signal(signal.SIGTERM)
                out, stderr = proc.communicate(timeout=15)
                buf += out

            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("Alerting:", buf)
            self.assertIn("err 3", buf)
            self.assertIn("%err 75", buf)


class TestHeartbeatSnapshot(unittest.TestCase):
    def test_silence_after_heartbeat_fires_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "batch.log"
            log_path.touch()
            write_config(
                tmp,
                f"[alerts]\n"
                f'"logs.batch.heartbeat_age_seconds" = {{ max = 4, duration = 0, template = "heartbeat" }}\n\n'
                f"[[templates]]\n"
                f'name = "heartbeat"\n'
                f'title = "{{scope.name}} silent on {{hostname}}"\n'
                f'body = "age {{metric.value}}"\n\n'
                f'[[logs]]\nname = "batch"\npath = "{log_path}"\nheartbeat_pattern = "done"\n',
            )

            proc = start_microsre(tmp, ["--snapshot", "2"])
            buf = ""
            try:
                buf = read_until(proc, "Alerting:", buf)  # first tick, tail positioned at end of file
                with open(log_path, "a") as f:
                    f.write("2026-10-16 batch done\n")
                    f.flush()
                # one heartbeat tick to reset the clock, then silent ticks aging past the threshold
                time.sleep(9)
            finally:
                proc.send_signal(signal.SIGTERM)
                out, stderr = proc.communicate(timeout=15)
                buf += out

            self.assertEqual(proc.returncode, 0, stderr)
            alerting = [line for line in buf.splitlines() if line.startswith("Alerting:")]
            self.assertGreaterEqual(len(alerting), 3, buf)
            self.assertIn("logs.batch.heartbeat_age_seconds", alerting[0])  # never seen: warns from tick one
            self.assertNotIn("heartbeat_age_seconds", alerting[1])  # right after the beat: quiet
            self.assertIn("logs.batch.heartbeat_age_seconds", alerting[-1])  # silence: alerting


class TestDaemon(unittest.TestCase):
    def test_lifecycle_and_alert_delivery(self):
        receiver = Receiver()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                write_config(
                    tmp,
                    f'ingest_url = "http://127.0.0.1:{receiver.port}/ingest"\n'
                    f'webhook_url = "http://127.0.0.1:{receiver.port}/hook"\n'
                    f"interval = 5\n\n"
                    '[alerts]\n"mem.utilization_pct" = { min = 100, duration = 0, template = "metric" }\n\n'
                    "[[templates]]\n"
                    'name = "metric"\n'
                    'title = "{metric.name} alert on {hostname}"\n'
                    'body = "value {metric.value}"\n',
                )

                def tick_sends_summary_and_alert_settings(request):
                    if not kind_of(request, "tick"):
                        return False
                    payload = decode(request[2], request[1])
                    return (
                        payload.get("machine_id") == machine_id_path.read_text()
                        and "cpu.loadavg.1min" in payload.get("metrics", {})
                        and "raw" not in payload  # no log lines or /proc dumps leave the machine
                        and payload.get("alerts")
                        == [
                            {
                                "metric": "mem.utilization_pct",
                                "min": 100,
                                "max": None,
                                "duration": 0,
                                "recovery_margin": 0,
                                "alerting": True,
                            }
                        ]
                    )

                proc = start_microsre(tmp, ["--daemon"], stdout=subprocess.DEVNULL)
                try:
                    self.assertTrue(
                        receiver.wait_for(lambda rs: any(kind_of(r, "up") for r in rs), 30),
                        "no 'up' event received",
                    )

                    machine_id_path = Path(tmp) / "state" / "microsre" / "machine-id"
                    deadline = time.time() + 10
                    while not machine_id_path.exists() and time.time() < deadline:
                        time.sleep(0.25)
                    self.assertTrue(machine_id_path.exists(), "machine-id not created")
                    self.assertEqual(stat.S_IMODE(machine_id_path.stat().st_mode), 0o600)

                    self.assertTrue(
                        receiver.wait_for(lambda rs: any(tick_sends_summary_and_alert_settings(r) for r in rs), 30),
                        "no 'tick' event with metrics, no raw data, and alert settings received",
                    )

                    self.assertTrue(
                        receiver.wait_for(
                            lambda rs: any(
                                path.startswith("/hook") and b"mem.utilization_pct" in body for path, _, body in rs
                            ),
                            30,
                        ),
                        "no alert delivered to webhook",
                    )

                    proc.send_signal(signal.SIGTERM)
                    self.assertEqual(proc.wait(timeout=15), 0)
                    self.assertTrue(
                        receiver.wait_for(lambda rs: any(kind_of(r, "down") for r in rs), 15),
                        "no 'down' event received",
                    )
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
        finally:
            receiver.close()


class TestStaleOffsets(unittest.TestCase):
    def test_stale_saved_offsets_reported_to_ingest(self):
        receiver = Receiver()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                write_config(tmp, f'ingest_url = "http://127.0.0.1:{receiver.port}/ingest"\ninterval = 5\n')

                # Seed state, then age the save timestamp past FRESH_SECONDS
                env = microsre_env(tmp)
                subprocess.run(
                    [sys.executable, "-c", "from microsre import state; state.save({})"],
                    env=env,
                    check=True,
                )
                db = sqlite3.connect(Path(tmp) / "state" / "microsre" / "state.sqlite")
                db.execute("UPDATE meta SET value = ? WHERE key = 'offsets_saved_at'", (str(time.time() - 700),))
                db.commit()
                db.close()

                proc = start_microsre(tmp, ["--daemon"], stdout=subprocess.DEVNULL)
                try:
                    self.assertTrue(
                        receiver.wait_for(lambda rs: any(kind_of(r, "stale_offsets") for r in rs), 30),
                        "no 'stale_offsets' event received",
                    )
                finally:
                    proc.send_signal(signal.SIGTERM)
                    self.assertEqual(proc.wait(timeout=15), 0)
        finally:
            receiver.close()


def run_heartbeat_snapshot(tmp, log_path, seconds, pattern="done", write=None, threshold=4):
    """Run --snapshot once over a single heartbeat-watched log; return Alerting lines.

    write lands after the first tick, so the log tail is already positioned.
    """
    write_config(
        tmp,
        f"[alerts]\n"
        f'"logs.batch.heartbeat_age_seconds" = {{ max = {threshold}, duration = 0, template = "heartbeat" }}\n\n'
        f"[[templates]]\n"
        f'name = "heartbeat"\n'
        f'title = "{{scope.name}} silent on {{hostname}}"\n'
        f'body = "age {{metric.value}}"\n\n'
        f'[[logs]]\nname = "batch"\npath = "{log_path}"\nheartbeat_pattern = "{pattern}"\n',
    )
    proc = start_microsre(tmp, ["--snapshot", "2"])
    buf = ""
    try:
        buf = read_until(proc, "Alerting:", buf)  # first tick, tail positioned at end of file
        if write is not None:
            with open(log_path, "a") as f:
                f.write(write)
                f.flush()
        time.sleep(seconds)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, stderr = proc.communicate(timeout=15)
        buf += out
    assert proc.returncode == 0, stderr
    return [line for line in buf.splitlines() if line.startswith("Alerting:")]


class TestPersistence(unittest.TestCase):
    def test_restart_catches_up_heartbeat_written_while_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "batch.log"
            log_path.touch()

            run_heartbeat_snapshot(tmp, log_path, seconds=5)
            with open(log_path, "a") as f:
                f.write("batch done while down\n")

            alerting = run_heartbeat_snapshot(tmp, log_path, seconds=3.5)
            self.assertTrue(alerting, "no ticks produced")
            self.assertNotIn("heartbeat_age_seconds", alerting[0])  # caught up, no false "not seen yet"

    def test_offsets_older_than_ten_minutes_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "batch.log"
            log_path.touch()

            run_heartbeat_snapshot(tmp, log_path, seconds=5)
            with open(log_path, "a") as f:
                f.write("batch done while down\n")

            db = sqlite3.connect(Path(tmp) / "state" / "microsre" / "state.sqlite")
            db.execute("UPDATE meta SET value = ? WHERE key = 'offsets_saved_at'", (str(time.time() - 700),))
            db.commit()
            db.close()

            alerting = run_heartbeat_snapshot(tmp, log_path, seconds=3.5)
            self.assertTrue(alerting, "no ticks produced")
            self.assertIn("logs.batch.heartbeat_age_seconds", alerting[0])  # tailed past it: not seen yet

    def test_last_seen_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "batch.log"
            log_path.touch()

            run_heartbeat_snapshot(tmp, log_path, seconds=6, write="batch done\n")
            alerting = run_heartbeat_snapshot(tmp, log_path, seconds=3.5, threshold=300)
            self.assertTrue(alerting, "no ticks produced")
            # Nothing new to read: quiet is only possible via the persisted clock
            self.assertNotIn("heartbeat_age_seconds", alerting[0])

    def test_changed_pattern_starts_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "batch.log"
            log_path.touch()

            run_heartbeat_snapshot(tmp, log_path, seconds=6, write="batch done\n")
            alerting = run_heartbeat_snapshot(tmp, log_path, seconds=3.5, pattern="finished")
            self.assertIn("logs.batch.heartbeat_age_seconds", alerting[0])  # new pattern, fresh identity


class TestStartup(unittest.TestCase):
    def test_invalid_heartbeat_pattern_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(
                tmp,
                '[[logs]]\nname = "x"\npath = "/tmp/microsre-test-x.log"\nheartbeat_pattern = "["\n',
            )
            result = run_microsre(["--snapshot", "2"], tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Cannot start", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_daemon_rejects_interval_below_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(tmp, "interval = 2\n")
            result = run_microsre(["--daemon"], tmp, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Cannot start", result.stdout)
            self.assertIn("Invalid interval", result.stdout)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
