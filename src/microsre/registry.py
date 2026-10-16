import re


def _pressure(prefix):
    return tuple(
        f"{prefix}.pressure.{tag}.{avg}{suffix}"
        for tag in ("some", "full")
        for avg in ("avg10", "avg60")
        for suffix in ("", ".previous")
    )


# Single source of truth for every metric the product knows about.
# (family, display, names, prom, source, fields, alertable)
#   names - literal internal metric names, feed alert.py KNOWN and prom HELP
#   prom  - labeled-family prom base names (without the microsre_ prefix)
# Keep display/fields in sync with docs/readme.html via generate_docs.py.
ROWS = (
    (
        "Memory",
        "mem.utilization_pct",
        ("mem.utilization_pct",),
        (),
        "/proc/meminfo",
        "MemTotal, MemAvailable (used = total - available)",
        True,
    ),
    (
        "Memory",
        "mem.swap_utilization_pct",
        ("mem.swap_utilization_pct",),
        (),
        "/proc/meminfo",
        "SwapTotal, SwapFree",
        True,
    ),
    (
        "Memory",
        "mem.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("mem"),
        (),
        "/proc/pressure/memory",
        "some / full line, avg10 / avg60",
        True,
    ),
    #
    (
        "CPU",
        "cpu.utilization_pct, cpu.iowait_pct, cpu.steal_pct",
        ("cpu.utilization_pct", "cpu.iowait_pct", "cpu.steal_pct"),
        (),
        "/proc/stat",
        "cpu line - user, nice, system, idle, iowait, irq, softirq, steal ticks, diffed over the interval",
        True,
    ),
    (
        "CPU",
        "cpu.max_utilization_pct, cpu.max_utilization_core, cpu.utilization_per_core",
        ("cpu.max_utilization_pct", "cpu.max_utilization_core", "cpu.utilization_per_core"),
        (),
        "/proc/stat",
        "cpuN per-core lines",
        True,
    ),
    (
        "CPU",
        "cpu.loadavg.1min (+ .previous)",
        ("cpu.loadavg.1min", "cpu.loadavg.1min.previous"),
        (),
        "/proc/loadavg",
        "1 min field",
        True,
    ),
    (
        "CPU",
        "cpu.run_queue, cpu.run_queue_per_cpu",
        ("cpu.run_queue", "cpu.run_queue_per_cpu"),
        (),
        "/proc/stat",
        "procs_running, max of 5 samples 50ms apart; per_cpu is a pct of cpu.count",
        True,
    ),
    (
        "CPU",
        "cpu.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("cpu"),
        (),
        "/proc/pressure/cpu",
        "some / full line, avg10 / avg60",
        True,
    ),
    #
    (
        "Disk",
        "disk.space_usage_pct",
        ("disk.space_usage_pct",),
        (),
        "/proc/self/mountinfo",
        "per-mount statvfs bytes used / total; each device counted once",
        True,
    ),
    (
        "Disk",
        "disk.max.utilization_pct, disk.max.await_ms, disk.max.queue_depth, disk.max.device",
        ("disk.max.utilization_pct", "disk.max.await_ms", "disk.max.queue_depth", "disk.max.device"),
        (),
        "/proc/diskstats",
        "per device: time_in_progress diff; await = (time_reading + time_writing) / (reads + writes); queue depth = in_progress gauge; stacked dm/md deduplicated to the backing disk",
        True,
    ),
    (
        "Disk",
        "disk.total.{read,write}_bytes_per_sec",
        ("disk.total.read_bytes_per_sec", "disk.total.write_bytes_per_sec"),
        (),
        "/proc/diskstats",
        "sectors_read / sectors_written x sector size",
        True,
    ),
    (
        "Disk",
        "disk.total.{read,write}_iops",
        ("disk.total.read_iops", "disk.total.write_iops"),
        (),
        "/proc/diskstats",
        "reads / writes completed",
        True,
    ),
    (
        "Disk",
        "disk.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("disk"),
        (),
        "/proc/pressure/io",
        "some / full line, avg10 / avg60",
        True,
    ),
    (
        "Disk",
        "disk.<device>.*",
        (),
        (
            "disk_read_bytes_per_sec",
            "disk_write_bytes_per_sec",
            "disk_read_iops",
            "disk_write_iops",
            "disk_await_ms",
            "disk_utilization_pct",
            "disk_weighted_utilization_pct",
            "disk_queue_depth",
        ),
        "/proc/diskstats",
        "per-device counters, diffs over the check interval",
        False,
    ),
    #
    (
        "Network",
        "net.total.{rx,tx}_bytes_per_sec",
        ("net.total.rx_bytes_per_sec", "net.total.tx_bytes_per_sec"),
        (),
        "/proc/net/dev",
        "sum of per-interface rx_bytes / tx_bytes",
        True,
    ),
    (
        "Network",
        "net.total.errs_per_sec, net.total.drop_per_sec",
        ("net.total.errs_per_sec", "net.total.drop_per_sec"),
        (),
        "/proc/net/dev",
        "rx_errs + tx_errs, rx_drop + tx_drop",
        True,
    ),
    (
        "Network",
        "net.tcp.retrans_per_sec, net.tcp.retrans_ratio_pct",
        ("net.tcp.retrans_per_sec", "net.tcp.retrans_ratio_pct"),
        (),
        "/proc/net/snmp",
        "Tcp: - RetransSegs, OutSegs",
        True,
    ),
    (
        "Network",
        "net.tcp.listen_drops, net.tcp.listen_overflows",
        ("net.tcp.listen_drops", "net.tcp.listen_overflows"),
        (),
        "/proc/net/netstat",
        "TcpExt: - ListenDrops, ListenOverflows",
        True,
    ),
    (
        "Network",
        "net.max.utilization_pct, net.max.utilization_iface",
        ("net.max.utilization_pct", "net.max.utilization_iface"),
        (),
        "/proc/net/dev + /sys/class/net/<iface>/speed",
        "max of rx/tx bytes x8 as % of link speed",
        True,
    ),
    (
        "Network",
        "net.max.{rx,tx}_bytes_per_sec, net.max.{rx,tx}_iface",
        ("net.max.rx_bytes_per_sec", "net.max.rx_iface", "net.max.tx_bytes_per_sec", "net.max.tx_iface"),
        (),
        "/proc/net/dev",
        "busiest interface by bytes",
        True,
    ),
    (
        "Network",
        "net.<iface>.*",
        (),
        (
            "net_rx_bytes_per_sec",
            "net_tx_bytes_per_sec",
            "net_rx_packets_per_sec",
            "net_tx_packets_per_sec",
            "net_rx_errs_per_sec",
            "net_tx_errs_per_sec",
            "net_rx_drop_per_sec",
            "net_tx_drop_per_sec",
            "net_utilization_pct",
        ),
        "/proc/net/dev",
        "per-interface counters, diffs over the check interval",
        False,
    ),
    #
    (
        "OOM kills",
        "oom.count, oom.victims",
        ("oom.count", "oom.victims"),
        (),
        "kernel log (auto-detected, no config)",
        "OOM killer events",
        True,
    ),
    #
    (
        "Log sources",
        "logs.<source>.*, docker-logs.<source>.* (per configured log source)",
        (),
        (),
        "the configured log file or container JSON logs",
        "line_count / error_count / error_pct / errors - lines matching line_pattern / error_pattern",
        False,
    ),
    #
    (
        "Collection",
        "collect.procfs_seconds, collect.proc_pid_seconds",
        ("collect.procfs_seconds", "collect.proc_pid_seconds"),
        (),
        "self-measured, time.monotonic_ns() around the collectors",
        "procfs collection time per tick; proc_pid is the /proc/<pid> scan portion of it",
        True,
    ),
    (
        "Collection",
        "collect.logs_seconds, collect.docker_logs_seconds, collect.oom_seconds",
        ("collect.logs_seconds", "collect.docker_logs_seconds", "collect.oom_seconds"),
        (),
        "self-measured, time.monotonic_ns() around the collectors",
        "log scanning time per tick",
        True,
    ),
    (
        "Collection",
        "self.cpu_seconds, self.cpu_pct, self.rss_bytes, self.read_bytes_per_sec, self.ctx_switches_per_sec",
        (
            "self.cpu_seconds",
            "self.cpu_pct",
            "self.rss_bytes",
            "self.read_bytes_per_sec",
            "self.ctx_switches_per_sec",
        ),
        (),
        "self-measured, /proc/self/stat, /proc/self/io, /proc/self/status",
        "microsre itself: total CPU seconds, CPU pct last interval, RSS, storage reads caused, wakeups per second",
        True,
    ),
)


