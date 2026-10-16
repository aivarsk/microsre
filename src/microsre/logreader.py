import contextlib
import datetime
import json
import os
import re
import subprocess
import time
from functools import cache
from typing import Any

from microsre import state
from microsre.core import config, log


class File:
    def __init__(self, path, tail=True, offset=None):
        self.path = path
        self.st_ino = -1
        self.st_dev = -1
        self.pos = 0
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return

        # Adopt a persisted position only for the same inode and an intact file
        if offset:
            o_ino, o_dev, o_pos = offset
            if st.st_ino == o_ino and st.st_dev == o_dev and st.st_size >= o_pos:
                self.st_ino = st.st_ino
                self.st_dev = st.st_dev
                self.pos = o_pos
                return

        self.st_ino = st.st_ino
        self.st_dev = st.st_dev
        self.pos = st.st_size if tail else 0

    def poll(self):
        try:
            st = os.stat(self.path)
            # New file, truncated, etc
            if self.st_ino != st.st_ino or self.st_dev != st.st_dev or self.pos > st.st_size:
                self.st_ino = st.st_ino
                self.st_dev = st.st_dev
                self.pos = 0

            f = os.open(self.path, os.O_RDONLY)
        except (FileNotFoundError, PermissionError):
            return

        try:
            os.lseek(f, self.pos, os.SEEK_SET)
            read_size = 4 * 1024

            while True:
                size = read_size
                block = os.read(f, size)
                if not block:
                    break
                for line in block.splitlines(keepends=True):
                    if b"\n" not in line:
                        os.lseek(f, self.pos, os.SEEK_SET)
                        read_size = min(read_size * 2, 1024 * 1024)
                        break
                    self.pos += len(line)
                    yield line.decode("utf-8", errors="ignore")

                if len(block) < size:
                    break
        except OSError:
            ...
        finally:
            os.close(f)


class Logfile:
    def __init__(self, name, path, offset=None):
        self.name = name
        self.file = File(path, offset=offset)

    def poll(self, _since):
        yield self.name, None
        for line in self.file.poll():
            yield self.name, line


def output_of(*cmd):
    try:
        result = subprocess.run(
            cmd, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10
        )
        return result.stdout
    except FileNotFoundError:
        raise ValueError(f"{cmd[0]}: Command not found")
    except subprocess.TimeoutExpired:
        raise ValueError(f"{cmd[0]}: Timed out")
    except subprocess.CalledProcessError as e:
        raise ValueError(f"{cmd[0]} exit({e.returncode}): {e.stdout}")


class OOM:
    OOM = [
        re.compile(r"Out of memory:\s+Killed process\s+(?P<pid>\d+)\s+\((?P<comm>[^)]+)\)"),
        re.compile(r"oom-kill:constraint=.*task=(?P<comm>[^,]+),pid=(?P<pid>\d+)"),
        re.compile(r"Memory cgroup out of memory:\s+Kill process\s+(?P<pid>\d+)\s+\((?P<comm>[^)]+)\)"),
    ]

    def __init__(self):
        for path in ("/var/log/kern.log", "/var/log/syslog", "/var/log/messages"):
            if os.access(path, os.R_OK):
                self.file = File(path)
                self._poll = self._poll_file
                break
        else:
            try:
                output_of("journalctl", "--version")
                self._previous_lines = set()
                self._poll = self._poll_journalctl
            except ValueError:
                self._poll = lambda since: iter(())

    def _poll_file(self, since):
        for line in self.file.poll():
            if " kernel:" in line:
                yield line

    def _poll_journalctl(self, since):
        current_lines = set()
        for line in output_of("journalctl", "-k", "--no-pager", "--since", since.isoformat()).splitlines():
            if line == "-- No entries --" or line in self._previous_lines:
                continue
            current_lines.add(line)
            yield line
        # A delayed tick overlaps the previous window, do not report twice
        self._previous_lines = current_lines

    def poll(self, since):
        for line in self._poll(since):
            for r in self.OOM:
                if m := r.search(line):
                    d = m.groupdict()
                    yield {"pid": int(d["pid"]), "comm": d["comm"]}
                    break


