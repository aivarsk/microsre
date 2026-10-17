import contextlib
import time


def _pct(value, total):
    with contextlib.suppress(ZeroDivisionError):
        return int(value * 100 / total)
    return 0


def _delta(new, old):
    return max(0, new - old)


def _calculate_metrics_cpu(metrics, start, end, interval):
    for k, v in end["proc.stat"].items():
        if k.startswith("cpu") and k in start["proc.stat"]:
            o = start["proc.stat"][k]
            akeys = ("user", "nice", "system", "irq", "softirq", "steal")
            active = sum(_delta(v[i], o[i]) for i in akeys)
            tkeys = akeys + ("idle", "iowait")
            total = sum(_delta(v[i], o[i]) for i in tkeys)
            try:
                utilization = int(active * 100 / total)
            except ZeroDivisionError:
                utilization = 0
            if k == "cpu":
                metrics["cpu.utilization_pct"] = utilization
                try:
                    metrics["cpu.iowait_pct"] = int((v["iowait"] - o["iowait"]) * 100 / total)
                    metrics["cpu.steal_pct"] = int((v["steal"] - o["steal"]) * 100 / total)
                except ZeroDivisionError:
                    metrics["cpu.iowait_pct"] = 0
                    metrics["cpu.steal_pct"] = 0
            else:
                metrics.setdefault("cpu.utilization_per_core", {})[k] = utilization
                if utilization > metrics.get("cpu.max_utilization_pct", -1):
                    metrics["cpu.max_utilization_pct"] = utilization
                    metrics["cpu.max_utilization_core"] = k


def _calculate_metrics_net(metrics, start, end, interval):
    metrics.update(
        dict.fromkeys(
            (
                "net.total.errs_per_sec",
                "net.total.drop_per_sec",
                "net.total.rx_bytes_per_sec",
                "net.total.tx_bytes_per_sec",
                "net.max.rx_bytes_per_sec",
                "net.max.rx_iface",
                "net.max.tx_bytes_per_sec",
                "net.max.tx_iface",
                "net.max.utilization_pct",
            ),
            0,
        )
    )

    metrics["net.max.utilization_iface"] = None
    metrics["net.list"] = []

    for k, n in end["proc.net.dev"].items():
        if k not in start["proc.net.dev"]:
            continue
        o = start["proc.net.dev"][k]
        metrics["net.list"].append(k)

        for what in (
            "rx_bytes",
            "rx_packets",
            "rx_errs",
            "rx_drop",
            "tx_bytes",
            "tx_packets",
            "tx_errs",
            "tx_drop",
        ):
            metrics[f"net.{k}.{what}_per_sec"] = _delta(n[what], o[what]) / interval

        if n["speed"]:
            metrics[f"net.{k}.utilization_pct"] = int(
                max(
                    metrics[f"net.{k}.rx_bytes_per_sec"],
                    metrics[f"net.{k}.tx_bytes_per_sec"],
                )
                * 8
                * 100
                / (n["speed"] * 1_000_000)
            )
            if metrics[f"net.{k}.utilization_pct"] > metrics["net.max.utilization_pct"]:
                metrics["net.max.utilization_pct"] = metrics[f"net.{k}.utilization_pct"]
                metrics["net.max.utilization_iface"] = k
        else:
            metrics[f"net.{k}.utilization_pct"] = None

        if metrics[f"net.{k}.rx_bytes_per_sec"] > metrics["net.max.rx_bytes_per_sec"]:
            metrics["net.max.rx_bytes_per_sec"] = metrics[f"net.{k}.rx_bytes_per_sec"]
            metrics["net.max.rx_iface"] = k
        if metrics[f"net.{k}.tx_bytes_per_sec"] > metrics["net.max.tx_bytes_per_sec"]:
            metrics["net.max.tx_bytes_per_sec"] = metrics[f"net.{k}.tx_bytes_per_sec"]
            metrics["net.max.tx_iface"] = k

        metrics["net.total.rx_bytes_per_sec"] += _delta(n["rx_bytes"], o["rx_bytes"])
        metrics["net.total.tx_bytes_per_sec"] += _delta(n["tx_bytes"], o["tx_bytes"])
        metrics["net.total.errs_per_sec"] += _delta(n["rx_errs"], o["rx_errs"]) + _delta(n["tx_errs"], o["tx_errs"])
        metrics["net.total.drop_per_sec"] += _delta(n["rx_drop"], o["rx_drop"]) + _delta(n["tx_drop"], o["tx_drop"])

    metrics["net.total.errs_per_sec"] /= interval
    metrics["net.total.drop_per_sec"] /= interval
    metrics["net.total.rx_bytes_per_sec"] /= interval
    metrics["net.total.tx_bytes_per_sec"] /= interval

    metrics["net.tcp.retrans_per_sec"] = 0
    metrics["net.tcp.retrans_ratio_pct"] = 0
    with contextlib.suppress(KeyError):
        out = _delta(end["proc.net.snmp"]["TcpOutSegs"], start["proc.net.snmp"]["TcpOutSegs"])
        retrans = _delta(end["proc.net.snmp"]["TcpRetransSegs"], start["proc.net.snmp"]["TcpRetransSegs"])
        metrics["net.tcp.retrans_per_sec"] = retrans / interval
        metrics["net.tcp.retrans_ratio_pct"] = _pct(retrans, out)

    metrics["net.tcp.listen_drops"] = 0
    metrics["net.tcp.listen_overflows"] = 0
    with contextlib.suppress(KeyError):
        netstat, prev = end["proc.net.netstat"], start["proc.net.netstat"]
        metrics["net.tcp.listen_drops"] = _delta(netstat["TcpExtListenDrops"], prev["TcpExtListenDrops"])
        metrics["net.tcp.listen_overflows"] = _delta(netstat["TcpExtListenOverflows"], prev["TcpExtListenOverflows"])


