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