class Docker:
    CONTAINER_NUMBER = "com.docker.compose.container-number"

    def __init__(self, whitelist=None, offsets=None, base="/var/lib/docker/containers"):
        self.whitelist = whitelist
        self.base = base
        if os.access(base, os.R_OK | os.X_OK):
            self.containers = {}
            self._docker_ps(tail=True, offsets=offsets)
            self.poll = self._poll_file
        else:
            try:
                output_of("docker", "ps")
                self.seen = set()
                self.poll = self._poll_docker
            except ValueError:
                self.poll = lambda since: iter(())

    def _real_name(self, name, num):
        return name.removesuffix("-" + num).removesuffix("_" + num) if num else name

    def _docker_ps(self, tail=False, offsets=None):
        try:
            containers = os.listdir(self.base)
        except FileNotFoundError:
            return
        for container in containers:
            if container in self.containers:
                continue

            path = os.path.join(self.base, container, container + "-json.log")
            if os.path.exists(path):
                with contextlib.suppress(json.JSONDecodeError, KeyError, FileNotFoundError):
                    with open(os.path.join(self.base, container, "config.v2.json")) as f:
                        cfg = json.load(f)
                    name = cfg["Name"][1:]
                    name = self._real_name(
                        name,
                        cfg["Config"].get("Labels", {}).get(self.CONTAINER_NUMBER),
                    )
                    if self.whitelist and name not in self.whitelist:
                        continue

                    self.containers[container] = (
                        name,
                        File(path, tail, offset=(offsets or {}).get((name, path))),
                    )

    def _poll_file(self, since):
        self._docker_ps(tail=False)
        for name, file in self.containers.values():
            yield name, None  # tick
            for line in file.poll():
                with contextlib.suppress(json.JSONDecodeError, KeyError):
                    yield name, json.loads(line)["log"]

    def _poll_docker(self, since):
        try:
            listing = output_of(
                "docker",
                "ps",
                '--format={{.Names}},{{.Label "' + self.CONTAINER_NUMBER + '"}}',
            ).splitlines()
        except ValueError:
            # Daemon unreachable: report every known container as silent
            listing = []

        running = set()
        for name_num in listing:
            full_name, num = name_num.split(",")
            name = self._real_name(full_name, num)
            if self.whitelist and name not in self.whitelist:
                continue
            running.add(name)
            yield name, None  # tick
            try:
                output = output_of("docker", "logs", "--since", since.isoformat(), full_name)
            except ValueError:
                # Container might be not running at this point
                continue
            for line in output.splitlines():
                yield name, line
        for name in self.seen - running:
            yield name, None  # tick: known but stopped, heartbeats keep aging
        self.seen.update(running)


class LineCounter:
    def __init__(
        self, poller, error_pattern, line_pattern, heartbeat_pattern=None, heartbeat_seed=0.0, metric_prefix=None
    ):
        self.line_re = re.compile(line_pattern)
        self.error_re = re.compile(error_pattern)
        self.heartbeat_pattern = heartbeat_pattern
        self.heartbeat_re = re.compile(heartbeat_pattern) if heartbeat_pattern else None
        # 0 = never seen, or the pattern changed since the clock was persisted
        self.heartbeat_last_seen = heartbeat_seed
        self.metric_prefix = metric_prefix
        self.poller = poller

    def poll(self, since):
        stats: dict[str, Any] = {}

        for name, line in self.poller.poll(since):
            stat = stats.setdefault(
                name,
                {
                    "name": name,
                    "line_count": 0,
                    "error_count": 0,
                    "errors": [],
                },
            )
            if line and self.line_re.search(line):
                stat["line_count"] += 1
                if self.heartbeat_re and self.heartbeat_re.search(line):
                    self.heartbeat_last_seen = time.time()
                    if self.metric_prefix:
                        state.touch_heartbeat(
                            f"{self.metric_prefix}.{name}.heartbeat_last_seen",
                            self.heartbeat_pattern,
                            self.heartbeat_last_seen,
                        )
                if self.error_re.search(line):
                    stat["error_count"] += 1
                    stat["errors"].append(line)

        if self.heartbeat_re:
            for stat in stats.values():
                stat["heartbeat_last_seen"] = self.heartbeat_last_seen
        return stats.values()


@cache
def get_oom():
    return OOM()


