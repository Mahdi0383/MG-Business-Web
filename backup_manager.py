#!/usr/bin/env python3
"""Atomic SQLite snapshots with optional streaming AES-256-GCM encryption."""
from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import time
from pathlib import Path

MAGIC = b"MGBK1"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024


def decode_encryption_key(value: str | bytes | None) -> bytes | None:
    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError("Backup encryption key must contain exactly 32 bytes.")
        return value
    if not isinstance(value, str):
        raise ValueError("Backup encryption key must be a URL-safe base64 string.")
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Backup encryption key is not valid URL-safe base64.") from exc
    if len(key) != 32:
        raise ValueError("Backup encryption key must decode to exactly 32 bytes.")
    return key


def generate_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        output_file.write(MAGIC + nonce)
        while True:
            chunk = input_file.read(CHUNK_BYTES)
            if not chunk:
                break
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)
        output_file.flush()
        os.fsync(output_file.fileno())


def _decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    size = source.stat().st_size
    if size < len(MAGIC) + NONCE_BYTES + TAG_BYTES:
        raise ValueError("Encrypted backup is truncated.")
    with source.open("rb") as input_file:
        header = input_file.read(len(MAGIC) + NONCE_BYTES)
        if header[:len(MAGIC)] != MAGIC:
            raise ValueError("Encrypted backup format is not recognized.")
        nonce = header[len(MAGIC):]
        input_file.seek(-TAG_BYTES, os.SEEK_END)
        tag = input_file.read(TAG_BYTES)
        ciphertext_length = size - len(header) - TAG_BYTES
        input_file.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = ciphertext_length
        with destination.open("xb") as output_file:
            while remaining:
                chunk = input_file.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("Encrypted backup is truncated.")
                remaining -= len(chunk)
                output_file.write(decryptor.update(chunk))
            # GCM authentication is verified here before the plaintext is trusted.
            output_file.write(decryptor.finalize())
            output_file.flush()
            os.fsync(output_file.fileno())


def _is_encrypted(path: Path) -> bool:
    with path.open("rb") as source:
        return source.read(len(MAGIC)) == MAGIC


def create_snapshot(
    db_path: str | Path,
    backup_dir: str | Path,
    retention: int = 14,
    encryption_key: str | bytes | None = None,
) -> Path:
    source_path = Path(db_path).expanduser().resolve()
    target_dir = Path(backup_dir).expanduser().resolve()
    key = decode_encryption_key(encryption_key)
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
    suffix = ".sqlite3.enc" if key else ".sqlite3"
    final_path = target_dir / f"mg-business-{stamp}-{secrets.token_hex(4)}{suffix}"
    temp_db = target_dir / (final_path.name + ".plain.tmp")
    temp_output = target_dir / (final_path.name + ".tmp")
    source = destination = None
    try:
        source = sqlite3.connect(str(source_path), timeout=30)
        source.execute("PRAGMA busy_timeout=30000")
        destination = sqlite3.connect(str(temp_db), timeout=30)
        source.backup(destination, pages=256, sleep=0.05)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError("SQLite backup integrity check failed.")
        destination.close()
        destination = None
        try:
            os.chmod(temp_db, 0o600)
        except OSError:
            pass
        if key:
            _encrypt_file(temp_db, temp_output, key)
            try:
                os.chmod(temp_output, 0o600)
            except OSError:
                pass
            os.replace(temp_output, final_path)
            temp_db.unlink()
        else:
            os.replace(temp_db, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
    except Exception:
        if destination is not None:
            destination.close()
        for temporary in (temp_db, temp_output):
            if temporary.exists():
                temporary.unlink()
        raise
    finally:
        if source is not None:
            source.close()

    snapshots = sorted(
        (p for p in target_dir.glob("mg-business-*") if p.is_file()
         and (p.name.endswith(".sqlite3") or p.name.endswith(".sqlite3.enc"))),
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
    required_columns = {
        "schema_migrations": {"version", "applied_at"},
        "tenants": {"id", "name", "created_at"},
        "users": {"id", "tenant_id", "username", "password_hash", "role", "active", "created_at"},
    }
    for table, expected in required_columns.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not expected.issubset(actual):
            raise sqlite3.DatabaseError(f"Selected file has an invalid MG Business {table} schema.")
    migration = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    if not migration or migration[0] is None or migration[0] < 1:
        raise sqlite3.DatabaseError("Selected file has no applied MG Business schema migration.")


def restore_snapshot(
    snapshot_path: str | Path,
    db_path: str | Path,
    safety_backup_dir: str | Path | None = None,
    encryption_key: str | bytes | None = None,
) -> Path:
    snapshot = Path(snapshot_path).expanduser().resolve()
    target = Path(db_path).expanduser().resolve()
    key = decode_encryption_key(encryption_key)
    if not snapshot.is_file():
        raise FileNotFoundError("Selected MG snapshot does not exist.")
    if snapshot == target:
        raise ValueError("Snapshot and live database paths must be different.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    decrypted_path = target.parent / (target.name + ".decrypt-" + secrets.token_hex(6) + ".tmp")
    restore_source = snapshot
    if _is_encrypted(snapshot):
        if key is None:
            raise ValueError("This backup is encrypted; set MG_BACKUP_ENCRYPTION_KEY to restore it.")
        try:
            _decrypt_file(snapshot, decrypted_path, key)
            try:
                os.chmod(decrypted_path, 0o600)
            except OSError:
                pass
            restore_source = decrypted_path
        except Exception:
            if decrypted_path.exists():
                decrypted_path.unlink()
            raise

    check = sqlite3.connect(str(restore_source), timeout=30)
    try:
        _validate_mg_database(check)
    finally:
        check.close()

    if target.exists():
        backup_dir = Path(safety_backup_dir).expanduser().resolve() if safety_backup_dir else target.parent / "backups"
        create_snapshot(target, backup_dir, retention=365, encryption_key=key)

    temp_path = target.parent / (target.name + ".restore-" + secrets.token_hex(6) + ".tmp")
    source = destination = None
    try:
        source = sqlite3.connect(str(restore_source), timeout=30)
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
        if decrypted_path.exists():
            decrypted_path.unlink()
    return target


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Create or restore MG Business SQLite snapshots.")
    parser.add_argument("--generate-key", action="store_true", help="print a new random backup encryption key")
    parser.add_argument("--db", default=os.environ.get("MG_DB_PATH", str(Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).expanduser() / "mg-business.sqlite3")))
    parser.add_argument("--backup-dir", default=os.environ.get("MG_BACKUP_DIR", str(Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).expanduser() / "backups")))
    parser.add_argument("--restore", help="Path to a verified snapshot to restore; stop the MG server first.")
    parser.add_argument("--retention", type=int, default=int(os.environ.get("MG_BACKUP_RETENTION", "14")))
    args = parser.parse_args()
    if args.generate_key:
        print(generate_encryption_key())
    elif args.restore:
        print("Restored database:", restore_snapshot(args.restore, args.db, args.backup_dir,
                                                     os.environ.get("MG_BACKUP_ENCRYPTION_KEY")))
    else:
        print("Created database snapshot:", create_snapshot(args.db, args.backup_dir, args.retention,
                                                            os.environ.get("MG_BACKUP_ENCRYPTION_KEY")))
