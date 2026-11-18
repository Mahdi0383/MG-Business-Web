import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from backup_manager import create_snapshot, generate_encryption_key, restore_snapshot, verify_snapshot


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
            self.assertTrue(verify_snapshot(snapshot))

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
            self.assertTrue(verify_snapshot(snapshot, key))
            with self.assertRaises(ValueError):
                verify_snapshot(snapshot)
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

            # Authentication failure must happen before the current database is
            # safety-backed-up or replaced. Preserve the exact live data.
            damaged = bytearray(snapshot.read_bytes())
            damaged[-20] ^= 0x01
            snapshot.write_bytes(damaged)
            with self.assertRaises(ValueError):
                restore_snapshot(snapshot, restored, encryption_key=key)
            db = sqlite3.connect(restored)
            self.assertEqual(db.execute("SELECT value FROM existing_data").fetchone()[0],
                             "keep until verified restore")
            db.close()
            self.assertEqual(len(list(backup_dir.glob("*.sqlite3.enc"))), 1)
            snapshot.write_bytes(raw)

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
            # GCM may emit plaintext before its final authentication check. The
            # verification path must always remove that temporary plaintext.
            with mock.patch("backup_manager.tempfile.gettempdir", return_value=str(root)):
                with self.assertRaises(ValueError):
                    verify_snapshot(snapshot, encryption_key=key)
            self.assertFalse(list(root.glob("mg-backup-verify-*.sqlite3")))
            with self.assertRaises(ValueError):
                restore_snapshot(snapshot, root / "target.sqlite3", encryption_key=key)
            self.assertFalse((root / "target.sqlite3").exists())
            self.assertFalse(list(root.glob("target.sqlite3.decrypt-*.tmp")))


    def test_backup_cli_create_verify_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            backup_dir = root / "backups"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_migrations VALUES(1,1)")
            db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
            db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT, username TEXT,
                password_hash TEXT, role TEXT, active INTEGER, created_at INTEGER)""")
            db.execute("CREATE TABLE business_records(value TEXT NOT NULL)")
            db.execute("INSERT INTO business_records VALUES('before-cli-restore')")
            db.commit()
            db.close()

            script = Path(__file__).resolve().parents[1] / "backup_manager.py"
            key_result = subprocess.run(
                [sys.executable, str(script), "--generate-key"],
                check=True, capture_output=True, text=True, timeout=15,
            )
            key = key_result.stdout.strip()
            self.assertTrue(key)

            environment = os.environ.copy()
            environment.update({
                "MG_DB_PATH": str(database),
                "MG_BACKUP_DIR": str(backup_dir),
                "MG_BACKUP_ENCRYPTION_KEY": key,
                "MG_BACKUP_RETENTION": "5",
            })
            created = subprocess.run(
                [sys.executable, str(script)],
                check=True, capture_output=True, text=True, env=environment, timeout=30,
            )
            snapshot = Path(created.stdout.strip().split(":", 1)[1].strip())
            self.assertTrue(snapshot.is_file())
            self.assertTrue(snapshot.name.endswith(".sqlite3.enc"))

            verified = subprocess.run(
                [sys.executable, str(script), "--verify", str(snapshot)],
                check=True, capture_output=True, text=True, env=environment, timeout=30,
            )
            self.assertIn("Verified MG database snapshot", verified.stdout)

            db = sqlite3.connect(database)
            db.execute("UPDATE business_records SET value='after-cli-snapshot'")
            db.commit()
            db.close()
            restored = subprocess.run(
                [sys.executable, str(script), "--restore", str(snapshot)],
                check=True, capture_output=True, text=True, env=environment, timeout=30,
            )
            self.assertIn("Restored database", restored.stdout)
            db = sqlite3.connect(database)
            self.assertEqual(
                db.execute("SELECT value FROM business_records").fetchone()[0],
                "before-cli-restore",
            )
            db.close()
            self.assertEqual(len(list(backup_dir.glob("*.sqlite3.enc"))), 2)

    def test_restore_replacement_failure_preserves_existing_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            snapshot_source = root / "snapshot-source.sqlite3"
            backup_dir = root / "backups"

            def create_valid_database(path, value):
                db = sqlite3.connect(path)
                db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES(1,1)")
                db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
                db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT, username TEXT,
                    password_hash TEXT, role TEXT, active INTEGER, created_at INTEGER)""")
                db.execute("CREATE TABLE business_records(value TEXT NOT NULL)")
                db.execute("INSERT INTO business_records VALUES(?)", (value,))
                db.commit()
                db.close()

            create_valid_database(database, "live-before-failed-restore")
            create_valid_database(snapshot_source, "snapshot-value")
            snapshot = create_snapshot(snapshot_source, backup_dir)
            wal = Path(str(database) + "-wal")
            shm = Path(str(database) + "-shm")
            wal.write_bytes(b"pending-wal-state")
            shm.write_bytes(b"shared-memory-state")

            real_replace = os.replace

            def fail_database_commit(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if (source_path.name.startswith(database.name + ".restore-")
                        and destination_path == database):
                    raise OSError("simulated atomic database replacement failure")
                return real_replace(source, destination)

            with mock.patch("backup_manager.create_snapshot"), \
                    mock.patch("backup_manager.os.replace", side_effect=fail_database_commit):
                with self.assertRaisesRegex(OSError, "simulated atomic database replacement failure"):
                    restore_snapshot(snapshot, database, backup_dir)

            self.assertEqual(wal.read_bytes(), b"pending-wal-state")
            self.assertEqual(shm.read_bytes(), b"shared-memory-state")
            db = sqlite3.connect(database)
            self.assertEqual(db.execute("SELECT value FROM business_records").fetchone()[0],
                             "live-before-failed-restore")
            db.close()
            self.assertFalse(list(root.glob("live.sqlite3.restore-*.tmp")))

    def test_restore_rolls_back_first_sidecar_if_second_sidecar_move_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            snapshot_source = root / "snapshot-source.sqlite3"
            backup_dir = root / "backups"

            def create_valid_database(path, value):
                db = sqlite3.connect(path)
                db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES(1,1)")
                db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
                db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT, username TEXT,
                    password_hash TEXT, role TEXT, active INTEGER, created_at INTEGER)""")
                db.execute("CREATE TABLE business_records(value TEXT NOT NULL)")
                db.execute("INSERT INTO business_records VALUES(?)", (value,))
                db.commit()
                db.close()

            create_valid_database(database, "live-before-restore")
            create_valid_database(snapshot_source, "snapshot-value")
            snapshot = create_snapshot(snapshot_source, backup_dir)
            wal = Path(str(database) + "-wal")
            shm = Path(str(database) + "-shm")
            wal.write_bytes(b"wal-state")
            shm.write_bytes(b"shm-state")
            real_replace = os.replace

            def fail_second_sidecar(source, destination):
                if Path(source) == shm:
                    raise OSError("simulated shm move failure")
                return real_replace(source, destination)

            with mock.patch("backup_manager.create_snapshot"), \\
                    mock.patch("backup_manager.os.replace", side_effect=fail_second_sidecar):
                with self.assertRaisesRegex(OSError, "simulated shm move failure"):
                    restore_snapshot(snapshot, database, backup_dir)

            self.assertEqual(wal.read_bytes(), b"wal-state")
            self.assertEqual(shm.read_bytes(), b"shm-state")
            self.assertFalse(list(root.glob("live.sqlite3-wal.restore-*.tmp")))
            self.assertFalse(list(root.glob("live.sqlite3-shm.restore-*.tmp")))

    def test_restore_succeeds_if_committed_sidecar_cleanup_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "live.sqlite3"
            snapshot_source = root / "snapshot-source.sqlite3"
            backup_dir = root / "backups"

            def create_valid_database(path, value):
                db = sqlite3.connect(path)
                db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES(1,1)")
                db.execute("CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
                db.execute("""CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT, username TEXT,
                    password_hash TEXT, role TEXT, active INTEGER, created_at INTEGER)""")
                db.execute("CREATE TABLE business_records(value TEXT NOT NULL)")
                db.execute("INSERT INTO business_records VALUES(?)", (value,))
                db.commit()
                db.close()

            create_valid_database(database, "old-live-value")
            create_valid_database(snapshot_source, "restored-value")
            snapshot = create_snapshot(snapshot_source, backup_dir)
            wal = Path(str(database) + "-wal")
            wal.write_bytes(b"old-wal-state")
            original_unlink = Path.unlink

            def deny_preserved_sidecar_cleanup(path, *args, **kwargs):
                if ".restore-" in path.name and path.name.endswith(".tmp"):
                    raise PermissionError("simulated cleanup permission failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch("backup_manager.Path.unlink", autospec=True,
                            side_effect=deny_preserved_sidecar_cleanup):
                restored = restore_snapshot(snapshot, database, backup_dir)

            self.assertEqual(restored, database.resolve())
            db = sqlite3.connect(database)
            self.assertEqual(db.execute("SELECT value FROM business_records").fetchone()[0],
                             "restored-value")
            db.close()
            self.assertEqual(len(list(root.glob("live.sqlite3-wal.restore-*.tmp"))), 1)

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