_PROM_NAME = re.compile(r"[^a-zA-Z0-9_:]")


def to_prom(name):
    return "microsre_" + _PROM_NAME.sub("_", name)


KNOWN = frozenset(name for _, _, names, _, _, _, alertable in ROWS if alertable for name in names) | frozenset(
    ("timestamp", "hostname", "uptime")
)

HELP = {
    to_prom(name): f"{fields} (source: {source})"
    for _, _, names, prom, source, fields, _ in ROWS
    for name in names + prom
}

# Known caveats per metric family, rendered after the metrics table by generate_docs.py
CAVEATS = (
    (
        "CPU",
        "cpu.utilization_pct aggregates all cores - one hot core on a many-core host is hidden. Watch cpu.max_utilization_pct and cpu.utilization_per_core.",
    ),
    (
        "CPU",
        "cpu.run_queue is a burst sample (max of 5 reads 50ms apart), not a continuous measurement; sub-50ms spikes can slip through.",
    ),
    (
        "Memory",
        "MemAvailable is a kernel heuristic about reclaimable memory, not a measurement of what applications can actually allocate.",
    ),
    (
        "Pressure",
        "PSI avg10 is a windowed average of a windowed signal - it lags real stalls and smooths over short bursts (avg60 more so).",
    ),
    (
        "Disk",
        "Stacked device-mapper/md devices are deduplicated - I/O is attributed to the backing disk; mapper-level views are not reported.",
    ),
    (
        "Disk",
        "Rates and percentages are counter diffs over the measured interval; a suspend or counter reset distorts one interval (clamped to 0, not fatal).",
    ),
    (
        "Network",
        "net.tcp.retrans_ratio_pct is meaningless while TcpOutSegs is near zero; net.tcp.listen_* need /proc/net/netstat, absent in some containers.",
    ),
)
