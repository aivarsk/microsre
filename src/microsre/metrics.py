import contextlib
import os
import time

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def _pct(value, total):
    with contextlib.suppress(ZeroDivisionError):
        return int(value * 100 / total)
    return 0


def _delta(new, old):
    return max(0, new - old)


def names(value):
    return value.split(",") if value else []


_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
_CPU_ACTIVE = ("user", "nice", "system", "irq", "softirq", "steal")
_CPU_TOTAL = _CPU_ACTIVE + ("idle", "iowait")


def _cpu_line_pct(fields, previous):
    delta = {field: _delta(fields[field], previous[field]) for field in _CPU_FIELDS}
    total = sum(delta[field] for field in _CPU_TOTAL)
    if not total:
        return 0, 0, 0
    active = sum(delta[field] for field in _CPU_ACTIVE)
    return int(active * 100 / total), int(delta["steal"] * 100 / total), int(delta["iowait"] * 100 / total)


def _disk_utilization_pct(previous, current, interval):
    return _delta(current["time_in_progress"], previous["time_in_progress"]) * 100 / (interval * 1000)


def _calculate_metrics_cpu(metrics, start, end, interval):
    cores = []
    for k, v in end["proc.stat"].items():
        if k.startswith("cpu") and k in start["proc.stat"]:
            utilization, steal, iowait = _cpu_line_pct(v, start["proc.stat"][k])
            if k == "cpu":
                metrics["cpu.utilization_pct"] = utilization
                metrics["cpu.iowait_pct"] = iowait
                metrics["cpu.steal_pct"] = steal
            else:
                metrics[f"cpu.{k}.utilization_pct"] = utilization
                cores.append(k)
                if utilization > metrics.get("cpu.max_utilization_pct", -1):
                    metrics["cpu.max_utilization_pct"] = utilization
                    metrics["cpu.max_utilization_core"] = k
    metrics["cpu.list"] = ",".join(cores)


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
    ifaces = []

    for k, n in end["proc.net.dev"].items():
        if k not in start["proc.net.dev"]:
            continue
        o = start["proc.net.dev"][k]
        ifaces.append(k)

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

    metrics["net.list"] = ",".join(ifaces)
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

    devices = []
    metrics["disk.max.device"] = None

    for k, n in end["proc.diskstats"].items():
        if k not in start["proc.diskstats"]:
            continue
        o = start["proc.diskstats"][k]
        devices.append(k)
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

        metrics[f"disk.{k}.utilization_pct"] = _disk_utilization_pct(o, n, interval)
        metrics[f"disk.{k}.weighted_utilization_pct"] = (
            _delta(n["weighted_time_in_progress"], o["weighted_time_in_progress"]) * 100 / (interval * 1000)
        )
        metrics[f"disk.{k}.queue_depth"] = n["in_progress"]

        if metrics[f"disk.{k}.utilization_pct"] > metrics["disk.max.utilization_pct"]:
            metrics["disk.max.utilization_pct"] = metrics[f"disk.{k}.utilization_pct"]
            metrics["disk.max.await_ms"] = metrics[f"disk.{k}.await_ms"]
            metrics["disk.max.queue_depth"] = n["in_progress"]
            metrics["disk.max.device"] = k

    metrics["disk.list"] = ",".join(devices)
    metrics["disk.total.read_bytes_per_sec"] /= interval
    metrics["disk.total.write_bytes_per_sec"] /= interval
    metrics["disk.total.read_iops"] /= interval
    metrics["disk.total.write_iops"] /= interval


