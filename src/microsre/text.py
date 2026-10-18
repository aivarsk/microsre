import contextlib
import itertools

from microsre.metrics import names

_UNITS = [("G", 1024 * 1024 * 1024), ("M", 1024 * 1024), ("K", 1_024), (" ", 1)]
_PSUNITS = [("GB/s", 1_000_000_000), ("MB/s", 1_000_000), ("KB/s", 1_000), ("B/s ", 1)]


def format_bytes(value):
    if value == 0:
        return "    0 B"
    for unit, factor in _UNITS:
        if value >= factor:
            result = value / factor
            return f"{result:>5.1f}{unit}"
    return f"{value:>5.1f} "


def format_bps(value):
    if value == 0:
        return "    0 B/s "
    for unit, factor in _PSUNITS:
        if value >= factor:
            result = value / factor
            return f"{result:>5.1f} {unit}"
    return f"{value:>5.1f} B/s "


def format_uptime(seconds):
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    time_part = f"{hours}:{minutes:02d}:{secs:02d}"

    if days == 0:
        return time_part
    if days == 1:
        return f"1 day, {time_part}"
    return f"{days} days, {time_part}"


def _format_pct(value):
    return "-" if value is None else int(value)


def _print_pressure(raw, what, key):
    def fmt(n):
        return f"{n:>4.1f}"

    with contextlib.suppress(KeyError):
        print(
            what,
            " some",
            fmt(raw[key]["some"]["avg10"]),
            fmt(raw[key]["some"]["avg60"]),
            fmt(raw[key]["some"]["avg300"]),
            " full",
            fmt(raw[key]["full"]["avg10"]),
            fmt(raw[key]["full"]["avg60"]),
            fmt(raw[key]["full"]["avg300"]),
        )


def dashboard(metrics, raw):
    print()
    print("Timestamp:", metrics["timestamp"], "=" * 40)

    print(
        "Uptime:",
        format_uptime(metrics["uptime"]),
        "Load:",
        raw["proc.loadavg"]["1min"],
        raw["proc.loadavg"]["5min"],
        raw["proc.loadavg"]["15min"],
        "RunQ:",
        metrics["cpu.run_queue"],
        f"({metrics['cpu.run_queue_per_cpu']}%/cpu)",
    )

    _print_pressure(raw, "CPU pressure:", "proc.pressure.cpu")
    print(
        "CPU %util",
        metrics["cpu.utilization_pct"],
        "%iowait",
        metrics["cpu.iowait_pct"],
        "%steal",
        metrics["cpu.steal_pct"],
        "  Max",
        metrics["cpu.max_utilization_core"],
        "%util",
        metrics["cpu.max_utilization_pct"],
    )

    _print_pressure(raw, "Memory pressure:", "proc.pressure.memory")
    print(
        "Memory %ram",
        metrics["mem.utilization_pct"],
        "%swap",
        metrics["mem.swap_utilization_pct"],
    )

    _print_pressure(raw, "Disk pressure:", "proc.pressure.io")
    print("Disk space %util", metrics["disk.space_usage_pct"])
    width = max((len(x) for x in raw["proc.mountinfo"]), default=0) + 2
    for k, v in raw["proc.mountinfo"].items():
        print(
            f"{k:>{width}}:",
            "%util",
            int(v["bytes_used"] * 100 / v["bytes_total"]),
            "free",
            format_bytes(v["bytes_available"]),
            "reserved",
            format_bytes(v["bytes_reserved"]),
        )

    print("Disk IO")
    devices = names(metrics["disk.list"])
    width = max(len(x) for x in itertools.chain(devices, ["total"])) + 2

    def format_iops(n):
        return f"{n:>5.1f} IOPS"

    for device in sorted(devices):
        print(
            f"{device:>{width}}:",
            "Read",
            format_iops(metrics[f"disk.{device}.read_iops"]),
            format_bps(metrics[f"disk.{device}.read_bytes_per_sec"]),
            "Write",
            format_iops(metrics[f"disk.{device}.write_iops"]),
            format_bps(metrics[f"disk.{device}.write_bytes_per_sec"]),
            "%util",
            _format_pct(metrics[f"disk.{device}.utilization_pct"]),
            "qdepth",
            metrics[f"disk.{device}.queue_depth"],
        )
    print(
        f"{'Total':>{width}}:",
        "Read",
        format_iops(metrics["disk.total.read_iops"]),
        format_bps(metrics["disk.total.read_bytes_per_sec"]),
        "Write",
        format_iops(metrics["disk.total.write_iops"]),
        format_bps(metrics["disk.total.write_bytes_per_sec"]),
    )

    print("Network")
    ifaces = names(metrics["net.list"])
    width = max(len(x) for x in itertools.chain(ifaces, ["total"])) + 2
    for iface in sorted(ifaces):
        print(
            f"{iface:>{width}}:",
            "tx",
            format_bps(metrics[f"net.{iface}.tx_bytes_per_sec"]),
            "rx",
            format_bps(metrics[f"net.{iface}.rx_bytes_per_sec"]),
            "%util",
            _format_pct(metrics[f"net.{iface}.utilization_pct"]),
        )
    print(
        f"{'Total':>{width}}:",
        "tx",
        format_bps(metrics["net.total.tx_bytes_per_sec"]),
        "rx",
        format_bps(metrics["net.total.rx_bytes_per_sec"]),
    )
    print(
        f"{'Total':>{width}}:",
        "#errs",
        metrics["net.total.errs_per_sec"],
        "#drop",
        metrics["net.total.drop_per_sec"],
        "retrans",
        metrics["net.tcp.retrans_per_sec"],
        "%ratio",
        metrics["net.tcp.retrans_ratio_pct"],
        "listenovf",
        metrics["net.tcp.listen_overflows"],
    )

    for oom in raw.get("oom", []):
        print(f"!!! OOM killed PID={oom['pid']} name={oom['comm']}")

    print("Logs")
    log_sources = itertools.chain(names(metrics["logs.list"]), names(metrics["docker-logs.list"]))
    width = max((len(x) for x in log_sources), default=0) + 2
    for logs in ("logs", "docker-logs"):
        for name in names(metrics[f"{logs}.list"]):
            print(
                f"{name:>{width}}: ",
                "err",
                metrics[f"{logs}.{name}.error_count"],
                "%err",
                metrics[f"{logs}.{name}.error_pct"],
                "err sample",
                repr(metrics[f"{logs}.{name}.errors"]),
            )
