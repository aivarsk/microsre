import argparse
import atexit
import contextlib
import signal
import sys
import threading
import time
import traceback
from urllib.parse import urlparse

from microsre import prom, state
from microsre.alert import configured_alerts, send_configured_notification
from microsre.burst import BurstSampler
from microsre.core import banner, config, ingest, log, post_ingest, worker
from microsre.logreader import collect_logs, file_offsets, init_logs
from microsre.metrics import calculate_metrics
from microsre.procfs import collect_procfs, uptime_seconds
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


def _check_interval(name, interval):
    if not isinstance(interval, int) or interval < 5:
        raise ValueError(f"Invalid {name} {interval!r}, expected an int of at least 5 seconds")
    return interval


def _saas_cadence_note(interval):
    # Fixed SaaS plans accept one tick per 60s; faster cadence needs a custom plan.
    if interval < 60 and config().get("ingest_url"):
        return (
            f"interval {interval}s is under the SaaS 60s floor for fixed plans; "
            "over-cadence ticks are dropped at ingest unless the plan allows them"
        )
    return None


def run_monitoring_loop(interval, daemon, on_tick):
    # The unattended loop gets a floor; foreground --snapshot sets its own pace.
    if daemon:
        _check_interval("interval", interval)
        cadence_note = _saas_cadence_note(interval)
        if cadence_note:
            log.warning(cadence_note)
    alive_every = _check_interval("alive_interval", config().get("alive_interval", 5))

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
        ticks = 0

        # Alive pings ride the burst sampler's 1s timer, so a wedged main loop can't stall them.
        def sampler_tick(now):
            nonlocal ticks
            sampler._tick(now)
            ticks += 1
            if ticks % alive_every == 0:
                ingest("alive", {"uptime": uptime_seconds()})

        worker.every(1, sampler_tick)
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
    parser.add_argument(
        "--notify",
        metavar="MESSAGE",
        help="Send MESSAGE through the configured notification channel and exit",
    )

    args = parser.parse_args()

    if args.verify:
        try:
            config()
            init_logs(fail=True)
            configured_alerts(fail=True)
            prom.parse_listen(config())
            _check_interval("interval", config().get("interval", 60))
            _check_interval("alive_interval", config().get("alive_interval", 5))
            if url := config().get("ingest_url"):
                if not post_ingest("alive", {"uptime": uptime_seconds()}):
                    raise ValueError("ingest_url did not accept an alive event, see the log for details")
                print(f"alive event accepted by {urlparse(url).hostname}")
            cadence_note = _saas_cadence_note(config().get("interval", 60))
            if cadence_note:
                print(f"Warning: {cadence_note}")
            print("Configuration file valid")
        except Exception as exc:
            print(f"Invalid configuration file: {exc!r}")
            sys.exit(1)
    elif args.notify is not None:
        config()
        notify_or_exit(args.notify)
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


def notify_or_exit(message):
    if not config().get("webhook_url"):
        print("Cannot send: webhook_url is not set in the configuration file")
        sys.exit(1)
    if send_configured_notification(message) is None:
        print("Delivery failed, see the log for details")
        sys.exit(1)
    print("Notification sent")
