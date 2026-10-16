import contextlib
import sqlite3
import time

from microsre.core import log, state_db_path

FRESH_SECONDS = 600

_MIGRATIONS = [
    """
CREATE TABLE IF NOT EXISTS offsets (
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    st_ino INTEGER NOT NULL,
    st_dev INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    PRIMARY KEY (name, path)
);
""",
    """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""",
    """
CREATE TABLE IF NOT EXISTS heartbeat (
    metric TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    last_seen REAL NOT NULL
);
""",
]

VERSION = len(_MIGRATIONS)


def _connect():
    db = sqlite3.connect(state_db_path())
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 5000")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > VERSION:
        log.warning(f"state.sqlite is version {version}, newer than this microsre understands ({VERSION})")
    else:
        for index, script in enumerate(_MIGRATIONS[version:], start=version):
            db.executescript(script)
            db.execute(f"PRAGMA user_version = {index + 1}")
    return db


def load():
    """{(name, path): (st_ino, st_dev, pos)} and when it was saved, None if never."""
    with contextlib.closing(_connect()) as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'offsets_saved_at'").fetchone()
        offsets_saved_at = float(row[0]) if row else None
        rows = db.execute("SELECT name, path, st_ino, st_dev, pos FROM offsets").fetchall()
    return {(name, path): (st_ino, st_dev, pos) for name, path, st_ino, st_dev, pos in rows}, offsets_saved_at


def save(offsets):
    with contextlib.closing(_connect()) as db:
        with db:
            db.execute("DELETE FROM offsets")
            db.executemany(
                "INSERT INTO offsets (name, path, st_ino, st_dev, pos) VALUES (?, ?, ?, ?, ?)",
                [(name, path, st_ino, st_dev, pos) for (name, path), (st_ino, st_dev, pos) in offsets.items()],
            )
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('offsets_saved_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(time.time()),),
            )


def touch_heartbeat(metric, pattern, last_seen):
    try:
        with contextlib.closing(_connect()) as db:
            with db:
                db.execute(
                    "INSERT INTO heartbeat (metric, pattern, last_seen) VALUES (?, ?, ?) "
                    "ON CONFLICT(metric) DO UPDATE SET pattern = excluded.pattern, last_seen = excluded.last_seen",
                    (metric, pattern, last_seen),
                )
    except sqlite3.Error:
        log.warning(f"Persisting heartbeat {metric} failed", exc_info=True)


def load_heartbeats():
    """{metric: (pattern, last_seen)} for seeding LineCounter clocks at startup."""
    with contextlib.closing(_connect()) as db:
        rows = db.execute("SELECT metric, pattern, last_seen FROM heartbeat").fetchall()
    return {metric: (pattern, last_seen) for metric, pattern, last_seen in rows}