def calculate_burst_metrics(previous, current, interval):
    metrics = {}
    stat, prev_stat = current["stat"], previous["stat"]

    with contextlib.suppress(KeyError):
        metrics["cpu.max_ctxt_per_sec"] = _delta(stat["ctxt"], prev_stat["ctxt"]) / interval
    if "procs_running" in stat:
        metrics["cpu.max_procs_running"] = max(0, stat["procs_running"] - 1)
    if "procs_blocked" in stat:
        metrics["cpu.max_procs_blocked"] = stat["procs_blocked"]

    per_core = []
    for name, fields in stat.items():
        if name == "cpu" or not name.startswith("cpu") or name not in prev_stat:
            continue
        utilization, steal, iowait = _cpu_line_pct(fields, prev_stat[name])
        per_core.append((utilization, steal, iowait, name))
    if per_core:
        hottest = max(per_core, key=lambda core: core[0])
        metrics["cpu.max_utilization_pct"] = hottest[0]
        metrics["cpu.max_utilization_core"] = hottest[3]
        metrics["cpu.max_steal_pct"] = max(core[1] for core in per_core)
        metrics["cpu.max_iowait_pct"] = max(core[2] for core in per_core)

    utils = []
    for name, fields in current["diskstats"].items():
        prev = previous["diskstats"].get(name)
        if prev is None:
            continue
        metrics[f"disk.{name}.utilization_pct"] = _disk_utilization_pct(prev, fields, interval)
        metrics[f"disk.{name}.queue_depth"] = fields["in_progress"]
        metrics[f"disk.{name}.read_bytes_per_sec"] = (
            _delta(fields["sectors_read"], prev["sectors_read"]) * 512 / interval
        )
        metrics[f"disk.{name}.write_bytes_per_sec"] = (
            _delta(fields["sectors_written"], prev["sectors_written"]) * 512 / interval
        )
        utils.append((metrics[f"disk.{name}.utilization_pct"], name))
    if utils:
        metrics["disk.max_utilization_pct"] = max(util for util, _ in utils)
        metrics["disk.max_utilization_device"] = max(utils)[1]

    for name, fields in current["net.dev"].items():
        prev = previous["net.dev"].get(name)
        if prev is None:
            continue
        metrics[f"net.{name}.rx_bytes_per_sec"] = _delta(fields["rx_bytes"], prev["rx_bytes"]) / interval
        metrics[f"net.{name}.tx_bytes_per_sec"] = _delta(fields["tx_bytes"], prev["tx_bytes"]) / interval

    with contextlib.suppress(KeyError):
        metrics["net.tcp.max_retrans_per_sec"] = (
            _delta(current["net.snmp"]["TcpRetransSegs"], previous["net.snmp"]["TcpRetransSegs"]) / interval
        )
    for key, metric in (
        ("TcpExtListenDrops", "net.tcp.max_listen_drops_per_sec"),
        ("TcpExtListenOverflows", "net.tcp.max_listen_overflows_per_sec"),
    ):
        with contextlib.suppress(KeyError):
            metrics[metric] = _delta(current["net.netstat"][key], previous["net.netstat"][key]) / interval

    for key, metric in (
        ("pswpin", "vm.max_swap_in_bytes_per_sec"),
        ("pswpout", "vm.max_swap_out_bytes_per_sec"),
    ):
        with contextlib.suppress(KeyError):
            metrics[metric] = _delta(current["vmstat"][key], previous["vmstat"][key]) * _PAGE_SIZE / interval
    with contextlib.suppress(KeyError):
        metrics["vm.max_major_faults_per_sec"] = (
            _delta(current["vmstat"]["pgmajfault"], previous["vmstat"]["pgmajfault"]) / interval
        )

    return metrics


def calculate_metrics(start, end, interval):
    metrics = {}
    metrics["timestamp"] = end["timestamp"]
    metrics["hostname"] = end["hostname"]
    metrics["uptime"] = end["proc.uptime"]["seconds"]
    metrics["cpu.loadavg.1min"] = end["proc.loadavg"]["1min"]
    metrics["cpu.loadavg.1min.previous"] = start["proc.loadavg"]["1min"]

    with contextlib.suppress(KeyError):
        metrics["cpu.ctxt_per_sec"] = _delta(end["proc.stat"]["ctxt"], start["proc.stat"]["ctxt"]) / interval
    if "procs_blocked" in end["proc.stat"]:
        metrics["cpu.procs_blocked"] = end["proc.stat"]["procs_blocked"]

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

    with contextlib.suppress(KeyError):
        metrics["vm.swap_in_bytes_per_sec"] = (
            _delta(end["proc.vmstat"]["pswpin"], start["proc.vmstat"]["pswpin"]) * _PAGE_SIZE / interval
        )
        metrics["vm.swap_out_bytes_per_sec"] = (
            _delta(end["proc.vmstat"]["pswpout"], start["proc.vmstat"]["pswpout"]) * _PAGE_SIZE / interval
        )
        metrics["vm.major_faults_per_sec"] = (
            _delta(end["proc.vmstat"]["pgmajfault"], start["proc.vmstat"]["pgmajfault"]) / interval
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

    ooms = end["oom"]
    for oom in ooms:
        with contextlib.suppress(KeyError):
            pid = start["proc.pid"][int(oom["pid"])]
            oom["cmdline"] = pid["cmdline"]
            oom["rss"] = pid["rss"]
            oom["starttime"] = pid["starttime"]

    metrics["oom.count"] = len(ooms)
    metrics["oom.victims"] = "\n".join(
        [f"PID={oom['pid']} {oom['comm']} RSS={oom.get('rss', 0) // 1048576}MiB" for oom in ooms]
    )

    for logs in ("logs", "docker-logs"):
        sources = []
        for name, stats in end[logs].items():
            sources.append(name)
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
        metrics[f"{logs}.list"] = ",".join(sources)

    return metrics
