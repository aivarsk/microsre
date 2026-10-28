import contextlib
import datetime
import os
import re
import socket
import struct
import time
from typing import Any


def _read_bytes(path, max_size=256 * 1024):
    fd = os.open(path, os.O_RDONLY)
    try:
        b = os.read(fd, max_size)
        if len(b) == max_size:
            return _read_bytes(path, max_size=max_size * 4)
        return b
    finally:
        os.close(fd)


def _primary_ip():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s, contextlib.suppress(OSError):
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    return "127.0.0.1"


def _hostname(ip):
    with contextlib.suppress(socket.herror, socket.gaierror, OSError):
        return socket.gethostbyaddr(ip)[0]
    return socket.gethostname()


def _read_cpuinfo():
    count, model = 0, b""
    for line in _read_bytes("/proc/cpuinfo").splitlines():
        if line.startswith(b"processor"):
            count += 1
        elif line.startswith(b"model name"):
            model = line.split(b": ")[1]
    yield "proc.cpuinfo.model_name", model.decode()
    yield "cpu.count", _online_cpu_count() or count


_CPU_DIR = re.compile(r"cpu\d+")


def _online_cpu_count():
    count = 0
    with contextlib.suppress(OSError):
        for name in os.listdir("/sys/devices/system/cpu"):
            if not _CPU_DIR.fullmatch(name):
                continue
            online = b"1"
            with contextlib.suppress(OSError):
                online = _read_bytes(f"/sys/devices/system/cpu/{name}/online")
            if online.strip() == b"0":
                continue
            count += 1
    return count


_ESCAPED = re.compile(rb"\\([0-7]{3}|\\)")


def _unescape(field):
    # mountinfo escapes " ", "\t", "\n" and "\\" as octal / double backslash
    return _ESCAPED.sub(lambda m: bytes([int(m.group(1), 8)]) if len(m.group(1)) == 3 else b"\\", field)


def _read_mountinfo():
    physical_fstypes = set()
    with open("/proc/filesystems", "rb") as f:
        for line in f:
            fields = line.split(maxsplit=1)
            if len(fields) == 1:
                physical_fstypes.add(fields[0])

    blacklist = (b"/snap", b"/var/snap", b"/boot")
    with open("/proc/self/mountinfo", "rb") as f:
        for line in f:
            fields = line.split()
            if len(fields) < 10:
                continue

            mount_point = _unescape(fields[4])
            try:
                hyphen_idx = fields.index(b"-")
                fs_type = fields[hyphen_idx + 1]
            except (ValueError, IndexError):
                continue

            # Filter out unwanted pseudo-filesystems (like cgroup, autofs, configs)
            if fs_type not in physical_fstypes or any(mount_point.startswith(prefix) for prefix in blacklist):
                continue

            with contextlib.suppress(FileNotFoundError, PermissionError):
                st = os.statvfs(mount_point)
                device = os.stat(mount_point).st_dev
                bytes_total = st.f_blocks * st.f_frsize
                bytes_available = st.f_bavail * st.f_frsize
                bytes_reserved = (st.f_bfree - st.f_bavail) * st.f_frsize

                yield (
                    mount_point.decode(),
                    {
                        "device": device,
                        "fs_type": fs_type.decode(),
                        "bytes_available": bytes_available,
                        "bytes_used": bytes_total - bytes_available,
                        "bytes_reserved": bytes_reserved,
                        "bytes_total": bytes_total,
                    },
                )


