import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from backup_manager import create_snapshot, restore_snapshot


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

            self.assertTrue(third.is_file())
            self.assertEqual(len(list(backup_dir.glob("mg-business-*.sqlite3"))), 2)
            snapshot = sqlite3.connect(third)
            self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM records").fetchone()[0], 3)
            snapshot.close()
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(third.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)

    def test_restore_validates_snapshot_and_saves_current_database_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            backup_dir = root / "backups"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT REFERENCES tenants(id))")
            db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO records(value) VALUES('before restore')")
            db.commit()
            db.close()
            snapshot = create_snapshot(database, backup_dir, retention=5)

            db = sqlite3.connect(database)
            db.execute("INSERT INTO records(value) VALUES('after snapshot')")
            db.commit()
            db.close()

            restored = restore_snapshot(snapshot, database, backup_dir)
            self.assertEqual(restored, database.resolve())
            db = sqlite3.connect(database)
            values = [row[0] for row in db.execute("SELECT value FROM records ORDER BY id")]
            self.assertEqual(values, ["before restore"])
            db.close()
            self.assertGreaterEqual(len(list(backup_dir.glob("mg-business-*.sqlite3"))), 2)

    def test_invalid_retention_and_missing_database_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 0)
            with self.assertRaises(FileNotFoundError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 2)
            invalid = Path(directory) / "not-mg.sqlite3"
            db = sqlite3.connect(invalid)
            db.execute("CREATE TABLE random_table(id INTEGER PRIMARY KEY)")
            db.commit()
            db.close()
            with self.assertRaises(sqlite3.DatabaseError):
                restore_snapshot(invalid, Path(directory) / "target.sqlite3", Path(directory) / "backups")


if __name__ == "__main__":
    unittest.main()
