import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from testutil import isolate_xdg

from microsre import state
from microsre.logreader import Docker, File, LineCounter, Logfile, file_offsets, get_oom

FAKE_DOCKER_CLI = (
    "#!/bin/sh\n"
    "# Test stand-in for the docker CLI: prints the canned ps output from\n"
    "# $FAKE_DOCKER_PS, log output from $FAKE_DOCKER_LOGS.\n"
    'if [ "$1" = "ps" ]; then cat "$FAKE_DOCKER_PS"; else cat "$FAKE_DOCKER_LOGS"; fi\n'
)


def setUpModule():
    isolate_xdg()


class TestLogReader(unittest.TestCase):
    def test_polling_sees_new_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            with self.assertRaises(StopIteration):
                next(reader.poll())

            f.write("someline\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            with self.assertRaises(StopIteration):
                next(gen)

    def test_polling_waits_for_a_newline(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("some")
            f.flush()

            with self.assertRaises(StopIteration):
                next(reader.poll())

            f.write("line")
            f.flush()

            with self.assertRaises(StopIteration):
                next(reader.poll())

            f.write("\nnext")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            with self.assertRaises(StopIteration):
                next(gen)

    def test_polling_known_truncation_issue(self):
        # Does not notice truncation and immediate write of exactly the same size
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("someline\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            with self.assertRaises(StopIteration):
                next(gen)

            f.truncate(0)
            f.seek(0)
            f.write("SOMELINE\n")
            f.flush()

            gen = reader.poll()
            with self.assertRaises(StopIteration):
                next(gen)

    def test_polling_handles_truncation(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("someline\nsomeline2\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            self.assertEqual(next(gen), "someline2\n")
            with self.assertRaises(StopIteration):
                next(gen)

            f.truncate(0)
            f.seek(0)
            f.write("SOMELINE\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "SOMELINE\n")
            with self.assertRaises(StopIteration):
                next(gen)

    def test_polling_handles_rotation(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("someline\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            with self.assertRaises(StopIteration):
                next(gen)

            os.rename(f.name, f.name + ".1")
            f = open(f.name, "w")
            f.write("SOMELINE\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "SOMELINE\n")
            with self.assertRaises(StopIteration):
                next(gen)

            os.unlink(f.name + ".1")

    def test_polling_handles_recreation(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("someline\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "someline\n")
            with self.assertRaises(StopIteration):
                next(gen)

            os.unlink(f.name)
            f = open(f.name, "w")
            f.write("SOMELINE\n")
            f.flush()

            gen = reader.poll()
            self.assertEqual(next(gen), "SOMELINE\n")
            with self.assertRaises(StopIteration):
                next(gen)

    def test_polling_handles_line_longer_than_a_block(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("x" * 10000 + "\n")
            f.write("short\n")
            f.flush()

            lines = list(reader.poll())
            self.assertEqual([len(line) for line in lines], [10001, 6])

    def test_polling_handles_line_of_exact_block_size(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("x" * 4095 + "\n")
            f.flush()

            lines = list(reader.poll())
            self.assertEqual([len(line) for line in lines], [4096])

    def test_polling_handles_line_written_across_polls(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            reader = File(f.name)

            f.write("x" * 5000)
            f.flush()
            self.assertEqual(list(reader.poll()), [])

            f.write("y" * 5000 + "\nnext\n")
            f.flush()

            lines = list(reader.poll())
            self.assertEqual([len(line) for line in lines], [10001, 5])

    @unittest.skipIf(os.geteuid() == 0, "permission checks do not bite as root")
    def test_unreadable_file_yields_nothing(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            f.write("hidden\n")
            f.flush()
            os.chmod(f.name, 0)

            reader = File(f.name, tail=False)
            self.assertEqual(list(reader.poll()), [])

    @unittest.skipIf(os.geteuid() == 0, "permission checks do not bite as root")
    def test_oom_backend_only_uses_readable_file(self):
        oom = get_oom()
        backend = getattr(oom, "file", None)
        if backend is not None:
            self.assertTrue(os.access(backend.path, os.R_OK), f"OOM backend reads unreadable {backend.path}")


class TestHeartbeat(unittest.TestCase):
    def test_match_updates_last_seen_and_row_carries_it(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(
                Logfile("batch", f.name), error_pattern="ERROR", line_pattern=".*", heartbeat_pattern="done"
            )

            f.write("INFO started\nERROR boom\n")
            f.flush()
            before = time.time()
            (row,) = counter.poll(None)
            # No match yet - the row still carries the clock (daemon-start value)
            self.assertEqual(row["heartbeat_last_seen"], counter.heartbeat_last_seen)
            self.assertLess(counter.heartbeat_last_seen, before)

            f.write("batch done\n")
            f.flush()
            (row,) = counter.poll(None)
            self.assertGreaterEqual(counter.heartbeat_last_seen, before)
            self.assertEqual(row["heartbeat_last_seen"], counter.heartbeat_last_seen)

    def test_clock_starts_at_zero_until_first_match(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(Logfile("batch", f.name), "ERROR", ".*", heartbeat_pattern="done")
            self.assertEqual(counter.heartbeat_last_seen, 0.0)

            f.write("batch done\n")
            f.flush()
            list(counter.poll(None))
            seen = counter.heartbeat_last_seen
            self.assertGreater(seen, 0)

            (row,) = counter.poll(None)  # nothing new
            self.assertEqual(counter.heartbeat_last_seen, seen)
            self.assertEqual(row["heartbeat_last_seen"], seen)

    def test_plain_lines_do_not_reset_the_clock(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(Logfile("batch", f.name), "ERROR", ".*", heartbeat_pattern="done")
            f.write("batch done\n")
            f.flush()
            list(counter.poll(None))
            seen = counter.heartbeat_last_seen

            f.write("INFO still running\n")
            f.flush()
            list(counter.poll(None))
            self.assertEqual(counter.heartbeat_last_seen, seen)

    def test_no_heartbeat_pattern_means_no_key(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(Logfile("app", f.name), "ERROR", ".*")
            f.write("anything\n")
            f.flush()
            (row,) = counter.poll(None)
            self.assertNotIn("heartbeat_last_seen", row)


class TestHeartbeatPersistence(unittest.TestCase):
    def setUp(self):
        Path(os.environ["XDG_STATE_HOME"], "microsre", "state.sqlite").unlink(missing_ok=True)

    def test_match_writes_through_to_state(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(
                Logfile("batch", f.name), "ERROR", ".*", heartbeat_pattern="done", metric_prefix="logs"
            )
            f.write("batch done\n")
            f.flush()
            before = time.time()
            list(counter.poll(None))

            beats = state.load_heartbeats()
            pattern, last_seen = beats["logs.batch.heartbeat_last_seen"]
            self.assertEqual(pattern, "done")
            self.assertGreaterEqual(last_seen, before)
            self.assertAlmostEqual(last_seen, time.time(), delta=5)

    def test_seed_restores_clock_across_restart(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            counter = LineCounter(
                Logfile("batch", f.name),
                "ERROR",
                ".*",
                heartbeat_pattern="done",
                heartbeat_seed=12345.0,
                metric_prefix="logs",
            )
            (row,) = counter.poll(None)
            self.assertEqual(row["heartbeat_last_seen"], 12345.0)


class TestDockerNonRoot(unittest.TestCase):
    """The non-root docker poller, driven by a fake docker CLI on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="microsre-test-docker-")
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(FAKE_DOCKER_CLI)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.ps = os.path.join(self.tmp, "ps")
        self.logs = os.path.join(self.tmp, "logs")
        open(self.ps, "w").close()
        open(self.logs, "w").close()

        self.saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_DOCKER_PS", "FAKE_DOCKER_LOGS")}
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_DOCKER_PS"] = self.ps
        os.environ["FAKE_DOCKER_LOGS"] = self.logs

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_docker(self):
        # The non-root poller, without the root/docker-availability probe in __init__
        docker = Docker.__new__(Docker)
        docker.whitelist = None
        docker.seen = set()
        docker.poll = docker._poll_docker
        return docker

    def since(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def test_stopped_container_keeps_ticking_so_heartbeat_ages(self):
        with open(self.ps, "w") as f:
            f.write("worker,1\n")
        with open(self.logs, "w") as f:
            f.write("alive\n")
        counter = LineCounter(self.make_docker(), "ERROR", ".*", heartbeat_pattern="alive")

        (row,) = counter.poll(self.since())
        self.assertEqual(row["line_count"], 1)
        seen = row["heartbeat_last_seen"]
        self.assertGreater(seen, 0)

        open(self.ps, "w").close()  # container stopped
        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "worker")
        self.assertEqual(row["line_count"], 0)
        self.assertEqual(row["heartbeat_last_seen"], seen)  # frozen match, but the row flows

        os.unlink(self.ps)  # daemon unreachable: same as everything stopped
        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "worker")

    def test_compose_suffix_stripped_for_seen_names(self):
        with open(self.ps, "w") as f:
            f.write("app-worker,2\n")
        counter = LineCounter(self.make_docker(), "ERROR", ".*")

        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "app-worker")

        open(self.ps, "w").close()
        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "app-worker")


class TestFileOffsetRestore(unittest.TestCase):
    def offset_of(self, f, pos):
        st = os.stat(f.name)
        return (st.st_ino, st.st_dev, pos)

    def test_matching_inode_resumes_from_offset(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            f.write("12345\n")
            f.flush()
            reader = File(f.name, offset=self.offset_of(f, 2))
            self.assertEqual(reader.pos, 2)
            gen = reader.poll()
            self.assertEqual(next(gen), "345\n")

    def test_rotated_file_tails_instead(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            f.write("12345\n")
            f.flush()
            reader = File(f.name, offset=(999999999, 999999999, 0))
            self.assertEqual(reader.pos, 6)

    def test_truncated_file_tails_instead(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".log") as f:
            f.write("12345\n")
            f.flush()
            reader = File(f.name, offset=self.offset_of(f, 999999))
            self.assertEqual(reader.pos, 6)

    def test_missing_file_starts_empty(self):
        reader = File("/nonexistent/microsre-test.log", offset=(1, 2, 3))
        self.assertEqual((reader.st_ino, reader.st_dev, reader.pos), (-1, -1, 0))


class TestDockerFileMode(unittest.TestCase):
    """Direct container-log reading, selected by readability of the base dir."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="microsre-test-containers-")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def make_container(self, cid, name, lines=(), num=None):
        d = Path(self.base) / cid
        d.mkdir()
        path = d / f"{cid}-json.log"
        with open(path, "w") as f:
            for line in lines:
                f.write(json.dumps({"log": line}) + "\n")
        labels = {Docker.CONTAINER_NUMBER: num} if num else {}
        (d / "config.v2.json").write_text(json.dumps({"Name": "/" + name, "Config": {"Labels": labels}}))
        return str(path)

    def since(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def test_readable_base_reads_container_logs(self):
        path = self.make_container("abc123", "worker")
        counter = LineCounter(Docker(base=self.base), ".*", ".*")

        with open(path, "a") as f:
            f.write(json.dumps({"log": "hello\n"}) + "\n")
            f.flush()

        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "worker")
        self.assertEqual(row["line_count"], 1)

    def test_compose_suffix_stripped_and_offset_restores_position(self):
        path = self.make_container("abc123", "worker", lines=("one\n", "two\n"), num="1")
        st = os.stat(path)
        with open(path, "rb") as f:
            f.readline()
            pos = f.tell()

        docker = Docker(base=self.base, offsets={("worker", path): (st.st_ino, st.st_dev, pos)})
        counter = LineCounter(docker, ".*", ".*")

        (row,) = counter.poll(self.since())
        self.assertEqual(row["name"], "worker")
        self.assertEqual(row["line_count"], 1)
        self.assertEqual(row["errors"], ["two\n"])

    def test_whitelist_filters_discovery(self):
        self.make_container("abc123", "worker")
        self.make_container("def456", "other")
        counter = LineCounter(Docker(["worker"], base=self.base), ".*", ".*")

        rows = list(counter.poll(self.since()))
        self.assertEqual([r["name"] for r in rows], ["worker"])

    def test_unreadable_base_falls_back_to_cli_mode(self):
        docker = Docker(base="/nonexistent/microsre-test/containers")
        self.assertFalse(hasattr(docker, "containers"))

    def test_file_offsets_reports_docker_positions(self):
        path = self.make_container("abc123", "worker", lines=("hello\n",))
        counter = LineCounter(Docker(base=self.base), ".*", ".*")

        offsets = file_offsets([counter])
        st = os.stat(path)
        self.assertEqual(offsets[("worker", path)], (st.st_ino, st.st_dev, st.st_size))
