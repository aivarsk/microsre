import re


def _pressure(prefix):
    return tuple(
        f"{prefix}.pressure.{tag}.{avg}{suffix}"
        for tag in ("some", "full")
        for avg in ("avg10", "avg60")
        for suffix in ("", ".previous")
    )


# Single source of truth for every metric the product knows about.
# (family, display, names, prom, source, fields, alertable, semantics)
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
        "instantaneous",
    ),
    (
        "Memory",
        "mem.swap_utilization_pct",
        ("mem.swap_utilization_pct",),
        (),
        "/proc/meminfo",
        "SwapTotal, SwapFree",
        True,
        "instantaneous",
    ),
    (
        "Memory",
        "mem.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("mem"),
        (),
        "/proc/pressure/memory",
        "some / full line, avg10 / avg60",
        True,
        "instantaneous (kernel-windowed)",
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
        "integral",
    ),
    (
        "CPU",
        "cpu.max_utilization_pct, cpu.max_utilization_core, cpu.<core>.utilization_pct",
        ("cpu.max_utilization_pct", "cpu.max_utilization_core"),
        ("cpu_utilization_per_core",),
        "/proc/stat",
        "cpuN per-core lines",
        True,
        "integral",
    ),
    (
        "CPU",
        "cpu.loadavg.1min (+ .previous)",
        ("cpu.loadavg.1min", "cpu.loadavg.1min.previous"),
        (),
        "/proc/loadavg",
        "1 min field",
        True,
        "instantaneous (kernel-decayed)",
    ),
    (
        "CPU",
        "cpu.run_queue, cpu.run_queue_per_cpu",
        ("cpu.run_queue", "cpu.run_queue_per_cpu"),
        (),
        "/proc/loadavg",
        "runnable field (self excluded), max of 5 samples 50ms apart; per_cpu is a pct of cpu.count",
        True,
        "instantaneous (burst max of 5)",
    ),
    (
        "CPU",
        "cpu.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("cpu"),
        (),
        "/proc/pressure/cpu",
        "some / full line, avg10 / avg60",
        True,
        "instantaneous (kernel-windowed)",
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
        "instantaneous",
    ),
    (
        "Disk",
        "disk.max.utilization_pct, disk.max.await_ms, disk.max.queue_depth, disk.max.device",
        ("disk.max.utilization_pct", "disk.max.await_ms", "disk.max.queue_depth", "disk.max.device"),
        (),
        "/proc/diskstats",
        "per device: time_in_progress diff; await = (time_reading + time_writing) / (reads + writes); queue depth = in_progress gauge; stacked dm/md deduplicated to the backing disk",
        True,
        "integral; instantaneous (queue depth)",
    ),
    (
        "Disk",
        "disk.total.{read,write}_bytes_per_sec",
        ("disk.total.read_bytes_per_sec", "disk.total.write_bytes_per_sec"),
        (),
        "/proc/diskstats",
        "sectors_read / sectors_written x 512-byte sectors (standard kernel units)",
        True,
        "integral",
    ),
    (
        "Disk",
        "disk.total.{read,write}_iops",
        ("disk.total.read_iops", "disk.total.write_iops"),
        (),
        "/proc/diskstats",
        "reads / writes completed",
        True,
        "counter",
    ),
    (
        "Disk",
        "disk.pressure.{some,full}.avg{10,60} (+ .previous)",
        _pressure("disk"),
        (),
        "/proc/pressure/io",
        "some / full line, avg10 / avg60",
        True,
        "instantaneous (kernel-windowed)",
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
        "integral; instantaneous (queue depth)",
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
        "integral",
    ),
    (
        "Network",
        "net.total.errs_per_sec, net.total.drop_per_sec",
        ("net.total.errs_per_sec", "net.total.drop_per_sec"),
        (),
        "/proc/net/dev",
        "rx_errs + tx_errs, rx_drop + tx_drop",
        True,
        "counter",
    ),
    (
        "Network",
        "net.tcp.retrans_per_sec, net.tcp.retrans_ratio_pct",
        ("net.tcp.retrans_per_sec", "net.tcp.retrans_ratio_pct"),
        (),
        "/proc/net/snmp",
        "Tcp: - RetransSegs, OutSegs",
        True,
        "counter",
    ),
    (
        "Network",
        "net.tcp.listen_drops, net.tcp.listen_overflows",
        ("net.tcp.listen_drops", "net.tcp.listen_overflows"),
        (),
        "/proc/net/netstat",
        "TcpExt: - ListenDrops, ListenOverflows",
        True,
        "counter",
    ),
    (
        "Network",
        "net.max.utilization_pct, net.max.utilization_iface",
        ("net.max.utilization_pct", "net.max.utilization_iface"),
        (),
        "/proc/net/dev + /sys/class/net/<iface>/speed",
        "max of rx/tx bytes x8 as % of link speed (unknown for virtual or link-down interfaces)",
        True,
        "integral",
    ),
    (
        "Network",
        "net.max.{rx,tx}_bytes_per_sec, net.max.{rx,tx}_iface",
        ("net.max.rx_bytes_per_sec", "net.max.rx_iface", "net.max.tx_bytes_per_sec", "net.max.tx_iface"),
        (),
        "/proc/net/dev",
        "busiest interface by bytes",
        True,
        "integral",
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
        "integral; counter (errs, drop)",
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
        "counter",
    ),
    #
    (
        "Log sources",
        "logs.<source>.*, docker-logs.<source>.* (per configured log source)",
        (),
        (),
        "the configured log file or container JSON logs",
        "line_count / error_count / error_pct / errors - lines matching line_pattern / error_pattern; with heartbeat_pattern configured: heartbeat_last_seen (unix time of last match, 0 = never), heartbeat_age_seconds (seconds since it, huge when never), heartbeat_status (pre-worded 'not seen yet' / 'not seen for N seconds' for templates)",
        False,
        "counter; gauge (heartbeat age)",
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
        "measured duration",
    ),
    (
        "Collection",
        "collect.logs_seconds, collect.docker_logs_seconds, collect.oom_seconds",
        ("collect.logs_seconds", "collect.docker_logs_seconds", "collect.oom_seconds"),
        (),
        "self-measured, time.monotonic_ns() around the collectors",
        "log scanning time per tick",
        True,
        "measured duration",
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
        "mixed: counter, integral, instantaneous",
    ),
)