@cache
def get_logfiles(fail=False):
    logfiles = []
    for entry in config().get("logs", []):
        try:
            name = entry["name"]
            pattern = entry.get("heartbeat_pattern")
            stored = _heartbeats.get(f"logs.{name}.heartbeat_last_seen")
            logfiles.append(
                LineCounter(
                    Logfile(
                        name=name,
                        path=entry["path"],
                        offset=_offsets.get((name, entry["path"])),
                    ),
                    error_pattern=entry.get("error_pattern", ".*"),
                    line_pattern=entry.get("line_pattern", ".*"),
                    heartbeat_pattern=pattern,
                    heartbeat_seed=stored[1] if stored and stored[0] == pattern else 0.0,
                    metric_prefix="logs",
                )
            )
        except Exception as exc:
            if fail:
                raise ValueError(f"Invalid log configuration {entry}: {exc!r}") from exc
            log.error(f"Invalid log configuration {entry}", exc_info=True)
    return logfiles


@cache
def get_dockerlogs(fail=False):
    logfiles = []
    for entry in config().get("docker-logs", []):
        try:
            docker = Docker(entry.get("containers"), offsets=_docker_offsets)
            pattern = entry.get("heartbeat_pattern")
            # Container names do not exist until discovered, so seed by pattern
            # identity: a changed pattern starts over, like the file sources.
            seeds = [ls for m, (p, ls) in _heartbeats.items() if p == pattern and m.startswith("docker-logs.")]
            logfiles.append(
                LineCounter(
                    docker,
                    error_pattern=entry.get("error_pattern", ".*"),
                    line_pattern=entry.get("line_pattern", ".*"),
                    heartbeat_pattern=pattern,
                    heartbeat_seed=max(seeds, default=0.0),
                    metric_prefix="docker-logs",
                )
            )
        except Exception as exc:
            if fail:
                raise ValueError(f"Invalid docker-log configuration {entry}: {exc!r}") from exc
            log.error(f"Invalid docker-log configuration {entry}", exc_info=True)
    return logfiles


_offsets = {}
_docker_offsets = {}
_heartbeats = {}


def init_logs(fail=False, offsets=None):
    offsets = offsets or {}
    _offsets.clear()
    _docker_offsets.clear()
    _heartbeats.clear()
    _heartbeats.update(state.load_heartbeats())

    # Catching up after a restart is applied to heartbeat sources only: replayed
    # error lines would alert on stale errors, a missed heartbeat would false-fire.
    log_keys = {(e.get("name"), e.get("path")) for e in config().get("logs", []) if e.get("heartbeat_pattern")}
    _offsets.update({k: v for k, v in offsets.items() if k in log_keys})

    entries = [e for e in config().get("docker-logs", []) if e.get("heartbeat_pattern")]
    if any(not e.get("containers") for e in entries):
        _docker_offsets.update(offsets)  # an entry without a list watches all containers
    else:
        names = {n for e in entries for n in e.get("containers", [])}
        _docker_offsets.update({k: v for k, v in offsets.items() if k[0] in names})

    _ = get_oom()
    _ = get_logfiles(fail)
    _ = get_dockerlogs(fail)


def file_offsets(counters=None):
    """Current read positions of configured sources, for state.save()."""
    result = {}
    for counter in counters or (get_logfiles() + get_dockerlogs()):
        poller = counter.poller
        files = poller.containers.values() if isinstance(poller, Docker) else ((poller.name, poller.file),)
        for name, f in files:
            if f.st_ino > 0:
                result[(name, f.path)] = (f.st_ino, f.st_dev, f.pos)
    return result


def collect_logs(since):
    since = datetime.datetime.fromtimestamp(since, tz=datetime.timezone.utc)

    raw: dict[str, Any] = {}
    logs: dict[str, Any] = {}
    raw["docker-logs"] = logs
    start = time.monotonic_ns()
    for poller in get_dockerlogs():
        for row in poller.poll(since):
            add_logs(logs, row)
    raw["docker_logs_duration"] = time.monotonic_ns() - start

    start = time.monotonic_ns()
    raw["oom"] = list(get_oom().poll(since))
    raw["oom_duration"] = time.monotonic_ns() - start

    logs = {}
    raw["logs"] = logs
    start = time.monotonic_ns()
    for poller in get_logfiles():
        for row in poller.poll(since):
            add_logs(logs, row)
    raw["logs_duration"] = time.monotonic_ns() - start
    return raw


def add_logs(logs, row):
    m = logs.setdefault(row["name"], row)
    if m is not row:
        m["line_count"] += row["line_count"]
        m["error_count"] += row["error_count"]
        m["errors"] += row["errors"]
        if "heartbeat_last_seen" in row:
            m["heartbeat_last_seen"] = max(m.get("heartbeat_last_seen", 0), row["heartbeat_last_seen"])