def _calculate_metrics_disk(metrics, start, end, interval):
    counted_devices = set()
    bytes_used, bytes_total = 0, 0
    # Multiple mounts can share one device, count each device once
    for name, d in end["proc.mountinfo"].items():
        if d["device"] in counted_devices:
            continue
        counted_devices.add(d["device"])
        bytes_used += d["bytes_used"]
        bytes_total += d["bytes_total"]
    metrics["disk.space_usage_pct"] = _pct(bytes_used, bytes_total)

    metrics.update(
        dict.fromkeys(
            (
                "disk.total.read_bytes_per_sec",
                "disk.total.write_bytes_per_sec",
                "disk.total.read_iops",
                "disk.total.write_iops",
                "disk.max.utilization_pct",
                "disk.max.await_ms",
                "disk.max.queue_depth",
            ),
            0,
        )
    )

    metrics["disk.list"] = []
    metrics["disk.max.device"] = None

    for k, n in end["proc.diskstats"].items():
        if k not in start["proc.diskstats"]:
            continue
        o = start["proc.diskstats"][k]
        metrics["disk.list"].append(k)
        metrics[f"disk.{k}.read_bytes_per_sec"] = _delta(n["sectors_read"], o["sectors_read"]) * 512 / interval
        metrics[f"disk.{k}.write_bytes_per_sec"] = _delta(n["sectors_written"], o["sectors_written"]) * 512 / interval
        metrics[f"disk.{k}.read_iops"] = _delta(n["reads"], o["reads"]) / interval
        metrics[f"disk.{k}.write_iops"] = _delta(n["writes"], o["writes"]) / interval

        metrics["disk.total.read_bytes_per_sec"] += _delta(n["sectors_read"], o["sectors_read"]) * 512
        metrics["disk.total.write_bytes_per_sec"] += _delta(n["sectors_written"], o["sectors_written"]) * 512
        metrics["disk.total.read_iops"] += _delta(n["reads"], o["reads"])
        metrics["disk.total.write_iops"] += _delta(n["writes"], o["writes"])

        try:
            metrics[f"disk.{k}.await_ms"] = (
                _delta(n["time_reading"], o["time_reading"]) + _delta(n["time_writing"], o["time_writing"])
            ) / (_delta(n["reads"], o["reads"]) + _delta(n["writes"], o["writes"]))
        except ZeroDivisionError:
            metrics[f"disk.{k}.await_ms"] = 0

        metrics[f"disk.{k}.utilization_pct"] = (
            _delta(n["time_in_progress"], o["time_in_progress"]) * 100 / (interval * 1000)
        )
        metrics[f"disk.{k}.weighted_utilization_pct"] = (
            _delta(n["weighted_time_in_progress"], o["weighted_time_in_progress"]) * 100 / (interval * 1000)
        )
        metrics[f"disk.{k}.queue_depth"] = n["in_progress"]

        if metrics[f"disk.{k}.utilization_pct"] > metrics["disk.max.utilization_pct"]:
            metrics["disk.max.utilization_pct"] = metrics[f"disk.{k}.utilization_pct"]
            metrics["disk.max.await_ms"] = metrics[f"disk.{k}.await_ms"]
            metrics["disk.max.queue_depth"] = n["in_progress"]
            metrics["disk.max.device"] = k

    metrics["disk.total.read_bytes_per_sec"] /= interval
    metrics["disk.total.write_bytes_per_sec"] /= interval
    metrics["disk.total.read_iops"] /= interval
    metrics["disk.total.write_iops"] /= interval


