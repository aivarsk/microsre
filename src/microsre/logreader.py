import contextlib
import datetime
import json
import os
import re
import subprocess
import time
from functools import cache
from typing import Any

from microsre.core import config, log


class File:
    def __init__(self, path, tail=True):
        self.path = path
        try:
            st = os.stat(self.path)
            self.st_ino = st.st_ino
            self.st_dev = st.st_dev
            self.pos = st.st_size if tail else 0
        except FileNotFoundError:
            self.st_ino = -1
            self.st_dev = -1
            self.pos = 0

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
    def __init__(self, name, path):
        self.name = name
        self.file = File(path)

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

    def __init__(self, whitelist=None):
        self.whitelist = whitelist
        if os.geteuid() == 0:
            self.containers = {}
            self._docker_ps(tail=True)
            self.poll = self._poll_file
        else:
            try:
                output_of("docker", "ps")
                self.poll = self._poll_docker
            except ValueError:
                self.poll = lambda since: iter(())

    def _real_name(self, name, num):
        return name.removesuffix("-" + num).removesuffix("_" + num) if num else name

    def _docker_ps(self, tail=False):
        try:
            containers = os.listdir("/var/lib/docker/containers/")
        except FileNotFoundError:
            return
        for container in containers:
            if container in self.containers:
                continue

            if os.path.exists(f"/var/lib/docker/containers/{container}/{container}-json.log"):
                with contextlib.suppress(json.JSONDecodeError, KeyError, FileNotFoundError):
                    with open(f"/var/lib/docker/containers/{container}/config.v2.json") as f:
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
                        File(
                            f"/var/lib/docker/containers/{container}/{container}-json.log",
                            tail,
                        ),
                    )

    def _poll_file(self, since):
        self._docker_ps(tail=False)
        for name, file in self.containers.values():
            yield name, None  # tick
            for line in file.poll():
                with contextlib.suppress(json.JSONDecodeError, KeyError):
                    yield name, json.loads(line)["log"]

    def _poll_docker(self, since):
        for name_num in output_of(
            "docker",
            "ps",
            '--format={{.Names}},{{.Label "' + self.CONTAINER_NUMBER + '"}}',
        ).splitlines():
            full_name, num = name_num.split(",")
            name = self._real_name(full_name, num)
            if self.whitelist and name not in self.whitelist:
                continue
            yield name, None  # tick
            try:
                output = output_of("docker", "logs", "--since", since.isoformat(), full_name)
            except ValueError:
                # Container might be not running at this point
                continue
            for line in output.splitlines():
                yield name, line


class LineCounter:
    def __init__(self, poller, error_pattern, line_pattern):
        self.line_re = re.compile(line_pattern)
        self.error_re = re.compile(error_pattern)
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
                if self.error_re.search(line):
                    stat["error_count"] += 1
                    stat["errors"].append(line)
        return stats.values()


@cache
def get_oom():
    return OOM()


@cache
def get_logfiles(fail=False):
    logfiles = []
    for entry in config().get("logs", []):
        try:
            logfiles.append(
                LineCounter(
                    Logfile(
                        name=entry["name"],
                        path=entry["path"],
                    ),
                    error_pattern=entry.get("error_pattern", ".*"),
                    line_pattern=entry.get("line_pattern", ".*"),
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
            logfiles.append(
                LineCounter(
                    Docker(entry.get("containers")),
                    error_pattern=entry.get("error_pattern", ".*"),
                    line_pattern=entry.get("line_pattern", ".*"),
                )
            )
        except Exception as exc:
            if fail:
                raise ValueError(f"Invalid docker-log configuration {entry}: {exc!r}") from exc
            log.error(f"Invalid docker-log configuration {entry}", exc_info=True)
    return logfiles


def init_logs(fail=False):
    _ = get_oom()
    _ = get_logfiles(fail)
    _ = get_dockerlogs(fail)


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