def _read_proc_pid():
    for name in os.listdir("/proc"):
        try:
            pid = int(name)
        except ValueError:
            continue

        prefix = f"/proc/{pid}/"
        proc: dict[str, Any] = {}

        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            for line in _read_bytes(prefix + "status").splitlines():
                if line.startswith(b"Uid:"):
                    proc["uid"] = int(line.split()[1])
                elif line.startswith(b"voluntary_ctxt_switches"):
                    proc["voluntary_ctxt_switches"] = int(line.split()[1])
                elif line.startswith(b"nonvoluntary_ctxt_switches:"):
                    proc["nonvoluntary_ctxt_switches"] = int(line.split()[1])

            proc["cmdline"] = _read_bytes(prefix + "cmdline").replace(b"\x00", b" ").decode()
            proc["cgroup"] = _read_bytes(prefix + "cgroup").strip().decode()
            proc["oom_score"] = int(_read_bytes(prefix + "oom_score"))

            raw = _read_bytes(prefix + "stat")

            _, rest = raw.split(b"(", 1)
            # TASK_COMM_LEN=16 but `pool_workqueue_release` is larger than 16
            for i in range(64, -1, -1):
                if rest[i : i + 2] == b") ":
                    comm = rest[:i]
                    rest = rest[i + 1 :]
                    break
            else:
                continue

            fields = rest.split()

            proc["comm"] = comm.decode()
            proc["state"] = fields[0].decode()  # 3
            proc["ppid"] = int(fields[1])
            proc["utime"] = int(fields[11])
            proc["stime"] = int(fields[12])
            proc["num_threads"] = int(fields[17])
            proc["starttime"] = int(fields[19])
            proc["vsize"] = int(fields[20])
            proc["rss"] = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
            proc["processor"] = int(fields[36])
            proc["delayacct_blkio_ticks"] = int(fields[39])

        with contextlib.suppress(FileNotFoundError, PermissionError, ProcessLookupError):
            for line in _read_bytes(prefix + "io").splitlines():
                if line.startswith(b"read_bytes:"):
                    proc["read_bytes"] = int(line.split()[1])
                elif line.startswith(b"write_bytes:"):
                    proc["write_bytes"] = int(line.split()[1])

        with contextlib.suppress(FileNotFoundError, PermissionError, ProcessLookupError):
            # CONFIG_SCHEDSTATS
            raw = _read_bytes(prefix + "schedstat")
            fields = raw.split()
            proc["sum_exec_runtime"] = int(fields[0])
            proc["wait_time"] = int(fields[1])
            proc["timeslices"] = int(fields[2])

        proc["sockets"] = sockets = []
        proc["files"] = files = []
        proc["fd_count"] = 0
        with contextlib.suppress(FileNotFoundError, PermissionError, ProcessLookupError):
            fdprefix = prefix + "fd/"
            for fd in os.listdir(fdprefix):
                proc["fd_count"] += 1
                # Might be gone by now
                with contextlib.suppress(FileNotFoundError):
                    link = os.readlink(fdprefix + fd)
                    if link.startswith("socket:["):
                        sockets.append(link[8:-1])
                    elif link.startswith("pipe:") or link.startswith("anon_inode:"):
                        pass
                    else:
                        files.append(link)

        yield pid, proc


def _read_uptime():
    fields = _read_bytes("/proc/uptime").split()
    yield "seconds", float(fields[0])
    yield "idle_seconds", float(fields[1])


def uptime_seconds():
    return dict(_read_uptime())["seconds"]


def _read_loadavg():
    fields = _read_bytes("/proc/loadavg").split()
    yield "1min", float(fields[0])
    yield "5min", float(fields[1])
    yield "15min", float(fields[2])
    tasks = fields[3].split(b"/")
    yield "runnable", int(tasks[0])
    yield "total_tasks", int(tasks[1])
    yield "last_pid", int(fields[4])


def _read_pressure(resource):
    # kernel < 4.20 or CONFIG_PSI=n
    with contextlib.suppress(FileNotFoundError):
        for line in _read_bytes(f"/proc/pressure/{resource}").splitlines():
            what, *values = line.split()
            avg10, avg60, avg300, total = (v.split(b"=")[1] for v in values)
            yield (
                what.decode(),
                {
                    "avg10": float(avg10),
                    "avg60": float(avg60),
                    "avg300": float(avg300),
                    "total": int(total),
                },
            )


def _read_meminfo():
    for line in _read_bytes("/proc/meminfo").splitlines():
        name, value = line.split(b": ")
        if name in {
            b"MemTotal",
            b"MemAvailable",
            b"MemFree",
            b"Buffers",
            b"Cached",
            b"SwapTotal",
            b"SwapFree",
        }:
            yield name.decode(), int(value.split()[0]) * 1024