_PROM_NAME = re.compile(r"[^a-zA-Z0-9_:]")


def to_prom(name):
    return "microsre_" + _PROM_NAME.sub("_", name)


KNOWN = frozenset(name for _, _, names, _, _, _, _, alertable in ROWS if alertable for name in names) | frozenset(
    ("timestamp", "hostname", "uptime")
)

HELP = {
    to_prom(name): f"{fields} (source: {source})"
    for _, _, names, prom, source, fields, _, _ in ROWS
    for name in names + prom
}

# Known caveats per metric family, rendered after the metrics table by generate_docs.py
CAVEATS = (
    (
        "CPU",
        "cpu.utilization_pct aggregates all cores - one hot core on a many-core host is hidden. Watch cpu.max_utilization_pct or the per-core cpu.<core>.utilization_pct.",
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
    (
        "Log sources",
        "The heartbeat last-seen time is persisted in state.sqlite and survives restarts. An edited heartbeat_pattern starts over ('not seen yet'), so a broken pattern surfaces within one interval instead of one threshold. A beat that landed while microsre was down for over ten minutes is missed and can alert once - it self-heals at the next beat.",
    ),
    (
        "CPU",
        "Guest CPU time is included in cpu.utilization_pct: it sits inside the /proc/stat user field, only 8 fields are read, so nothing double-counts. iostat reports the same way; mpstat subtracts guest for its %usr column.",
    ),
    (
        "Semantics",
        "Metrics mix three semantics: integrals (CPU time, bytes - a burst smears across the window average), counters (retransmits, OOM kills, error lines - exact count in the window, no intra-window timing), and instantaneous samples (run queue, PSI, gauges - true at tick time; PSI is a kernel-windowed average on top). Size thresholds for the class: a 5-second spike can hide inside a calm 60-second integral, a counter sees the count but not the timing, and an instantaneous sample can miss the spike altogether.",
    ),
)
