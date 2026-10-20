import argparse
import atexit
import contextlib
import signal
import sys
import threading
import time
import traceback

from microsre import prom, state
from microsre.alert import configured_alerts
from microsre.burst import BurstSampler
from microsre.core import banner, config, ingest, log, worker
from microsre.logreader import collect_logs, file_offsets, init_logs
from microsre.metrics import calculate_metrics
from microsre.procfs import collect_procfs
from microsre.text import dashboard


@contextlib.contextmanager
def lifecycle(daemon):
    if daemon:
        ingest("up", {})
        atexit.register(lambda: (ingest("down", {}), worker.shutdown()))
        try:
            sock = prom.listener(config())
            if sock:
                worker.add_listener(sock, prom.handle)
                log.info(f"Prometheus metrics on {sock.getsockname()}")
        except OSError:
            log.exception("Prometheus listener failed to start, endpoint disabled")

    event = threading.Event()

    def _sighandler(signum, frame):
        log.info(f"Signal {signum} received, shutting down")
        event.set()

    signal.signal(signal.SIGTERM, _sighandler)
    signal.signal(signal.SIGINT, _sighandler)

    yield event


def _alert_settings():
    return [
        {
            "metric": alert.metric,
            "min": alert.min_threshold,
            "max": alert.max_threshold,
            "duration": alert.duration,
            "recovery_margin": alert.recovery_margin,
            "alerting": alert.alerting,
        }
        for alert in configured_alerts()
    ]


def on_tick_report(metrics, raw):
    # Evaluate first, so the tick carries this interval's alerting state.
    for alert in configured_alerts():
        alert.evaluate(metrics, time.monotonic())
    ingest("tick", {"metrics": metrics, "alerts": _alert_settings()})
    prom.publish(metrics)


def on_tick_snapshot(metrics, raw):
    dashboard(metrics, raw)
    print("Alerting:", end=" ")
    for alert in configured_alerts(dryrun=True):
        alert.evaluate(metrics, time.monotonic())
        if alert.alerting:
            print(alert.metric, end=" ")
    print(flush=True)


def _check_interval(interval):
    if not isinstance(interval, int) or interval < 5:
        raise ValueError(f"Invalid interval {interval!r}, expected an int of at least 5 seconds")


def run_monitoring_loop(interval, daemon, on_tick):
    # The unattended loop gets a floor; foreground --snapshot sets its own pace.
    if daemon:
        _check_interval(interval)

    # Offsets from a recent-enough shutdown let heartbeat sources catch up.
    # Invalid log configuration refuses to start: a silently dropped source
    # would mean silently missing monitoring.
    saved_offsets, offsets_saved_at = state.load()
    if not offsets_saved_at:
        saved_offsets = {}
    elif time.time() - offsets_saved_at > state.FRESH_SECONDS:
        if daemon:
            # Sources tail to end instead of catching up: a coverage gap is possible.
            ingest("stale_offsets", {})
        saved_offsets = {}
    init_logs(fail=True, offsets=saved_offsets)

    with lifecycle(daemon) as stop:
        sampler = BurstSampler(interval)
        worker.every(1, sampler._tick)
        timer = time.monotonic()
        since = timestamp = time.time()
        previous = {}
        previous.update(collect_procfs(timestamp))
        last = time.monotonic()

        while not stop.wait(max(interval - (time.monotonic() - timer), 0)):
            try:
                timestamp = time.time()
                now = time.monotonic()
                current = {}
                current.update(collect_procfs(timestamp))

                next_since = time.time()
                current.update(collect_logs(since))
                since = next_since
                state.save(file_offsets())

                metrics = calculate_metrics(previous, current, max(now - last, 1))
                metrics.update(sampler.drain())
                on_tick(metrics, current)
                previous = current
                last = now
            except Exception:
                log.exception("Unexpected error, continuing")
                ingest("exc", {"traceback": traceback.format_exc()})
            timer += interval


def main():
    parser = argparse.ArgumentParser(description="Linux health checks at your cadence.")
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const=60,
        type=int,
        metavar="INTERVAL",
        help="Run in foreground: print metrics every N seconds, 60 by default.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Start the daemon",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version number and important files",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify configuration file",
    )

    args = parser.parse_args()

    if args.verify:
        try:
            config()
            init_logs(fail=True)
            configured_alerts(fail=True)
            prom.parse_listen(config())
            _check_interval(config().get("interval", 60))
            print("Configuration file valid")
        except Exception as exc:
            print(f"Invalid configuration file: {exc!r}")
            sys.exit(1)
    elif args.version:
        banner()
    elif args.snapshot is not None:
        config()
        run_or_exit(args.snapshot, False, on_tick_snapshot)
    elif args.daemon:
        config()
        run_or_exit(config().get("interval", 60), True, on_tick_report)
    else:
        parser.print_help()


def run_or_exit(interval, daemon, on_tick):
    try:
        run_monitoring_loop(interval, daemon, on_tick)
    except Exception as exc:
        print(f"Cannot start: {exc!r}")
        sys.exit(1)
