import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from backup_manager import create_snapshot, generate_encryption_key, restore_snapshot


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
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,1)")
            db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
            db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT REFERENCES tenants(id),
                username TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                active INTEGER NOT NULL, created_at INTEGER NOT NULL)""")
            for table in (
                "sessions", "transactions", "accounts", "journal_entries", "journal_lines",
                "products", "inventory_movements", "customers", "customer_ledger",
                "orders", "order_items", "game_devices", "game_sessions",
                "gamenet_reservations", "licenses", "tenant_modules",
            ):
                db.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
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

    def test_encrypted_snapshot_round_trips_and_rejects_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            backup_dir = root / "backups"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(13,1)")
            db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
            db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT REFERENCES tenants(id),
                username TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                active INTEGER NOT NULL, created_at INTEGER NOT NULL)""")
            db.execute("INSERT INTO tenants VALUES('tenant-1','MG Test',1)")
            db.execute("INSERT INTO users VALUES('user-1','tenant-1','owner','hash','owner',1,1)")
            db.execute("CREATE TABLE private_records(value TEXT NOT NULL)")
            db.execute("INSERT INTO private_records VALUES('confidential backup payload')")
            db.commit()
            db.close()

            key = generate_encryption_key()
            snapshot = create_snapshot(database, backup_dir, encryption_key=key)
            self.assertTrue(snapshot.name.endswith(".sqlite3.enc"))
            raw = snapshot.read_bytes()
            self.assertNotIn(b"SQLite format 3", raw)
            self.assertNotIn(b"confidential backup payload", raw)
            self.assertFalse(list(backup_dir.glob("*.plain.tmp")))
            self.assertFalse(list(backup_dir.glob("*.tmp")))

            restored = root / "restored.sqlite3"
            db = sqlite3.connect(restored)
            db.execute("CREATE TABLE existing_data(value TEXT)")
            db.execute("INSERT INTO existing_data VALUES('keep until verified restore')")
            db.commit()
            db.close()
            with self.assertRaises(ValueError):
                restore_snapshot(snapshot, restored, encryption_key=generate_encryption_key())
            db = sqlite3.connect(restored)
            self.assertEqual(db.execute("SELECT value FROM existing_data").fetchone()[0],
                             "keep until verified restore")
            db.close()
            restore_snapshot(snapshot, restored, encryption_key=key)
            safety_backups = list((root / "backups").glob("*.sqlite3.enc"))
            self.assertEqual(len(safety_backups), 2)
            db = sqlite3.connect(restored)
            self.assertEqual(db.execute("SELECT value FROM private_records").fetchone()[0],
                             "confidential backup payload")
            db.close()

    def test_encrypted_snapshot_requires_key_and_authenticates_ciphertext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE records(value TEXT)")
            db.execute("INSERT INTO records VALUES('secret')")
            db.commit()
            db.close()
            key = generate_encryption_key()
            snapshot = create_snapshot(database, root / "backups", encryption_key=key)
            with self.assertRaises(ValueError):
                restore_snapshot(snapshot, root / "target.sqlite3")
            damaged = bytearray(snapshot.read_bytes())
            damaged[-20] ^= 0x01
            snapshot.write_bytes(damaged)
            with self.assertRaises(Exception):
                restore_snapshot(snapshot, root / "target.sqlite3", encryption_key=key)
            self.assertFalse((root / "target.sqlite3").exists())

    def test_invalid_retention_and_missing_database_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 0)
            with self.assertRaises(FileNotFoundError):
                create_snapshot(Path(directory) / "missing.sqlite3", Path(directory) / "backups", 2)
            invalid = Path(directory) / "not-mg.sqlite3"
            db = sqlite3.connect(invalid)
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,1)")
            db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
            db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT, username TEXT,
                role TEXT, active INTEGER, created_at INTEGER)""")
            db.execute("CREATE TABLE random_table(id INTEGER PRIMARY KEY)")
            db.commit()
            db.close()
            with self.assertRaises(sqlite3.DatabaseError):
                restore_snapshot(invalid, Path(directory) / "target.sqlite3", Path(directory) / "backups")


if __name__ == "__main__":
    unittest.main()
