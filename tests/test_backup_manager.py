import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from backup_manager import create_snapshot


class BackupManagerTests(unittest.TestCase):
    def test_snapshot_is_consistent_private_and_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "source.sqlite3"
            backup_dir = root / "backups"
            db = sqlite3.connect(database)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO records(value) VALUES('MG snapshot')")
            db.commit()
            first = create_snapshot(database, backup_dir, retention=2)
            db.execute("INSERT INTO records(value) VALUES('second record')")
            db.commit()
            second = create_snapshot(database, backup_dir, retention=2)
            db.execute("INSERT INTO records(value) VALUES('third record')")
            db.commit()
            third = create_snapshot(database, backup_dir, retention=2)
            db.close()

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertTrue(third.is_file())
            self.assertEqual(len(list(backup_dir.glob("mg-business-*.sqlite3"))), 2)
            snapshot = sqlite3.connect(third)
            self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM records").fetchone()[0], 3)
            snapshot.close()
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(third.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)

    def test_invalid_retention_and_missing_database_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 0)
            with self.assertRaises(FileNotFoundError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 2)


if __name__ == "__main__":
    unittest.main()
