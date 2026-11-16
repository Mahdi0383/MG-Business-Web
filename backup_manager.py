#!/usr/bin/env python3
"""Atomic, permission-restricted SQLite snapshots for MG Business."""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from pathlib import Path


def create_snapshot(db_path: str | Path, backup_dir: str | Path, retention: int = 14) -> Path:
    source_path = Path(db_path).expanduser().resolve()
    target_dir = Path(backup_dir).expanduser().resolve()
    if type(retention) is not int or not 1 <= retention <= 365:
        raise ValueError("Backup retention must be between 1 and 365 snapshots.")
    if not source_path.is_file():
        raise FileNotFoundError("MG database file does not exist.")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target_dir, 0o700)
    except OSError:
        pass

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    final_path = target_dir / f"mg-business-{stamp}-{secrets.token_hex(4)}.sqlite3"
    temp_path = target_dir / (final_path.name + ".tmp")
    source = destination = None
    try:
        source = sqlite3.connect(str(source_path), timeout=30)
        source.execute("PRAGMA busy_timeout=30000")
        destination = sqlite3.connect(str(temp_path), timeout=30)
        source.backup(destination, pages=256, sleep=0.05)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError("SQLite backup integrity check failed.")
        destination.close()
        destination = None
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
    except Exception:
        if destination is not None:
            destination.close()
        if temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        if source is not None:
            source.close()

    snapshots = sorted(
        (p for p in target_dir.glob("mg-business-*.sqlite3") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in snapshots[retention:]:
        try:
            old.unlink()
        except OSError:
            pass
    return final_path



def _validate_mg_database(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    required = {"schema_migrations", "tenants", "users"}
    if not integrity or integrity[0] != "ok" or violations or not required.issubset(tables):
        raise sqlite3.DatabaseError("Selected file is not a valid MG Business database snapshot.")


def restore_snapshot(snapshot_path: str | Path, db_path: str | Path, safety_backup_dir: str | Path | None = None) -> Path:
    snapshot = Path(snapshot_path).expanduser().resolve()
    target = Path(db_path).expanduser().resolve()
    if not snapshot.is_file():
        raise FileNotFoundError("Selected MG snapshot does not exist.")
    if snapshot == target:
        raise ValueError("Snapshot and live database paths must be different.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    check = sqlite3.connect(str(snapshot), timeout=30)
    try:
        _validate_mg_database(check)
    finally:
        check.close()

    if target.exists():
        backup_dir = Path(safety_backup_dir).expanduser().resolve() if safety_backup_dir else target.parent / "backups"
        create_snapshot(target, backup_dir, retention=365)

    temp_path = target.parent / (target.name + ".restore-" + secrets.token_hex(6) + ".tmp")
    source = destination = None
    try:
        source = sqlite3.connect(str(snapshot), timeout=30)
        destination = sqlite3.connect(str(temp_path), timeout=30)
        source.backup(destination, pages=256, sleep=0.05)
        _validate_mg_database(destination)
        destination.close()
        destination = None
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        os.replace(temp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        if destination is not None:
            destination.close()
        if temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        if source is not None:
            source.close()
    return target


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Create or restore MG Business SQLite snapshots.")
    data_dir = Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).expanduser()
    parser.add_argument("--db", default=os.environ.get("MG_DB_PATH", str(data_dir / "mg-business.sqlite3")))
    parser.add_argument("--backup-dir", default=os.environ.get("MG_BACKUP_DIR", str(data_dir / "backups")))
    parser.add_argument("--restore", help="Path to a verified snapshot to restore; stop the MG server first.")
    parser.add_argument("--retention", type=int, default=int(os.environ.get("MG_BACKUP_RETENTION", "14")))
    args = parser.parse_args()
    if args.restore:
        print("Restored database:", restore_snapshot(args.restore, args.db, args.backup_dir))
    else:
        print("Created database snapshot:", create_snapshot(args.db, args.backup_dir, args.retention))
