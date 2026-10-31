import concurrent.futures
import functools
import gzip
import json
import logging
import logging.handlers
import os
import pkgutil
import queue
import selectors
import syslog
import threading
import time
import tomllib
import urllib.error
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

APP_NAME = "microsre"
try:
    from importlib.metadata import version

    APP_VERSION = version(APP_NAME)
except Exception:
    APP_VERSION = "dev"

syslog.openlog(
    ident=APP_NAME,
    logoption=syslog.LOG_PID | syslog.LOG_PERROR,
    facility=syslog.LOG_USER,
)


def _app_dir(system_dir, env_var, user_default):
    if os.geteuid() == 0:
        return Path(system_dir) / APP_NAME
    return Path(os.environ.get(env_var, os.path.expanduser(user_default))) / APP_NAME


def _write_private(path, data):
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


def state_dir():
    app_dir = _app_dir("/var/lib", "XDG_STATE_HOME", "~/.local/state")
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def state_db_path():
    return state_dir() / "state.sqlite"


def _id_path():
    return state_dir() / "machine-id"


@functools.lru_cache(maxsize=1)
def machine_id():
    id_path = _id_path()
    _write_private(id_path, str(uuid.uuid4()).encode())
    return id_path.read_text()


def _config_path():
    app_dir = _app_dir("/etc", "XDG_CONFIG_HOME", "~/.config")
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / "config.toml"


@functools.lru_cache(maxsize=1)
def config():
    config_path = _config_path()
    if not config_path.exists():
        default = pkgutil.get_data(__package__ or APP_NAME, "config.toml")
        if default is None:
            raise FileNotFoundError("Default config file not found")
        _write_private(config_path, default)
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _log_path():
    return _app_dir("/var/log", "XDG_STATE_HOME", "~/.local/state") / f"{APP_NAME}.log"


class _RotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def _setup_logger():
    log_path = _log_path()

    handler = _RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
        delay=True,
    )

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    return logger


def banner():
    print(APP_NAME, APP_VERSION)
    print("    Config file:", _config_path())
    print("       Log file:", _log_path())
    print("Machine ID file:", _id_path())
    print("     State file:", state_db_path())


log = _setup_logger()


_reported_interval = None


def reported_interval():
    """Cadence the SaaS advertises for this plan, once an ingest response carried it."""
    return _reported_interval


def _learn_cadence(headers):
    global _reported_interval
    if value := headers.get("x-microsre-interval"):
        try:
            _reported_interval = int(float(value))
        except ValueError:
            pass


def _post(url, data, headers, what):
    req = Request(
        url,
        data=data,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", **headers},
        method="POST",
    )
    for _ in range(3):
        try:
            with urlopen(req, timeout=5) as r:
                _learn_cadence(r.headers)
                return r.read()
        except urllib.error.HTTPError as exc:
            log.warning(f"Failed to post {what} to {url!r}", exc_info=True)
            if exc.code < 500:
                return None
        except (urllib.error.URLError, TimeoutError):
            log.warning(f"Failed to post {what} to {url!r}", exc_info=True)

    log.error(f"Retries failed to post {what} to {url!r}")
    return None


def _post_compressed_json(url, payload):
    body = json.dumps(payload, check_circular=False, separators=(",", ":"))
    data = gzip.compress(body.encode("utf-8"))

    response = _post(
        url,
        data,
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        repr(payload.get("kind")),
    )
    if response is None:
        return False
    try:
        accepted = json.loads(response)["status"] == "ok"
    except (json.JSONDecodeError, KeyError):
        accepted = True  # a 200 without a status body still means delivered
    if not accepted:
        log.warning(f"Ingest of {payload.get('kind')!r} rejected: {response!r}")
    return accepted


class BackgroundWorker:
    def __init__(self):
        self._queue: queue.Queue[Any] = queue.Queue()
        self._pipe = os.pipe()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._pipe[0], selectors.EVENT_READ)
        self._timers: list[list[Any]] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def add_listener(self, sock, handler):
        def register():
            self._selector.register(sock, selectors.EVENT_READ, (sock, handler))

        self.submit(register)

    def every(self, seconds, func):
        self._timers.append([time.monotonic() + seconds, seconds, func])
        os.write(self._pipe[1], b"\0")

    def _timeout(self):
        if not self._timers:
            return None
        return max(min(timer[0] for timer in self._timers) - time.monotonic(), 0)

    def _run_timers(self, now=None):
        if now is None:
            now = time.monotonic()
        for timer in self._timers:
            next_ts, seconds, func = timer
            if now < next_ts:
                continue
            if now >= next_ts + seconds:
                next_ts = now + seconds
            else:
                next_ts += seconds
            timer[0] = next_ts
            try:
                func(now)
            except Exception:
                log.warning(f"Timer {func} failed", exc_info=True)

    def _run(self):
        stop = False
        while not stop:
            for key, _ in self._selector.select(self._timeout()):
                if key.data is None:
                    os.read(self._pipe[0], 4096)
                    continue
                sock, handler = key.data
                try:
                    conn, _ = sock.accept()
                    handler(conn)
                except Exception:
                    log.warning("Listener handler failed", exc_info=True)
            self._run_timers()
            while True:
                try:
                    func, args, kwargs, future = self._queue.get_nowait()
                except queue.Empty:
                    break
                if func is None:
                    self._queue.task_done()
                    stop = True
                    break
                try:
                    future.set_result(func(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                    log.warning(f"Task {func}({args}, {kwargs}) failed", exc_info=True)
                finally:
                    self._queue.task_done()
        self._selector.close()

    def submit(self, func, *args, **kwargs):
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._queue.put((func, args, kwargs, future))
        os.write(self._pipe[1], b"\0")
        return future

    def shutdown(self):
        self.submit(None)
        self._queue.join()
        self._thread.join()


worker = BackgroundWorker()


def _ingest_payload(kind, data):
    return {
        "kind": kind,
        "uuid": str(uuid.uuid4()),
        "machine_id": machine_id(),
        "version": APP_VERSION,
        **data,
    }


def ingest(kind, data):
    url = config().get("ingest_url")
    if url:
        payload = _ingest_payload(kind, data)
        worker.submit(lambda: _post_compressed_json(url, payload))


def post_ingest(kind, data):
    # Synchronous counterpart of ingest: the caller needs the delivery result.
    url = config().get("ingest_url")
    if not url:
        return False
    return _post_compressed_json(url, _ingest_payload(kind, data))
