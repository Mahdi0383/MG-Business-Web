#!/usr/bin/env python3
"""Fail-fast deployment configuration checks for MG Business Web."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse


def validate_config(env: dict[str, str], root: Path) -> list[str]:
    errors: list[str] = []
    root = root.resolve()
    environment = env.get("MG_ENV", "development").strip().lower()
    if environment not in {"development", "test", "production"}:
        errors.append("MG_ENV must be one of: development, test, production.")
    host = env.get("MG_HOST", "127.0.0.1").strip()
    port = env.get("MG_PORT", "8080").strip()

    try:
        parsed_port = int(port)
        if not 1 <= parsed_port <= 65535:
            raise ValueError
    except ValueError:
        errors.append("MG_PORT must be an integer between 1 and 65535.")

    try:
        address = ipaddress.ip_address(host)
        public_bind = not address.is_loopback
    except ValueError:
        public_bind = host not in {"localhost"}
        if not host:
            errors.append("MG_HOST must not be empty.")

    try:
        public_url = urlparse(env.get("MG_PUBLIC_URL", "").strip())
        valid_https_url = (public_url.scheme == "https" and bool(public_url.hostname)
                           and not public_url.username and not public_url.password
                           and not public_url.fragment)
        if valid_https_url:
            valid_https_url = public_url.port is None or 1 <= public_url.port <= 65535
    except ValueError:
        valid_https_url = False

    trusted_proxy_networks = []
    for proxy in env.get("MG_TRUSTED_PROXIES", "").split(","):
        proxy = proxy.strip()
        if proxy:
            try:
                trusted_proxy_networks.append(ipaddress.ip_network(proxy, strict=False))
            except ValueError:
                errors.append(f"MG_TRUSTED_PROXIES contains an invalid IP address or CIDR: {proxy}")
    if environment == "production" and not trusted_proxy_networks:
        errors.append("Production requires MG_TRUSTED_PROXIES to identify the actual TLS reverse proxy; otherwise client IP rate limits cannot be enforced correctly.")
    if public_bind and not valid_https_url:
        errors.append("A non-loopback MG_HOST requires MG_PUBLIC_URL with a valid https:// URL.")
    if environment == "production":
        license_secret = env.get("MG_LICENSE_SECRET", "").strip()
        placeholder_secret = license_secret.lower().startswith(("replace_", "replace-", "change_me", "your_", "example"))
        if len(license_secret) < 32 or placeholder_secret:
            errors.append("Production requires a real MG_LICENSE_SECRET with at least 32 characters; template placeholders are not accepted.")
        if not valid_https_url:
            errors.append("Production requires MG_PUBLIC_URL with a valid https:// URL.")
        if public_bind:
            errors.append("Production MG_HOST must bind to loopback behind a TLS-terminating reverse proxy; direct public HTTP binding is not allowed.")

    paths = {
        "MG_DATA_DIR": Path(env.get("MG_DATA_DIR", str(Path.home() / ".mg-business-data"))).expanduser(),
        "MG_DB_PATH": Path(env.get("MG_DB_PATH", str(Path.home() / ".mg-business-data" / "mg-business.sqlite3"))).expanduser(),
        "MG_BACKUP_DIR": Path(env.get("MG_BACKUP_DIR", str(Path.home() / ".mg-business-data" / "backups"))).expanduser(),
    }
    resolved = {name: path.resolve() for name, path in paths.items()}
    for name, path in resolved.items():
        if path == root or root in path.parents:
            errors.append(f"{name} must be outside the public application directory.")
    if resolved["MG_BACKUP_DIR"] == resolved["MG_DB_PATH"] or resolved["MG_BACKUP_DIR"] in resolved["MG_DB_PATH"].parents:
        errors.append("MG_BACKUP_DIR must not contain or replace the live database file.")
    if resolved["MG_BACKUP_DIR"] == resolved["MG_DATA_DIR"]:
        errors.append("MG_BACKUP_DIR must be a dedicated subdirectory, not MG_DATA_DIR itself.")
    if resolved["MG_DB_PATH"] == resolved["MG_DATA_DIR"]:
        errors.append("MG_DB_PATH must identify a database file inside or outside MG_DATA_DIR, not the data directory itself.")

    try:
        interval = int(env.get("MG_BACKUP_INTERVAL_HOURS", "24"))
        retention = int(env.get("MG_BACKUP_RETENTION", "14"))
        if not 1 <= interval <= 168:
            errors.append("MG_BACKUP_INTERVAL_HOURS must be between 1 and 168.")
        if not 1 <= retention <= 365:
            errors.append("MG_BACKUP_RETENTION must be between 1 and 365.")
    except ValueError:
        errors.append("Backup interval and retention must be integers.")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--production", action="store_true", help="validate using production requirements")
    args = parser.parse_args()
    env = dict(os.environ)
    if args.production:
        env["MG_ENV"] = "production"
    errors = validate_config(env, args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("MG Business deployment configuration checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
