import os
import sqlite3
import time
import unittest
from pathlib import Path

from testutil import isolate_xdg

from microsre import state


def setUpModule():
    isolate_xdg()


class TestState(unittest.TestCase):
    def setUp(self):
        Path(os.environ["XDG_STATE_HOME"], "microsre", "state.sqlite").unlink(missing_ok=True)

    def test_round_trip(self):
        offsets = {("batch", "/var/log/batch.log"): (123, 456, 789), ("app", "/app.log"): (1, 2, 3)}
        state.save(offsets)
        loaded, offsets_saved_at = state.load()
        self.assertEqual(loaded, offsets)
        self.assertAlmostEqual(offsets_saved_at, time.time(), delta=5)

    def test_empty_database_loads_nothing(self):
        loaded, offsets_saved_at = state.load()
        self.assertEqual(loaded, {})
        self.assertIsNone(offsets_saved_at)

    def test_save_replaces_previous_offsets(self):
        state.save({("a", "/a.log"): (1, 1, 1)})
        state.save({("b", "/b.log"): (2, 2, 2)})
        loaded, _ = state.load()
        self.assertEqual(loaded, {("b", "/b.log"): (2, 2, 2)})

    def test_database_is_versioned(self):
        state.save({})
        db = sqlite3.connect(Path(os.environ["XDG_STATE_HOME"], "microsre", "state.sqlite"))
        version = db.execute("PRAGMA user_version").fetchone()[0]
        journal = db.execute("PRAGMA journal_mode").fetchone()[0]
        db.close()
        self.assertEqual(version, state.VERSION)
        self.assertEqual(journal, "wal")

    def test_version_1_database_gets_meta_table(self):
        path = Path(os.environ["XDG_STATE_HOME"], "microsre", "state.sqlite")
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE offsets (name TEXT NOT NULL, path TEXT NOT NULL, "
            "st_ino INTEGER NOT NULL, st_dev INTEGER NOT NULL, pos INTEGER NOT NULL, "
            "PRIMARY KEY (name, path))"
        )
        db.execute("PRAGMA user_version = 1")
        db.commit()
        db.close()

        state.save({("batch", "/var/log/batch.log"): (1, 2, 3)})
        db = sqlite3.connect(path)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        db.close()
        self.assertEqual(version, state.VERSION)
        self.assertIn("meta", tables)

    def test_version_2_database_gets_heartbeat_table(self):
        path = Path(os.environ["XDG_STATE_HOME"], "microsre", "state.sqlite")
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE offsets (name TEXT NOT NULL, path TEXT NOT NULL, "
            "st_ino INTEGER NOT NULL, st_dev INTEGER NOT NULL, pos INTEGER NOT NULL, "
            "PRIMARY KEY (name, path))"
        )
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("PRAGMA user_version = 2")
        db.commit()
        db.close()

        state.touch_heartbeat("logs.batch.heartbeat_last_seen", "done", 1.0)
        db = sqlite3.connect(path)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        db.close()
        self.assertEqual(version, state.VERSION)
        self.assertIn("heartbeat", tables)
        self.assertEqual(state.load_heartbeats()["logs.batch.heartbeat_last_seen"], ("done", 1.0))

    def test_heartbeat_round_trip(self):
        state.touch_heartbeat("logs.batch.heartbeat_last_seen", "done", 123.5)
        state.touch_heartbeat("logs.batch.heartbeat_last_seen", "done", 456.5)
        self.assertEqual(state.load_heartbeats(), {"logs.batch.heartbeat_last_seen": ("done", 456.5)})


if __name__ == "__main__":
    unittest.main()