def _read_stat():
    for line in _read_bytes("/proc/stat").splitlines():
        name, rest = line.split(maxsplit=1)
        if name.startswith(b"cpu"):
            fields = [int(x) for x in rest.split(maxsplit=9)[:8]]
            yield (
                name.decode(),
                dict(zip(("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"), fields)),
            )
        elif name in (b"ctxt", b"procs_running", b"procs_blocked"):
            yield name.decode(), int(rest)


def _read_vmstat():
    for line in _read_bytes("/proc/vmstat").splitlines():
        name, _, value = line.partition(b" ")
        if name in (b"pswpin", b"pswpout", b"pgmajfault", b"pgscan_kswapd", b"pgscan_direct") or name.startswith(
            b"allocstall_"
        ):
            yield name.decode(), int(value)


_PARTITION_SUFFIX = re.compile(r"(p?\d+)$")


def _is_stacked(slaves, devices):
    # device-mapper/md report the same I/O as their backing disks; partition names
    # resolve to the base disk (sda2 -> sda, nvme0n1p1 -> nvme0n1)
    return any(s in devices or _PARTITION_SUFFIX.sub("", s) in devices for s in slaves)


def _read_diskstats():
    devices = {}
    for line in _read_bytes("/proc/diskstats").splitlines():
        (
            major,
            minor,
            name,
            *fields,
        ) = line.split()
        if major == b"7" or minor != b"0":
            continue
        devices[name.decode()] = fields

    for name, fields in devices.items():
        with contextlib.suppress(OSError):
            if _is_stacked(os.listdir(f"/sys/block/{name}/slaves"), devices):
                continue
        yield (
            name,
            {
                "reads": int(fields[0]),
                "sectors_read": int(fields[2]),
                "time_reading": int(fields[3]),
                "writes": int(fields[4]),
                "sectors_written": int(fields[6]),
                "time_writing": int(fields[7]),
                "in_progress": int(fields[8]),
                "time_in_progress": int(fields[9]),
                "weighted_time_in_progress": int(fields[10]),
            },
        )


def _read_net_dev():
    for line in _read_bytes("/proc/net/dev").splitlines()[2:]:
        iface, *fields = line.split()
        if iface in {b"lo:", b"docker0:"} or iface.startswith(b"veth") or iface.startswith(b"br-"):
            continue

        iface = iface[:-1].decode()

        speed = None
        with contextlib.suppress(OSError, ValueError):
            speed = int(_read_bytes(f"/sys/class/net/{iface}/speed").strip())
            if speed <= 0 or speed == 65535:
                speed = None

        yield (
            iface,
            {
                "rx_bytes": int(fields[0]),
                "rx_packets": int(fields[1]),
                "rx_errs": int(fields[2]),
                "rx_drop": int(fields[3]),
                # "rx_fifo": int(fields[4]),
                # "rx_frame": int(fields[5]),
                # "rx_compressed": int(fields[6]),
                # "rx_multicast": int(fields[7]),
                "tx_bytes": int(fields[8]),
                "tx_packets": int(fields[9]),
                "tx_errs": int(fields[10]),
                "tx_drop": int(fields[11]),
                # "tx_fifo": int(fields[12]),
                # "tx_colls": int(fields[13]),
                # "tx_carrier": int(fields[14]),
                # "tx_compressed": int(fields[15]),
                "speed": speed,
            },
        )


def _read_net_kv(path, prefix):
    labels = None
    for line in _read_bytes(path).splitlines():
        if not line.startswith(prefix):
            continue
        if labels is None:
            labels = line.split()
        else:
            values = line.split()
            for label, value in zip(labels[1:], values[1:]):
                yield prefix[:-1].decode() + label.decode(), int(value)
            break


def _parse_addr(hex):
    addr, port = hex.split(b":")
    return socket.inet_ntoa(struct.pack("<I", int(addr, 16))) + ":" + str(int(port, 16))


