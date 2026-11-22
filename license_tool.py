#!/usr/bin/env python3
"""Generate signed MG Business module license codes."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from module_catalog import MODULE_KEYS, resolve_module_dependencies

MODULES = set(MODULE_KEYS)

def main():
    parser = argparse.ArgumentParser(description="Issue a signed MG Business license")
    parser.add_argument("--modules", required=True, help="Comma-separated module keys")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--plan", default="custom")
    parser.add_argument("--license-id", default=None)
    args = parser.parse_args()
    secret = os.environ.get("MG_LICENSE_SECRET")
    if not secret or len(secret) < 32:
        parser.error("Set MG_LICENSE_SECRET to a strong secret of at least 32 characters.")
    requested = sorted(set(x.strip() for x in args.modules.split(",") if x.strip()))
    unknown = set(requested) - MODULES
    if unknown:
        parser.error("Unknown module keys: " + ", ".join(sorted(unknown)))
    modules = resolve_module_dependencies(requested)
    license_id = args.license_id if args.license_id is not None else secrets.token_urlsafe(18)
    if not modules or not 1 <= args.days <= 3650:
        parser.error("Choose at least one module and a license duration between 1 and 3650 days.")
    if not 1 <= len(license_id) <= 100:
        parser.error("License ID must contain between 1 and 100 characters.")
    payload = {
        "license_id": license_id,
        "modules": modules,
        "plan": args.plan[:80],
        "expires_at": int(time.time()) + args.days * 86400
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":"),sort_keys=True).encode()).decode().rstrip("=")
    signed = ("MG1." + encoded).encode()
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(),signed,hashlib.sha256).digest()).decode().rstrip("=")
    print("MG1." + encoded + "." + signature)

if __name__ == "__main__":
    main()