def calculate_metrics(start, end, interval):
    metrics = {}
    metrics["timestamp"] = end["timestamp"]
    metrics["hostname"] = end["hostname"]
    metrics["uptime"] = end["proc.uptime"]["seconds"]
    metrics["cpu.loadavg.1min"] = end["proc.loadavg"]["1min"]
    metrics["cpu.loadavg.1min.previous"] = start["proc.loadavg"]["1min"]
    metrics["cpu.run_queue"] = end["proc.stat"]["procs_running"]
    metrics["cpu.run_queue_per_cpu"] = _pct(metrics["cpu.run_queue"], end["cpu.count"])

    # Self-measured collection cost of this tick (nanoseconds in raw, seconds here)
    metrics["collect.procfs_seconds"] = end["procfs_duration"] / 1e9
    metrics["collect.proc_pid_seconds"] = end["procfs_pid_duration"] / 1e9
    metrics["collect.logs_seconds"] = end["logs_duration"] / 1e9
    metrics["collect.docker_logs_seconds"] = end["docker_logs_duration"] / 1e9
    metrics["collect.oom_seconds"] = end["oom_duration"] / 1e9

    with contextlib.suppress(KeyError):
        end_self = end["self"]
        start_self = start.get("self", end_self)
        metrics["self.cpu_seconds"] = round(end_self["cpu_seconds"], 3)
        metrics["self.cpu_pct"] = round(_delta(end_self["cpu_seconds"], start_self["cpu_seconds"]) * 100 / interval, 2)
        metrics["self.rss_bytes"] = end_self["rss_bytes"]
        metrics["self.read_bytes_per_sec"] = _delta(end_self["read_bytes"], start_self["read_bytes"]) / interval
        metrics["self.ctx_switches_per_sec"] = (
            _delta(end_self["voluntary_ctxt_switches"], start_self["voluntary_ctxt_switches"]) / interval
        )

    available = end["proc.meminfo"].get("MemAvailable", end["proc.meminfo"]["MemFree"])
    metrics["mem.utilization_pct"] = _pct(end["proc.meminfo"]["MemTotal"] - available, end["proc.meminfo"]["MemTotal"])
    # TODO free, buff, etc?

    metrics["mem.swap_utilization_pct"] = _pct(
        end["proc.meminfo"]["SwapTotal"] - end["proc.meminfo"]["SwapFree"],
        end["proc.meminfo"]["SwapTotal"],
    )

    _calculate_metrics_cpu(metrics, start, end, interval)
    _calculate_metrics_net(metrics, start, end, interval)
    _calculate_metrics_disk(metrics, start, end, interval)

    with contextlib.suppress(KeyError):
        for prefix, file in {"mem": "memory", "cpu": "cpu", "disk": "io"}.items():
            for tag in ("full", "some"):
                for avg in ("avg10", "avg60"):
                    metrics[f"{prefix}.pressure.{tag}.{avg}"] = end[f"proc.pressure.{file}"][tag][avg]
                    metrics[f"{prefix}.pressure.{tag}.{avg}.previous"] = start[f"proc.pressure.{file}"][tag][avg]

    metrics["oom"] = end["oom"]
    for oom in metrics["oom"]:
        with contextlib.suppress(KeyError):
            pid = start["proc.pid"][int(oom["pid"])]
            oom["cmdline"] = pid["cmdline"]
            oom["rss"] = pid["rss"]
            oom["starttime"] = pid["starttime"]

    metrics["oom.count"] = len(metrics["oom"])
    metrics["oom.victims"] = "\n".join(
        [f"PID={oom['pid']} {oom['comm']} RSS={oom.get('rss', 0) // 1048576}MiB" for oom in metrics["oom"]]
    )

    for logs in ("logs", "docker-logs"):
        metrics[f"{logs}.list"] = []
        for name, stats in end[logs].items():
            metrics[f"{logs}.list"].append(name)
            metrics[f"{logs}.{name}.name"] = name
            metrics[f"{logs}.{name}.line_count"] = stats["line_count"]
            metrics[f"{logs}.{name}.error_count"] = stats["error_count"]
            metrics[f"{logs}.{name}.error_pct"] = _pct(stats["error_count"], stats["line_count"])
            if len(stats["errors"]) > 4:
                metrics[f"{logs}.{name}.errors"] = "".join(stats["errors"][:3] + ["...\n"] + stats["errors"][-1:])
            else:
                metrics[f"{logs}.{name}.errors"] = "".join(stats["errors"])
            with contextlib.suppress(KeyError):
                metrics[f"{logs}.{name}.heartbeat_last_seen"] = stats["heartbeat_last_seen"]
                age = max(time.time() - stats["heartbeat_last_seen"], 0)
                metrics[f"{logs}.{name}.heartbeat_age_seconds"] = age
                metrics[f"{logs}.{name}.heartbeat_status"] = (
                    "not seen yet" if not stats["heartbeat_last_seen"] else f"not seen for {int(age)} seconds"
                )

    return metrics