_TCP_STATES = {
    0x01: "ESTABLISHED",
    0x02: "SYN_SENT",
    0x03: "SYN_RECV",
    0x04: "FIN_WAIT1",
    0x05: "FIN_WAIT2",
    0x06: "TIME_WAIT",
    0x07: "CLOSE",
    0x08: "CLOSE_WAIT",
    0x09: "LAST_ACK",
    0x0A: "LISTEN",
    0x0B: "CLOSING",
    0x0C: "NEW_SYN_RECV",
}


def _read_net_tcp():
    for line in _read_bytes("/proc/net/tcp").splitlines()[1:]:
        fields = line.split()
        queues = fields[4].split(b":")
        tr = fields[5].split(b":")

        yield (
            int(fields[9]),
            {
                "local_address": _parse_addr(fields[1]),
                "rem_address": _parse_addr(fields[2]),
                "state": int(fields[3], 16),
                "state_name": _TCP_STATES.get(int(fields[3], 16)),
                "tx_queue": int(queues[0], 16),
                "rx_queue": int(queues[1], 16),
                "timer_active": int(tr[0], 16),
                "jiffies_until": int(tr[1], 16),
                "retrnsmt": int(fields[6], 16),
                "uid": int(fields[7]),
                "timeout": int(fields[8]),
            },
        )


def _read_self():
    stat = _read_bytes("/proc/self/stat")
    fields = stat[stat.rfind(b") ") + 2 :].split()
    io = dict(line.split(b":", 1) for line in _read_bytes("/proc/self/io").splitlines())
    status = dict(line.split(b":", 1) for line in _read_bytes("/proc/self/status").splitlines())
    return {
        "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        "read_bytes": int(io[b"read_bytes"]),
        "rss_bytes": int(status[b"VmRSS"].split()[0]) * 1024,
        "voluntary_ctxt_switches": int(status[b"voluntary_ctxt_switches"]),
    }


def collect_procfs(now):
    start = time.monotonic_ns()

    raw: dict[str, Any] = {}
    raw["timestamp"] = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat()
    raw["ip"] = _primary_ip()
    raw["hostname"] = _hostname(raw["ip"])

    raw.update(_read_cpuinfo())
    raw["proc.stat"] = dict(_read_stat())
    raw["proc.uptime"] = dict(_read_uptime())
    raw["proc.loadavg"] = dict(_read_loadavg())
    raw["proc.pressure.cpu"] = dict(_read_pressure("cpu"))
    raw["proc.pressure.memory"] = dict(_read_pressure("memory"))
    raw["proc.pressure.io"] = dict(_read_pressure("io"))
    raw["proc.meminfo"] = dict(_read_meminfo())
    raw["proc.vmstat"] = dict(_read_vmstat())
    raw["proc.diskstats"] = dict(_read_diskstats())
    raw["proc.net.dev"] = dict(_read_net_dev())
    raw["proc.net.snmp"] = dict(_read_net_kv("/proc/net/snmp", b"Tcp:"))
    raw["proc.net.netstat"] = dict(_read_net_kv("/proc/net/netstat", b"TcpExt:"))
    raw["proc.mountinfo"] = dict(_read_mountinfo())
    raw["proc.net.tcp"] = dict(_read_net_tcp())
    raw["self"] = _read_self()

    # Depends on number of processes and open files
    start_pid = time.monotonic_ns()
    raw["proc.pid"] = dict(_read_proc_pid())
    raw["procfs_pid_duration"] = time.monotonic_ns() - start_pid
    raw["procfs_duration"] = time.monotonic_ns() - start

    return raw


def collect_burst_procfs():
    return {
        "stat": dict(_read_stat()),
        "diskstats": dict(_read_diskstats()),
        "net.dev": dict(_read_net_dev()),
        "net.snmp": dict(_read_net_kv("/proc/net/snmp", b"Tcp:")),
        "net.netstat": dict(_read_net_kv("/proc/net/netstat", b"TcpExt:")),
        "vmstat": dict(_read_vmstat()),
        "pressure.cpu": dict(_read_pressure("cpu")),
        "pressure.memory": dict(_read_pressure("memory")),
        "pressure.io": dict(_read_pressure("io")),
    }
