import socket

from microsre.registry import HELP, to_prom


def parse_listen(config):
    address = config.get("prometheus_listen")
    if not address:
        return None
    host, sep, port = address.rpartition(":")
    if not sep or not host:
        raise ValueError(f"Invalid prometheus_listen {address!r}, expected 'host:port'")
    try:
        return host, int(port)
    except ValueError:
        raise ValueError(f"Invalid prometheus_listen {address!r}, expected 'host:port'") from None


def listener(config):
    parsed = parse_listen(config)
    if parsed:
        return socket.create_server(parsed)
    return None


_snapshot = "# no metrics collected yet\n"


def publish(metrics):
    global _snapshot
    _snapshot = render(metrics)


# Naming: internal metric names get a "microsre_" prefix with dots turned into
# underscores; per-core, per-device, and per-interface families become labels.
# Keep in sync with the "Prometheus" section of docs/readme.html.
def render(metrics):
    lines = []
    seen = set()

    def emit(name, labels, value):
        if type(value) not in (int, float):
            return
        if name not in seen:
            seen.add(name)
            if name in HELP:
                lines.append(f"# HELP {name} {HELP[name]}")
            lines.append(f"# TYPE {name} gauge")
        labels = "" if not labels else "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{labels} {value}")

    def flat(name, value):
        emit(to_prom(name), {}, value)

    for core, value in metrics.get("cpu.utilization_per_core", {}).items():
        emit("microsre_cpu_utilization_per_core", {"core": core}, value)

    for device in metrics.get("disk.list", []):
        for field in (
            "read_bytes_per_sec",
            "write_bytes_per_sec",
            "read_iops",
            "write_iops",
            "await_ms",
            "utilization_pct",
            "weighted_utilization_pct",
            "queue_depth",
        ):
            emit(f"microsre_disk_{field}", {"device": device}, metrics.get(f"disk.{device}.{field}"))

    for iface in metrics.get("net.list", []):
        for field in (
            "rx_bytes_per_sec",
            "tx_bytes_per_sec",
            "rx_packets_per_sec",
            "tx_packets_per_sec",
            "rx_errs_per_sec",
            "tx_errs_per_sec",
            "rx_drop_per_sec",
            "tx_drop_per_sec",
            "utilization_pct",
        ):
            emit(f"microsre_net_{field}", {"iface": iface}, metrics.get(f"net.{iface}.{field}"))

    labeled = [f"disk.{d}." for d in metrics.get("disk.list", [])] + [f"net.{i}." for i in metrics.get("net.list", [])]
    for name, value in metrics.items():
        if name == "cpu.utilization_per_core" or any(name.startswith(p) for p in labeled):
            continue
        flat(name, value)

    return "\n".join(lines) + "\n"


def _respond(conn, status, body):
    data = body.encode()
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    conn.sendall(head.encode() + data)


def handle(conn):
    try:
        conn.settimeout(5)
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            request += chunk
        method, _, rest = request.partition(b" ")
        if method == b"GET" and rest.split(b" ", 1)[0] == b"/metrics":
            _respond(conn, "200 OK", _snapshot)
        else:
            _respond(conn, "404 Not Found", "")
    except OSError:
        pass
    finally:
        conn.close()
