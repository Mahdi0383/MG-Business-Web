#!/usr/bin/env python3
"""MG Business local-first HTTP application and SQLite API (stdlib only)."""
from __future__ import annotations
import base64, hashlib, hmac, ipaddress, json, os, secrets, sqlite3, time, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from backup_manager import create_snapshot

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).resolve()
DB_PATH = Path(os.environ.get("MG_DB_PATH", DATA_DIR / "mg-business.sqlite3")).resolve()
BACKUP_DIR = Path(os.environ.get("MG_BACKUP_DIR", DATA_DIR / "backups")).expanduser().resolve()
if DATA_DIR == ROOT or ROOT in DATA_DIR.parents or DB_PATH == ROOT or ROOT in DB_PATH.parents:
    raise RuntimeError("MG_DATA_DIR and MG_DB_PATH must be outside the public web root.")
if BACKUP_DIR == ROOT or ROOT in BACKUP_DIR.parents or BACKUP_DIR == DB_PATH:
    raise RuntimeError("MG_BACKUP_DIR must be outside the public web root and separate from the database file.")
try:
    BACKUP_INTERVAL_SECONDS = int(os.environ.get("MG_BACKUP_INTERVAL_HOURS", "24")) * 3600
    BACKUP_RETENTION = int(os.environ.get("MG_BACKUP_RETENTION", "14"))
except ValueError as exc:
    raise RuntimeError("Backup interval and retention settings must be integers.") from exc
if not 3600 <= BACKUP_INTERVAL_SECONDS <= 168 * 3600 or not 1 <= BACKUP_RETENTION <= 365:
    raise RuntimeError("Backup interval must be 1-168 hours and retention 1-365 snapshots.")
HOST = os.environ.get("MG_HOST", "127.0.0.1")
PORT = int(os.environ.get("MG_PORT", "8080"))
SESSION_SECONDS = 60 * 60 * 24 * 7
PBKDF2_ROUNDS = 310_000
MAX_BODY = 2 * 1024 * 1024
MAX_BACKUP_BODY = 50 * 1024 * 1024
MODULE_KEYS = ('cafe','gamenet','accounting','customers','inventory','reports','settings')
LOGIN_FAILURES = {}
LOGIN_IP_FAILURES = {}
LOGIN_LOCK = threading.Lock()
PUBLIC_ORDER_FAILURES = {}
BACKUP_MAX_ROWS = 500000
SCHEMA_VERSION = 13

def allocate_proportional_discount(amounts, discount):
    """Allocate a whole-order discount to lines with deterministic largest remainders."""
    if not isinstance(amounts, (list, tuple)) or any(type(x) is not int or x < 0 for x in amounts):
        raise ValueError("line amounts must be non-negative integers")
    if type(discount) is not int or discount < 0 or discount > sum(amounts):
        raise ValueError("discount is outside the order subtotal")
    subtotal=sum(amounts)
    if subtotal == 0:
        if discount:
            raise ValueError("cannot discount a zero subtotal")
        return [0 for _ in amounts]
    allocations=[(discount*amount)//subtotal for amount in amounts]
    remainder=discount-sum(allocations)
    order=sorted(range(len(amounts)),key=lambda i:((discount*amounts[i])%subtotal,amounts[i],-i),reverse=True)
    for index in order[:remainder]:
        allocations[index]+=1
    return allocations

def remember_login_failure(key, ip, now):
    LOGIN_FAILURES.setdefault(key, []).append(now)
    LOGIN_IP_FAILURES.setdefault(ip, []).append(now)
    # Bound attacker-controlled username/IP cardinality in this process.
    while len(LOGIN_FAILURES) > 4096:
        LOGIN_FAILURES.pop(next(iter(LOGIN_FAILURES)))
    while len(LOGIN_IP_FAILURES) > 2048:
        LOGIN_IP_FAILURES.pop(next(iter(LOGIN_IP_FAILURES)))

def remember_public_order_attempt(ip, now):
    attempts = [stamp for stamp in PUBLIC_ORDER_FAILURES.get(ip, []) if now - stamp < 900]
    if len(attempts) >= 5:
        PUBLIC_ORDER_FAILURES[ip] = attempts
        return False
    attempts.append(now)
    PUBLIC_ORDER_FAILURES[ip] = attempts
    # Public IPs are attacker-controlled; cap cardinality to bound process memory.
    while len(PUBLIC_ORDER_FAILURES) > 2048:
        PUBLIC_ORDER_FAILURES.pop(next(iter(PUBLIC_ORDER_FAILURES)))
    return True

def public_order_fingerprint(customer_name, phone, normalized_lines):
    payload = {
        "customerName": customer_name,
        "phone": phone,
        "items": [
            {"productId": product_id, "addons": list(addon_ids), "quantity": quantity}
            for (product_id, addon_ids), quantity in sorted(normalized_lines.items())
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def public_order_duplicate(db, order_id, tenant_id, customer_name, phone, normalized_lines,
                           request_fingerprint=None):
    existing=db.execute("""SELECT id,order_number,total,customer_id,status,request_fingerprint FROM orders
        WHERE id=? AND tenant_id=?""",(order_id,tenant_id)).fetchone()
    if not existing:
        return None
    if existing["status"] != "paid":
        return False
    if existing["request_fingerprint"]:
        if not request_fingerprint or not hmac.compare_digest(existing["request_fingerprint"], request_fingerprint):
            return False
    else:
        # Compatibility for orders created before fingerprint migration 13.
        customer=db.execute("SELECT full_name,phone FROM customers WHERE id=? AND tenant_id=?",
                            (existing["customer_id"],tenant_id)).fetchone()
        if not customer or customer["full_name"] != customer_name or customer["phone"] != phone:
            return False
        old_items={}
        for row in db.execute("SELECT product_id,quantity,addons_json FROM order_items WHERE tenant_id=? AND order_id=?",
                              (tenant_id,order_id)).fetchall():
            try:
                saved=json.loads(row["addons_json"] or "[]")
                addon_ids=tuple(sorted(item["id"] for item in saved if isinstance(item,dict) and isinstance(item.get("id"),str)))
            except (TypeError,ValueError):
                addon_ids=()
            key=(row["product_id"],addon_ids)
            old_items[key]=old_items.get(key,0)+row["quantity"]
        if old_items != normalized_lines:
            return False
    return {"orderNumber":existing["order_number"],"total":existing["total"]}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tenants(
 id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 username TEXT NOT NULL COLLATE NOCASE UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('owner','manager','cashier','viewer')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at INTEGER NOT NULL, UNIQUE(tenant_id, username), UNIQUE(id,tenant_id)
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_global ON users(username COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS transactions(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 created_by TEXT NOT NULL REFERENCES users(id),
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
 amount INTEGER NOT NULL CHECK(amount > 0),
 kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
 category TEXT NOT NULL DEFAULT 'عمومی' CHECK(length(category) <= 80),
 occurred_at TEXT NOT NULL, created_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN ('posted','voided')),
 voided_at INTEGER, voided_by TEXT REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_transactions_tenant_date ON transactions(tenant_id, created_at DESC);
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 code TEXT NOT NULL, name TEXT NOT NULL, account_type TEXT NOT NULL
 CHECK(account_type IN ('asset','liability','equity','revenue','expense')),
 normal_balance TEXT NOT NULL CHECK(normal_balance IN ('debit','credit')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 UNIQUE(tenant_id,code), UNIQUE(id,tenant_id)
);
CREATE TABLE IF NOT EXISTS journal_entries(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 entry_date TEXT NOT NULL, memo TEXT NOT NULL,
 source_type TEXT NOT NULL, source_id TEXT NOT NULL,
 reversal_of TEXT, created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
 UNIQUE(tenant_id,source_type,source_id), UNIQUE(id,tenant_id),
 FOREIGN KEY(reversal_of,tenant_id) REFERENCES journal_entries(id,tenant_id)
);
CREATE TABLE IF NOT EXISTS journal_lines(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
 entry_id TEXT NOT NULL, account_id TEXT NOT NULL,
 debit INTEGER NOT NULL DEFAULT 0 CHECK(debit>=0),
 credit INTEGER NOT NULL DEFAULT 0 CHECK(credit>=0),
 CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),
 FOREIGN KEY(entry_id,tenant_id) REFERENCES journal_entries(id,tenant_id),
 FOREIGN KEY(account_id,tenant_id) REFERENCES accounts(id,tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_entries_tenant_date ON journal_entries(tenant_id,entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id);
CREATE TRIGGER IF NOT EXISTS journal_entries_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT,'journal entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_entries_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT,'journal entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_no_update BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END;
CREATE TABLE IF NOT EXISTS products(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 160),
 sku TEXT, category TEXT NOT NULL DEFAULT 'عمومی' CHECK(length(category)<=80),
 sell_price INTEGER NOT NULL CHECK(sell_price>=0),
 cost_price INTEGER NOT NULL DEFAULT 0 CHECK(cost_price>=0),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(tenant_id,sku)
);
CREATE INDEX IF NOT EXISTS idx_products_tenant_active ON products(tenant_id,active,name);
CREATE TABLE IF NOT EXISTS product_addons(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 product_id TEXT NOT NULL REFERENCES products(id),
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
 price INTEGER NOT NULL CHECK(price>=0),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(tenant_id,product_id,name)
);
CREATE INDEX IF NOT EXISTS idx_product_addons_tenant_product ON product_addons(tenant_id,product_id,active);
CREATE TABLE IF NOT EXISTS inventory_movements(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 product_id TEXT NOT NULL REFERENCES products(id), created_by TEXT NOT NULL REFERENCES users(id),
 movement_type TEXT NOT NULL CHECK(movement_type IN ('purchase','sale','adjustment','waste')),
 quantity_delta INTEGER NOT NULL CHECK(quantity_delta<>0),
 unit_cost INTEGER NOT NULL DEFAULT 0 CHECK(unit_cost>=0),
 note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=300),
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_tenant_product_date ON inventory_movements(tenant_id,product_id,created_at DESC);
CREATE TABLE IF NOT EXISTS suppliers(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 160),phone TEXT,email TEXT,notes TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(tenant_id,name));
CREATE TABLE IF NOT EXISTS purchase_invoices(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),supplier_id TEXT NOT NULL REFERENCES suppliers(id),created_by TEXT NOT NULL REFERENCES users(id),invoice_number TEXT NOT NULL,invoice_date TEXT NOT NULL,subtotal INTEGER NOT NULL CHECK(subtotal>0),discount INTEGER NOT NULL CHECK(discount>=0),total INTEGER NOT NULL CHECK(total>0),paid_amount INTEGER NOT NULL DEFAULT 0 CHECK(paid_amount>=0 AND paid_amount<=total),status TEXT NOT NULL CHECK(status IN ('unpaid','partial','paid')),created_at INTEGER NOT NULL,UNIQUE(tenant_id,invoice_number));
CREATE TABLE IF NOT EXISTS purchase_invoice_items(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,invoice_id TEXT NOT NULL REFERENCES purchase_invoices(id),product_id TEXT NOT NULL REFERENCES products(id),product_name TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),unit_cost INTEGER NOT NULL CHECK(unit_cost>=0),line_total INTEGER NOT NULL CHECK(line_total>=0));
CREATE TABLE IF NOT EXISTS supplier_payments(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,invoice_id TEXT NOT NULL REFERENCES purchase_invoices(id),supplier_id TEXT NOT NULL REFERENCES suppliers(id),created_by TEXT NOT NULL REFERENCES users(id),payment_method TEXT NOT NULL CHECK(payment_method IN ('cash','card','online')),amount INTEGER NOT NULL CHECK(amount>0),created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_tenant_date ON purchase_invoices(tenant_id,invoice_date DESC);
CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_invoice ON purchase_invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_supplier_payments_invoice ON supplier_payments(tenant_id,invoice_id,created_at DESC);
CREATE TABLE IF NOT EXISTS customers(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 full_name TEXT NOT NULL CHECK(length(full_name) BETWEEN 1 AND 160),
 phone TEXT, email TEXT, notes TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(tenant_id,phone)
);
CREATE INDEX IF NOT EXISTS idx_customers_tenant_name ON customers(tenant_id,full_name);
CREATE TABLE IF NOT EXISTS customer_ledger(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), source_type TEXT NOT NULL,
 source_id TEXT NOT NULL, debit INTEGER NOT NULL DEFAULT 0 CHECK(debit>=0),
 credit INTEGER NOT NULL DEFAULT 0 CHECK(credit>=0),
 note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
 CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),
 UNIQUE(tenant_id,source_type,source_id),
 FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_ledger_customer_date ON customer_ledger(tenant_id,customer_id,created_at DESC);
CREATE TABLE IF NOT EXISTS loyalty_programs(
 tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 spend_amount INTEGER NOT NULL DEFAULT 100000 CHECK(spend_amount BETWEEN 10000 AND 1000000000000),
 points_awarded INTEGER NOT NULL DEFAULT 1 CHECK(points_awarded BETWEEN 1 AND 1000),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_loyalty_ledger(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 customer_id TEXT NOT NULL REFERENCES customers(id),
 created_by TEXT NOT NULL REFERENCES users(id),
 source_type TEXT NOT NULL CHECK(source_type IN ('order_earn','order_reversal')),
 source_id TEXT NOT NULL, points_delta INTEGER NOT NULL CHECK(points_delta<>0),
 note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
 UNIQUE(tenant_id,source_type,source_id)
);
CREATE INDEX IF NOT EXISTS idx_customer_loyalty_customer_date
 ON customer_loyalty_ledger(tenant_id,customer_id,created_at DESC);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 order_number TEXT NOT NULL, customer_id TEXT, created_by TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN ('paid','cancelled')),
 subtotal INTEGER NOT NULL CHECK(subtotal>=0), discount INTEGER NOT NULL CHECK(discount>=0),
 total INTEGER NOT NULL CHECK(total>0), payment_method TEXT NOT NULL
 CHECK(payment_method IN ('cash','card','online','credit')), created_at INTEGER NOT NULL,
 UNIQUE(tenant_id,order_number),
 FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_tenant_date ON orders(tenant_id,created_at DESC);
CREATE TABLE IF NOT EXISTS game_devices(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 device_type TEXT NOT NULL CHECK(device_type IN ('pc','console','other')),
 hourly_rate INTEGER NOT NULL CHECK(hourly_rate>=0),
 is_free INTEGER NOT NULL DEFAULT 0 CHECK(is_free IN (0,1)),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at INTEGER NOT NULL, UNIQUE(tenant_id,name)
);
CREATE TABLE IF NOT EXISTS game_sessions(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 device_id TEXT NOT NULL REFERENCES game_devices(id),
 customer_id TEXT REFERENCES customers(id),
 started_by TEXT NOT NULL REFERENCES users(id),
 started_at INTEGER NOT NULL, ended_at INTEGER,
 status TEXT NOT NULL CHECK(status IN ('running','ended','cancelled')),
 players_count INTEGER NOT NULL CHECK(players_count BETWEEN 1 AND 16),
 rate_per_hour INTEGER NOT NULL CHECK(rate_per_hour>=0),
 is_free INTEGER NOT NULL CHECK(is_free IN (0,1)),
 amount_due INTEGER NOT NULL DEFAULT 0 CHECK(amount_due>=0),
 payment_method TEXT NOT NULL DEFAULT 'cash' CHECK(payment_method IN ('cash','card','online','credit')),
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_sessions_tenant_start ON game_sessions(tenant_id,started_at DESC);
CREATE TABLE IF NOT EXISTS game_session_segments(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 session_id TEXT NOT NULL REFERENCES game_sessions(id) ON DELETE CASCADE,
 device_id TEXT NOT NULL REFERENCES game_devices(id),
 started_at INTEGER NOT NULL, ended_at INTEGER,
 players_count INTEGER NOT NULL CHECK(players_count BETWEEN 1 AND 16),
 rate_per_hour INTEGER NOT NULL CHECK(rate_per_hour>=0),
 is_free INTEGER NOT NULL CHECK(is_free IN (0,1)),
 CHECK(ended_at IS NULL OR ended_at>=started_at)
);
CREATE INDEX IF NOT EXISTS idx_game_session_segments_session
 ON game_session_segments(tenant_id,session_id,started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_game_one_running_per_device ON game_sessions(tenant_id,device_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS gamenet_reservations(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 device_id TEXT NOT NULL REFERENCES game_devices(id), customer_id TEXT REFERENCES customers(id),
 created_by TEXT NOT NULL REFERENCES users(id), starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('booked','cancelled','completed')),
 players_count INTEGER NOT NULL CHECK(players_count BETWEEN 1 AND 16),
 notes TEXT NOT NULL DEFAULT '' CHECK(length(notes)<=500), created_at INTEGER NOT NULL,
 CHECK(ends_at>starts_at)
);
CREATE INDEX IF NOT EXISTS idx_gamenet_reservations_device_time
 ON gamenet_reservations(tenant_id,device_id,status,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS order_items(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL,
 product_id TEXT NOT NULL, product_name TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_price INTEGER NOT NULL CHECK(unit_price>=0),
 unit_cost INTEGER NOT NULL CHECK(unit_cost>=0), line_total INTEGER NOT NULL CHECK(line_total>=0),
 discount_amount INTEGER NOT NULL DEFAULT 0 CHECK(discount_amount>=0 AND discount_amount<=line_total),
 addons_json TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY(order_id) REFERENCES orders(id),
 FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS licenses(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 code_hash TEXT NOT NULL UNIQUE, plan TEXT NOT NULL, expires_at INTEGER NOT NULL,
 activated_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','expired','revoked'))
);
CREATE TABLE IF NOT EXISTS tenant_modules(
 tenant_id TEXT NOT NULL REFERENCES tenants(id), module_key TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), expires_at INTEGER,
 license_id TEXT, updated_at INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,module_key)
);
CREATE TABLE IF NOT EXISTS audit_log(
 id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 actor_id TEXT REFERENCES users(id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT, details_json TEXT NOT NULL DEFAULT '{}',
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_date ON audit_log(tenant_id, created_at DESC);
"""

def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(DB_PATH.parent, 0o700)
    except OSError:
        pass
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    order_item_columns = {row["name"] for row in db.execute("PRAGMA table_info(order_items)").fetchall()}
    if "addons_json" not in order_item_columns:
        db.execute("ALTER TABLE order_items ADD COLUMN addons_json TEXT NOT NULL DEFAULT '[]'")
    if "discount_amount" not in order_item_columns:
        db.execute("ALTER TABLE order_items ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0 CHECK(discount_amount>=0)")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)").fetchall()}
    if "status" not in columns:
        db.execute("ALTER TABLE transactions ADD COLUMN status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN ('posted','voided'))")
    if "voided_at" not in columns:
        db.execute("ALTER TABLE transactions ADD COLUMN voided_at INTEGER")
    if "voided_by" not in columns:
        db.execute("ALTER TABLE transactions ADD COLUMN voided_by TEXT REFERENCES users(id)")
    db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (int(time.time()),))
    db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
        for tenant in db.execute("SELECT id FROM tenants").fetchall():
            seed_accounts(db, tenant["id"])
        legacy = db.execute("""SELECT t.id,t.tenant_id,t.created_by,t.title,t.amount,t.kind,t.occurred_at
            FROM transactions t LEFT JOIN journal_entries e
            ON e.tenant_id=t.tenant_id AND e.source_type='transaction' AND e.source_id=t.id
            WHERE t.status='posted' AND e.id IS NULL""").fetchall()
        for tx in legacy:
            post_transaction_journal(db,tx["tenant_id"],tx["created_by"],tx["id"],tx["title"],
                                     tx["amount"],tx["kind"],tx["occurred_at"])
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
        for tenant in db.execute("SELECT id FROM tenants").fetchall():
            seed_accounts(db, tenant["id"])
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(4,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone():
        for tenant in db.execute("SELECT id FROM tenants").fetchall():
            seed_trial_modules(db, tenant["id"])
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(5,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone():
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(6,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone():
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(7,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone():
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(8,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=9").fetchone():
        legacy_sessions = db.execute("SELECT * FROM game_sessions").fetchall()
        for session in legacy_sessions:
            db.execute("""INSERT OR IGNORE INTO game_session_segments
                (id,tenant_id,session_id,device_id,started_at,ended_at,players_count,rate_per_hour,is_free)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                ("legacy-"+session["id"],session["tenant_id"],session["id"],session["device_id"],
                 session["started_at"],session["ended_at"],session["players_count"],
                 session["rate_per_hour"],session["is_free"]))
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(9,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=10").fetchone():
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(10,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=11").fetchone():
        session_columns={row["name"] for row in db.execute("PRAGMA table_info(game_sessions)").fetchall()}
        if "payment_method" not in session_columns:
            db.execute("ALTER TABLE game_sessions ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'cash' CHECK(payment_method IN ('cash','card','online','credit'))")
            db.execute("""UPDATE game_sessions SET payment_method='credit'
                WHERE status='ended' AND customer_id IS NOT NULL AND amount_due>0""")
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(11,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=12").fetchone():
        legacy_orders=db.execute("SELECT id,tenant_id,discount FROM orders WHERE discount>0").fetchall()
        for legacy_order in legacy_orders:
            legacy_items=db.execute("""SELECT id,line_total FROM order_items
                WHERE tenant_id=? AND order_id=? ORDER BY id""",
                (legacy_order["tenant_id"],legacy_order["id"])).fetchall()
            if legacy_items:
                allocations=allocate_proportional_discount(
                    [row["line_total"] for row in legacy_items],legacy_order["discount"])
                for item,allocated in zip(legacy_items,allocations):
                    db.execute("UPDATE order_items SET discount_amount=? WHERE id=?",
                        (allocated,item["id"]))
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(12,?)", (int(time.time()),))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=13").fetchone():
        order_columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
        if "request_fingerprint" not in order_columns:
            db.execute("ALTER TABLE orders ADD COLUMN request_fingerprint TEXT")
        legacy_public_orders = db.execute("""SELECT o.id,o.tenant_id,c.full_name,c.phone
            FROM orders o LEFT JOIN customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id
            WHERE o.id LIKE 'public-%' AND o.request_fingerprint IS NULL""").fetchall()
        for legacy_order in legacy_public_orders:
            if not legacy_order["full_name"] or not legacy_order["phone"]:
                continue
            normalized_lines = {}
            for item in db.execute("""SELECT product_id,quantity,addons_json FROM order_items
                WHERE tenant_id=? AND order_id=?""",
                (legacy_order["tenant_id"],legacy_order["id"])).fetchall():
                try:
                    saved_addons = json.loads(item["addons_json"] or "[]")
                    if not isinstance(saved_addons,list):
                        saved_addons = []
                    addon_ids = tuple(sorted(
                        addon["id"] for addon in saved_addons
                        if isinstance(addon,dict) and isinstance(addon.get("id"),str)
                    ))
                except (TypeError,ValueError):
                    addon_ids = ()
                key = (item["product_id"],addon_ids)
                normalized_lines[key] = normalized_lines.get(key,0) + item["quantity"]
            if normalized_lines:
                fingerprint = public_order_fingerprint(
                    legacy_order["full_name"],legacy_order["phone"],normalized_lines)
                db.execute("UPDATE orders SET request_fingerprint=? WHERE id=? AND tenant_id=?",
                           (fingerprint,legacy_order["id"],legacy_order["tenant_id"]))
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(13,?)", (int(time.time()),))
    db.commit()
    return db

def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), digest.hex())

def verify_password(password: str, encoded: str) -> bool:
    try:
        if not isinstance(password, str) or len(password) > 256 or not isinstance(encoded, str):
            return False
        algorithm, rounds_text, salt_hex, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not rounds_text.isascii() or not rounds_text.isdecimal():
            return False
        rounds = int(rounds_text)
        # Bound work for untrusted/corrupted stored hashes; current hashes use 310,000 rounds.
        if not 100_000 <= rounds <= 1_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        if len(salt) != 16 or len(expected) != 64:
            return False
        bytes.fromhex(expected)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, OverflowError):
        return False

def seed_accounts(db, tenant_id):
    defaults = [
        ("1000", "صندوق نقدی", "asset", "debit"),
        ("1010", "وجوه در راه کارت‌خوان", "asset", "debit"),
        ("1020", "وجوه در راه پرداخت آنلاین", "asset", "debit"),
        ("1100", "حساب‌های دریافتنی", "asset", "debit"),
        ("1200", "موجودی کالا", "asset", "debit"),
        ("2000", "حساب‌های پرداختنی", "liability", "credit"),
        ("3000", "سرمایه", "equity", "credit"),
        ("4000", "درآمد فروش", "revenue", "credit"),
        ("4100", "سایر درآمدها", "revenue", "credit"),
        ("5000", "هزینه‌های عملیاتی", "expense", "debit"),
        ("5100", "بهای تمام‌شده کالای فروش‌رفته", "expense", "debit"),
        ("5200", "ضایعات و کسری انبار", "expense", "debit"),
    ]
    for code, name, account_type, normal in defaults:
        db.execute("INSERT OR IGNORE INTO accounts(id,tenant_id,code,name,account_type,normal_balance) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,code,name,account_type,normal))

def post_transaction_journal(db, tenant_id, user_id, tx_id, title, amount, kind, occurred_at, debit_account_code='1000'):
    accounts = {row["code"]: row["id"] for row in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1", (tenant_id,))}
    cash, counterpart = accounts[debit_account_code] if kind == "income" else accounts["1000"], accounts["4000"] if kind == "income" else accounts["5000"]
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,occurred_at[:10],title,"transaction",tx_id,user_id,now))
    lines = ((cash,amount,0),(counterpart,0,amount)) if kind == "income" else ((counterpart,amount,0),(cash,0,amount))
    for account_id, debit, credit in lines:
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,entry_id,account_id,debit,credit))

def post_inventory_movement_journal(db, tenant_id, user_id, movement_id, movement_type, quantity_delta, unit_cost, note=''):
    amount = abs(quantity_delta) * unit_cost
    if amount <= 0:
        return
    accounts = {row["code"]: row["id"] for row in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1", (tenant_id,))}
    if movement_type == "purchase":
        lines = ((accounts["1200"],amount,0),(accounts["2000"],0,amount))
    elif movement_type == "sale":
        lines = ((accounts["5100"],amount,0),(accounts["1200"],0,amount))
    elif movement_type == "waste":
        lines = ((accounts["5200"],amount,0),(accounts["1200"],0,amount))
    elif movement_type == "adjustment" and note.startswith("برگشت سفارش"):
        lines = ((accounts["1200"],amount,0),(accounts["5100"],0,amount))
    elif quantity_delta > 0:
        lines = ((accounts["1200"],amount,0),(accounts["4100"],0,amount))
    else:
        lines = ((accounts["5200"],amount,0),(accounts["1200"],0,amount))
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,time.strftime("%Y-%m-%d"),"گردش انبار "+movement_id,
                                      "inventory_movement",movement_id,user_id,now))
    for account_id,debit,credit in lines:
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,entry_id,account_id,debit,credit))

def post_customer_payment_journal(db, tenant_id, user_id, payment_id, customer_id, amount, payment_method):
    accounts = {row["code"]: row["id"] for row in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1", (tenant_id,))}
    cash_code = {"cash":"1000","card":"1010","online":"1020"}[payment_method]
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,time.strftime("%Y-%m-%d"),"دریافت از مشتری "+customer_id,
                                      "customer_payment",payment_id,user_id,now))
    for account_id,debit,credit in ((accounts[cash_code],amount,0),(accounts["1100"],0,amount)):
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,entry_id,account_id,debit,credit))


def award_customer_loyalty_points(db, tenant_id, customer_id, actor_id, order_id, amount, now):
    if not customer_id or amount <= 0:
        return 0
    program = db.execute("""SELECT enabled,spend_amount,points_awarded
        FROM loyalty_programs WHERE tenant_id=?""", (tenant_id,)).fetchone()
    if not program or not program["enabled"]:
        return 0
    points = (int(amount) // int(program["spend_amount"])) * int(program["points_awarded"])
    if points <= 0:
        return 0
    if points > 9007199254740991:
        raise ValueError("loyalty points exceed the supported integer range")
    cursor = db.execute("""INSERT OR IGNORE INTO customer_loyalty_ledger
        (id,tenant_id,customer_id,created_by,source_type,source_id,points_delta,note,created_at)
        VALUES(?,?,?,?, 'order_earn',?,?,?,?)""",
        (secrets.token_urlsafe(18),tenant_id,customer_id,actor_id,order_id,points,
         "امتیاز خرید سفارش "+order_id,now))
    return points if cursor.rowcount else 0


def post_order_cogs_journal(db, tenant_id, user_id, order_id, cost_total):
    if cost_total <= 0:
        return
    accounts = {row["code"]: row["id"] for row in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1", (tenant_id,))}
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,time.strftime("%Y-%m-%d"),"بهای تمام‌شده سفارش "+order_id,
                                      "order_cogs",order_id,user_id,now))
    for account_id,debit,credit in ((accounts["5100"],cost_total,0),(accounts["1200"],0,cost_total)):
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,entry_id,account_id,debit,credit))

def reverse_transaction_journal(db, tenant_id, user_id, tx_id):
    original = db.execute("SELECT * FROM journal_entries WHERE tenant_id=? AND source_type='transaction' AND source_id=?",
                          (tenant_id,tx_id)).fetchone()
    if not original:
        raise sqlite3.IntegrityError("Posted transaction is missing its original journal entry.")
    lines = db.execute("SELECT account_id,debit,credit FROM journal_lines WHERE tenant_id=? AND entry_id=?",
                       (tenant_id,original["id"])).fetchall()
    if len(lines) < 2:
        raise sqlite3.IntegrityError("Original transaction journal is incomplete.")
    debit_total = sum(line["debit"] for line in lines)
    credit_total = sum(line["credit"] for line in lines)
    if debit_total <= 0 or debit_total != credit_total:
        raise sqlite3.IntegrityError("Original transaction journal is not balanced.")
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,reversal_of,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,time.strftime("%Y-%m-%d"),"برگشت تراکنش "+tx_id,
                                       "transaction_reversal",tx_id,original["id"],user_id,now))
    lines = db.execute("SELECT account_id,debit,credit FROM journal_lines WHERE tenant_id=? AND entry_id=?",
                       (tenant_id,original["id"])).fetchall()
    for line in lines:
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,entry_id,line["account_id"],line["credit"],line["debit"]))

def module_enabled(db, tenant_id, module_key):
    row = db.execute("SELECT enabled,expires_at FROM tenant_modules WHERE tenant_id=? AND module_key=?",
                     (tenant_id,module_key)).fetchone()
    return bool(row and row["enabled"] and (row["expires_at"] is None or row["expires_at"] > int(time.time())))

def require_any_module(db, tenant_id, *module_keys):
    return any(module_enabled(db,tenant_id,key) for key in module_keys)

def verify_license_code(code):
    secret = os.environ.get("MG_LICENSE_SECRET")
    if not secret or len(secret) < 32:
        raise ValueError("کلید صادرکننده لایسنس باید حداقل ۳۲ نویسه و روی سرور تنظیم شده باشد.")
    if not isinstance(code,str) or len(code) > 4096:
        raise ValueError("کد لایسنس معتبر نیست.")
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != "MG1":
        raise ValueError("ساختار کد لایسنس معتبر نیست.")
    signed = (parts[0]+"."+parts[1]).encode()
    expected = base64.urlsafe_b64encode(hmac.new(secret.encode(),signed,hashlib.sha256).digest()).decode().rstrip("=")
    try:
        signature_ok = hmac.compare_digest(expected,parts[2])
    except TypeError:
        signature_ok = False
    if not signature_ok:
        raise ValueError("امضای کد لایسنس معتبر نیست.")
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        raise ValueError("محتوای کد لایسنس معتبر نیست.")
    if not isinstance(payload,dict):
        raise ValueError("محتوای کد لایسنس باید یک شیء معتبر باشد.")
    modules = payload.get("modules")
    expires_at = payload.get("expires_at")
    license_id = payload.get("license_id")
    if not isinstance(license_id,str) or not 1 <= len(license_id) <= 100:
        raise ValueError("شناسه لایسنس معتبر نیست.")
    if not isinstance(modules,list) or any(not isinstance(module,str) or module not in MODULE_KEYS for module in modules):
        raise ValueError("فهرست ماژول‌های لایسنس معتبر نیست.")
    if type(expires_at) is not int or expires_at <= int(time.time()):
        raise ValueError("اعتبار لایسنس پایان یافته یا تاریخ آن معتبر نیست.")
    modules = set(modules)
    if "reports" in modules:
        modules.add("accounting")
    return {"license_id":license_id,"modules":sorted(modules),"expires_at":expires_at,
            "plan":str(payload.get("plan","custom"))[:80]}

def post_supplier_payment_journal(db, tenant_id, user_id, payment_id, amount, method):
    accounts={r["code"]:r["id"] for r in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1",(tenant_id,))}
    cash_code={"cash":"1000","card":"1010","online":"1020"}[method]
    entry_id,now=secrets.token_urlsafe(18),int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",(entry_id,tenant_id,time.strftime("%Y-%m-%d"),"پرداخت به تأمین‌کننده","supplier_payment",payment_id,user_id,now))
    for account_id,debit,credit in ((accounts["2000"],amount,0),(accounts[cash_code],0,amount)):
        db.execute("INSERT INTO journal_lines(id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)",
            (secrets.token_urlsafe(16),tenant_id,entry_id,account_id,debit,credit))

def seed_trial_modules(db, tenant_id):
    expires = int(time.time()) + 14*24*60*60
    for module in ("overview",)+MODULE_KEYS:
        db.execute("""INSERT OR IGNORE INTO tenant_modules(tenant_id,module_key,enabled,expires_at,license_id,updated_at)
            VALUES(?,?,1,?,'trial',?)""", (tenant_id,module,expires,int(time.time())))

def public_user(row):
    return {"id": row["id"], "username": row["username"], "role": row["role"], "tenantId": row["tenant_id"]}

def audit(db, tenant_id, actor_id, action, entity_type, entity_id=None, details=None):
    db.execute("INSERT INTO audit_log(tenant_id,actor_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
               (tenant_id, actor_id, action, entity_type, entity_id, json.dumps(details or {}, ensure_ascii=False), int(time.time())))

class Handler(SimpleHTTPRequestHandler):
    server_version = "MGBusiness/1.0"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        resolved = Path(super().translate_path(path)).resolve()
        try:
            parts = resolved.relative_to(ROOT).parts
        except ValueError:
            return str(ROOT / "__blocked_resource__")
        blocked = any(part.startswith(".") or part in ("data","__pycache__",".venv","tests",".github") for part in parts)
        blocked = blocked or resolved.name in ("server.py","license_tool.py","backup_manager.py","README.md")
        blocked = blocked or resolved.suffix.lower() in (".sqlite",".sqlite3",".db",".env")
        return str(ROOT / "__blocked_resource__") if blocked else str(resolved)

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if (os.environ.get("MG_ENV", "development").strip().lower() == "production"
                and os.environ.get("MG_PUBLIC_URL", "").strip().lower().startswith("https://")):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: raise ValueError("طول درخواست نامعتبر است.")
        body_limit = MAX_BACKUP_BODY if self.route() == "/api/backup/restore" else MAX_BODY
        if length <= 0 or length > body_limit: raise ValueError("حجم درخواست نامعتبر است.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError): raise ValueError("JSON درخواست معتبر نیست.")
        if not isinstance(value, dict): raise ValueError("بدنه درخواست باید یک شیء JSON باشد.")
        return value

    def route(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def client_ip(self):
        peer = self.client_address[0]
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        trusted = []
        for item in os.environ.get("MG_TRUSTED_PROXIES", "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                trusted.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
        if any(peer_address in network for network in trusted):
            forwarded = self.headers.get("X-Real-IP", "").strip()
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return str(peer_address)

    def authenticate(self, db):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "): return None
        token = header[7:].strip()
        if len(token) < 32: return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        row = db.execute("""SELECT u.*,s.expires_at,s.created_at AS session_created_at FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (token_hash, now)).fetchone()
        if not row:
            return None
        # Expiry in the database is the idle deadline; created_at is the hard lifetime.
        if now - row["session_created_at"] >= SESSION_SECONDS:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            db.commit()
            return None
        refreshed_expiry = min(row["session_created_at"] + SESSION_SECONDS, now + 30 * 60)
        if refreshed_expiry - row["expires_at"] >= 60:
            db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (refreshed_expiry, token_hash))
            db.commit()
        return row

    def do_GET(self):
        path = self.route()
        if not path.startswith("/api/"):
            if path == "/": self.path = "/index.html"
            return super().do_GET()
        db = connect()
        try:
            if path == "/api/health":
                return self.send_json(200, {"ok": True, "service": "mg-business", "database": "sqlite"})
            if path == "/api/ready":
                required_tables={"tenants","users","sessions","orders","order_items","products",
                    "inventory_movements","accounts","journal_entries","journal_lines","schema_migrations"}
                present={row["name"] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                missing=sorted(required_tables-present)
                integrity=db.execute("PRAGMA quick_check").fetchone()[0]
                foreign_key_error=db.execute("PRAGMA foreign_key_check").fetchone()
                migration_version=db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
                migrations_current=migration_version==SCHEMA_VERSION
                ready=not missing and integrity=="ok" and foreign_key_error is None and migrations_current
                return self.send_json(200 if ready else 503,{
                    "ready":ready,"service":"mg-business",
                    "checks":{"databaseIntegrity":integrity=="ok",
                              "foreignKeys":foreign_key_error is None,
                              "requiredSchema":not missing,"migrations":migrations_current},
                    "schemaVersion":migration_version,"expectedSchemaVersion":SCHEMA_VERSION,
                    "missingTables":missing if missing else []
                })
            if path == "/api/setup-status":
                configured = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
                return self.send_json(200, {"configured": configured})
            if path == "/api/public/menu":
                tenants = db.execute("SELECT id,name FROM tenants ORDER BY created_at LIMIT 2").fetchall()
                if len(tenants) > 1:
                    return self.send_json(409, {"error": "برای انتشار چند کسب‌وکار، ابتدا دامنه اختصاصی هر کسب‌وکار را پیکربندی کنید."})
                tenant = tenants[0] if tenants else None
                if not tenant or not module_enabled(db,tenant["id"],"cafe"):
                    return self.send_json(404, {"error": "منوی آنلاین این کسب‌وکار فعال نیست."})
                rows = db.execute("""SELECT p.id,p.name,p.category,p.sell_price AS sellPrice,
                    COALESCE(SUM(m.quantity_delta),0) AS available
                    FROM products p LEFT JOIN inventory_movements m
                    ON m.product_id=p.id AND m.tenant_id=p.tenant_id
                    WHERE p.tenant_id=? AND p.active=1 GROUP BY p.id ORDER BY p.category,p.name""",
                    (tenant["id"],)).fetchall()
                items=[dict(row) for row in rows if row["available"]>0]
                addon_rows=db.execute("""SELECT id,product_id AS productId,name,price FROM product_addons
                    WHERE tenant_id=? AND active=1 ORDER BY name""",(tenant["id"],)).fetchall()
                addons_by_product={}
                for addon in addon_rows:
                    addons_by_product.setdefault(addon["productId"],[]).append(dict(addon))
                for item in items:
                    item["addons"]=addons_by_product.get(item["id"],[])
                return self.send_json(200, {"businessName":tenant["name"],"items":items})
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path == "/api/me":
                tenant = db.execute("SELECT id,name FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
                return self.send_json(200, {"user": public_user(user), "tenant": dict(tenant)})
            if path == "/api/modules":
                rows = db.execute("""SELECT module_key,expires_at,license_id FROM tenant_modules
                    WHERE tenant_id=? AND enabled=1 AND (expires_at IS NULL OR expires_at>?)""",
                    (user["tenant_id"],int(time.time()))).fetchall()
                keys = ["overview","settings"]+[row["module_key"] for row in rows]
                return self.send_json(200, {"modules":sorted(set(keys)),
                    "expiresAt":min((row["expires_at"] for row in rows if row["expires_at"]),default=None)})
            if path == "/api/licenses/status":
                row = db.execute("SELECT plan,expires_at,status FROM licenses WHERE tenant_id=? ORDER BY activated_at DESC LIMIT 1",
                                 (user["tenant_id"],)).fetchone()
                if not row:
                    trial = db.execute("SELECT MIN(expires_at) FROM tenant_modules WHERE tenant_id=? AND license_id='trial'",
                                       (user["tenant_id"],)).fetchone()[0]
                    return self.send_json(200, {"plan":"trial","expiresAt":trial,
                        "status":"expired" if trial and trial <= int(time.time()) else "trial"})
                status = "expired" if row["expires_at"] <= int(time.time()) else row["status"]
                return self.send_json(200, {"plan":row["plan"],"expiresAt":row["expires_at"],"status":status})
            if path == "/api/backup":
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند نسخه پشتیبان کامل دریافت کند."})
                tenant_id = user["tenant_id"]
                # Keep every table in the export on one SQLite read snapshot.
                db.execute("BEGIN")
                tables = {
                    "users": "SELECT id,tenant_id,username,role,active,created_at FROM users WHERE tenant_id=?",
                    "transactions": "SELECT * FROM transactions WHERE tenant_id=?",
                    "accounts": "SELECT * FROM accounts WHERE tenant_id=?",
                    "journal_entries": "SELECT * FROM journal_entries WHERE tenant_id=?",
                    "journal_lines": "SELECT * FROM journal_lines WHERE tenant_id=?",
                    "products": "SELECT * FROM products WHERE tenant_id=?",
                    "product_addons": "SELECT * FROM product_addons WHERE tenant_id=?",
                    "customers": "SELECT * FROM customers WHERE tenant_id=?",
                    "customer_ledger": "SELECT * FROM customer_ledger WHERE tenant_id=?",
                    "loyalty_programs": "SELECT * FROM loyalty_programs WHERE tenant_id=?",
                    "customer_loyalty_ledger": "SELECT * FROM customer_loyalty_ledger WHERE tenant_id=?",
                    "inventory_movements": "SELECT * FROM inventory_movements WHERE tenant_id=?",
                    "suppliers": "SELECT * FROM suppliers WHERE tenant_id=?",
                    "purchase_invoices": "SELECT * FROM purchase_invoices WHERE tenant_id=?",
                    "purchase_invoice_items": "SELECT * FROM purchase_invoice_items WHERE tenant_id=?",
                    "supplier_payments": "SELECT * FROM supplier_payments WHERE tenant_id=?",
                    "orders": "SELECT * FROM orders WHERE tenant_id=?",
                    "order_items": "SELECT * FROM order_items WHERE tenant_id=?",
                    "game_devices": "SELECT * FROM game_devices WHERE tenant_id=?",
                    "game_sessions": "SELECT * FROM game_sessions WHERE tenant_id=?",
                    "game_session_segments": "SELECT * FROM game_session_segments WHERE tenant_id=?",
                    "gamenet_reservations": "SELECT * FROM gamenet_reservations WHERE tenant_id=?",
                    "licenses": "SELECT * FROM licenses WHERE tenant_id=?",
                    "tenant_modules": "SELECT * FROM tenant_modules WHERE tenant_id=?",
                    "audit_log": "SELECT * FROM audit_log WHERE tenant_id=?"
                }
                total_rows = sum(db.execute(
                    "SELECT COUNT(*) FROM " + name + " WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0] for name in tables)
                if total_rows > BACKUP_MAX_ROWS:
                    db.rollback()
                    return self.send_json(413, {"error":"حجم اطلاعات فضای کاری از سقف خروجی پشتیبان بیشتر است؛ از پشتیبان‌گیری محلی پایگاه داده استفاده کنید."})
                snapshot = {"schema":"mg-business-server-backup","version":1,
                            "exportedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                            "tenant":dict(db.execute("SELECT id,name,created_at FROM tenants WHERE id=?",(tenant_id,)).fetchone()),
                            "tables":{}}
                for name, query in tables.items():
                    snapshot["tables"][name] = [dict(row) for row in db.execute(query,(tenant_id,)).fetchall()]
                db.commit()
                return self.send_json(200, snapshot)
            if path == "/api/audit-log":
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403,{"error":"دسترسی مشاهده گزارش ممیزی ندارید."})
                rows=db.execute("""SELECT a.id,u.username AS actorUsername,a.action,
                    a.entity_type AS entityType,a.entity_id AS entityId,a.details_json AS detailsJson,
                    a.created_at AS createdAt FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id
                    WHERE a.tenant_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 500""",
                    (user["tenant_id"],)).fetchall()
                items=[]
                for row in rows:
                    item=dict(row)
                    try: item["details"]=json.loads(item.pop("detailsJson") or "{}")
                    except (TypeError,ValueError): item["details"]={}
                    items.append(item)
                return self.send_json(200,{"items":items})
            if path == "/api/users":
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error": "دسترسی مشاهده کاربران ندارید."})
                rows = db.execute("SELECT id,username,role,active,created_at AS createdAt FROM users WHERE tenant_id=? ORDER BY username",
                                  (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/transactions":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("""SELECT id,title,amount,kind,category,occurred_at AS createdAt
                    FROM transactions WHERE tenant_id=? AND status='posted' ORDER BY created_at DESC LIMIT 5000""",
                    (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/products":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                rows = db.execute("""SELECT id,name,sku,category,sell_price AS sellPrice,cost_price AS costPrice,active
                    FROM products WHERE tenant_id=? ORDER BY name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/product-addons":
                if not require_any_module(db,user["tenant_id"],"cafe","inventory"):
                    return self.send_json(403,{"error":"ماژول کافه یا انبار فعال نیست."})
                rows=db.execute("""SELECT a.id,a.product_id AS productId,p.name AS productName,
                    a.name,a.price,a.active,a.created_at AS createdAt
                    FROM product_addons a JOIN products p ON p.id=a.product_id
                    WHERE a.tenant_id=? AND a.active=1 AND p.active=1 ORDER BY p.name,a.name LIMIT 5000""",
                    (user["tenant_id"],)).fetchall()
                return self.send_json(200,{"items":[dict(row) for row in rows]})
            if path == "/api/inventory":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                rows = db.execute("""SELECT p.id AS productId,p.name,p.sku,p.category,
                    COALESCE(SUM(m.quantity_delta),0) AS quantity,p.cost_price AS costPrice
                    FROM products p LEFT JOIN inventory_movements m
                    ON m.product_id=p.id AND m.tenant_id=p.tenant_id
                    WHERE p.tenant_id=? AND p.active=1
                    GROUP BY p.id ORDER BY p.name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/inventory/movements":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                rows = db.execute("""SELECT m.id,m.product_id AS productId,p.name AS productName,
                    m.movement_type AS movementType,m.quantity_delta AS quantityDelta,
                    m.unit_cost AS unitCost,m.note,m.created_at AS createdAt
                    FROM inventory_movements m JOIN products p ON p.id=m.product_id
                    WHERE m.tenant_id=? ORDER BY m.created_at DESC LIMIT 1000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/suppliers":
                if not module_enabled(db,user["tenant_id"],"inventory"):
                    return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                rows=db.execute("SELECT id,name,phone,email,notes,active,created_at AS createdAt FROM suppliers WHERE tenant_id=? ORDER BY name LIMIT 5000",(user["tenant_id"],)).fetchall()
                return self.send_json(200,{"items":[dict(r) for r in rows]})
            if path.startswith("/api/suppliers/") and path.endswith("/ledger"):
                if not module_enabled(db,user["tenant_id"],"inventory"):
                    return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                supplier_id=path.split("/")[-2]
                supplier=db.execute("SELECT id,name,phone,email FROM suppliers WHERE id=? AND tenant_id=?",
                                    (supplier_id,user["tenant_id"])).fetchone()
                if not supplier:
                    return self.send_json(404,{"error":"تأمین‌کننده پیدا نشد."})
                rows=db.execute("""SELECT 'invoice' AS sourceType,i.id AS sourceId,i.invoice_number AS reference,
                    i.invoice_date AS entryDate,i.created_at AS createdAt,i.rowid AS sequence,i.total AS credit,0 AS debit
                    FROM purchase_invoices i WHERE i.tenant_id=? AND i.supplier_id=?
                    UNION ALL
                    SELECT 'payment' AS sourceType,p.id AS sourceId,i.invoice_number AS reference,
                    NULL AS entryDate,p.created_at AS createdAt,p.rowid AS sequence,0 AS credit,p.amount AS debit
                    FROM supplier_payments p JOIN purchase_invoices i ON i.id=p.invoice_id AND i.tenant_id=p.tenant_id
                    WHERE p.tenant_id=? AND p.supplier_id=?
                    ORDER BY createdAt,sequence,sourceType""",
                    (user["tenant_id"],supplier_id,user["tenant_id"],supplier_id)).fetchall()
                items=[]; balance=total_credit=total_debit=0
                for row in rows:
                    item=dict(row); total_credit+=item["credit"]; total_debit+=item["debit"]
                    balance+=item["credit"]-item["debit"]; item["balanceAfter"]=balance
                    items.append(item)
                return self.send_json(200,{"supplier":dict(supplier),"items":list(reversed(items)),
                    "totalPurchases":total_credit,"totalPayments":total_debit,"balance":balance})
            if path == "/api/purchase-invoices":
                if not module_enabled(db,user["tenant_id"],"inventory"):
                    return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                rows=db.execute("""SELECT i.id,i.invoice_number AS invoiceNumber,i.invoice_date AS invoiceDate,i.subtotal,i.discount,i.total,
                    i.paid_amount AS paidAmount,i.status,s.id AS supplierId,s.name AS supplierName
                    FROM purchase_invoices i JOIN suppliers s ON s.id=i.supplier_id WHERE i.tenant_id=?
                    ORDER BY i.invoice_date DESC,i.created_at DESC LIMIT 2000""",(user["tenant_id"],)).fetchall()
                return self.send_json(200,{"items":[dict(r) for r in rows]})
            if path == "/api/loyalty/program":
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error":"ماژول مشتریان در لایسنس فعال نیست."})
                row=db.execute("SELECT enabled,spend_amount AS spendAmount,points_awarded AS pointsAwarded,updated_at AS updatedAt FROM loyalty_programs WHERE tenant_id=?",
                    (user["tenant_id"],)).fetchone()
                return self.send_json(200, {"program":dict(row) if row else {
                    "enabled":False,"spendAmount":100000,"pointsAwarded":1,"updatedAt":None}})
            if path.startswith("/api/customers/") and path.endswith("/loyalty"):
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error":"ماژول مشتریان در لایسنس فعال نیست."})
                customer_id=path.split("/")[-2]
                customer=db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?",
                    (customer_id,user["tenant_id"])).fetchone()
                if not customer:
                    return self.send_json(404, {"error":"مشتری پیدا نشد."})
                rows=db.execute("""SELECT id,source_type AS sourceType,source_id AS sourceId,
                    points_delta AS pointsDelta,note,created_at AS createdAt
                    FROM customer_loyalty_ledger WHERE tenant_id=? AND customer_id=?
                    ORDER BY created_at DESC,id DESC LIMIT 1000""",
                    (user["tenant_id"],customer_id)).fetchall()
                balance=db.execute("SELECT COALESCE(SUM(points_delta),0) FROM customer_loyalty_ledger WHERE tenant_id=? AND customer_id=?",
                    (user["tenant_id"],customer_id)).fetchone()[0]
                return self.send_json(200, {"customerId":customer_id,"balance":balance,"items":[dict(row) for row in rows]})
            if path == "/api/customers":
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error": "ماژول مشتریان در لایسنس فعال نیست."})
                rows = db.execute("""SELECT c.id,c.full_name AS fullName,c.phone,c.email,c.notes,
                    c.created_at AS createdAt,COALESCE(SUM(l.debit-l.credit),0) AS balance
                    FROM customers c LEFT JOIN customer_ledger l ON l.customer_id=c.id AND l.tenant_id=c.tenant_id
                    WHERE c.tenant_id=? GROUP BY c.id ORDER BY c.full_name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path.startswith("/api/customers/") and path.endswith("/ledger"):
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error": "ماژول مشتریان در لایسنس فعال نیست."})
                customer_id = path.split("/")[-2]
                rows = db.execute("""SELECT id,source_type AS sourceType,source_id AS sourceId,
                    debit,credit,note,created_at AS createdAt FROM customer_ledger
                    WHERE tenant_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 1000""",
                    (user["tenant_id"],customer_id)).fetchall()
                return self.send_json(200, {"items":[dict(row) for row in rows]})
            if path.startswith("/api/orders/"):
                if not module_enabled(db,user["tenant_id"],"cafe"):
                    return self.send_json(403,{"error":"ماژول کافه در لایسنس فعال نیست."})
                order_id=path.rsplit("/",1)[-1]
                order=db.execute("""SELECT o.id,o.order_number AS orderNumber,o.status,o.subtotal,o.discount,o.total,
                    o.payment_method AS paymentMethod,o.created_at AS createdAt,c.full_name AS customerName
                    FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
                    WHERE o.id=? AND o.tenant_id=?""",(order_id,user["tenant_id"])).fetchone()
                if not order:
                    return self.send_json(404,{"error":"سفارش پیدا نشد."})
                items=[]
                for row in db.execute("""SELECT id,product_id AS productId,product_name AS productName,
                    quantity,unit_price AS unitPrice,line_total AS lineTotal,
                    discount_amount AS discountAmount,addons_json AS addonsJson
                    FROM order_items WHERE tenant_id=? AND order_id=? ORDER BY rowid""",
                    (user["tenant_id"],order_id)).fetchall():
                    item=dict(row)
                    try:
                        addons=json.loads(item.pop("addonsJson") or "[]")
                        item["addons"]=addons if isinstance(addons,list) else []
                    except (TypeError,ValueError):
                        item.pop("addonsJson",None)
                        item["addons"]=[]
                    items.append(item)
                return self.send_json(200,{"order":dict(order),"items":items})
            if path == "/api/orders":
                if not module_enabled(db,user["tenant_id"],"cafe"):
                    return self.send_json(403, {"error": "ماژول کافه در لایسنس فعال نیست."})
                rows = db.execute("""SELECT o.id,o.order_number AS orderNumber,o.status,o.subtotal,o.discount,o.total,
                    o.payment_method AS paymentMethod,o.created_at AS createdAt,c.full_name AS customerName
                    FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
                    WHERE o.tenant_id=? ORDER BY o.created_at DESC LIMIT 1000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/gamenet/devices":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                rows = db.execute("""SELECT d.id,d.name,d.device_type AS deviceType,d.hourly_rate AS hourlyRate,d.is_free AS isFree,
                    d.active,gs.id AS sessionId,gs.customer_id AS customerId,c.full_name AS customerName,
                    gs.started_at AS startedAt,gs.players_count AS playersCount,
                    gs.is_free AS sessionIsFree,gs.rate_per_hour AS sessionRatePerHour
                    FROM game_devices d LEFT JOIN game_sessions gs
                    ON gs.device_id=d.id AND gs.tenant_id=d.tenant_id AND gs.status='running'
                    LEFT JOIN customers c ON c.id=gs.customer_id AND c.tenant_id=gs.tenant_id
                    WHERE d.tenant_id=? ORDER BY d.name""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path.startswith("/api/gamenet/sessions/") and path.endswith("/segments"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id=path.split("/")[-2]
                session=db.execute("SELECT id FROM game_sessions WHERE id=? AND tenant_id=?",
                    (session_id,user["tenant_id"])).fetchone()
                if not session:
                    return self.send_json(404, {"error":"نشست پیدا نشد."})
                rows=db.execute("""SELECT g.id,g.device_id AS deviceId,d.name AS deviceName,
                    g.started_at AS startedAt,g.ended_at AS endedAt,g.players_count AS playersCount,
                    g.rate_per_hour AS ratePerHour,g.is_free AS isFree
                    FROM game_session_segments g JOIN game_devices d ON d.id=g.device_id
                    WHERE g.tenant_id=? AND g.session_id=? ORDER BY g.started_at,g.id""",
                    (user["tenant_id"],session_id)).fetchall()
                return self.send_json(200, {"items":[dict(row) for row in rows]})
            if path == "/api/gamenet/sessions":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                rows = db.execute("""SELECT s.id,s.device_id AS deviceId,d.name AS deviceName,
                    s.customer_id AS customerId,c.full_name AS customerName,s.started_at AS startedAt,
                    s.ended_at AS endedAt,s.status,s.players_count AS playersCount,
                    s.rate_per_hour AS ratePerHour,s.is_free AS isFree,s.amount_due AS amountDue,
                    s.payment_method AS paymentMethod
                    FROM game_sessions s JOIN game_devices d ON d.id=s.device_id
                    LEFT JOIN customers c ON c.id=s.customer_id
                    WHERE s.tenant_id=? ORDER BY s.started_at DESC LIMIT 500""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/gamenet/reservations":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403,{"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                rows=db.execute("""SELECT r.id,r.device_id AS deviceId,d.name AS deviceName,
                    r.customer_id AS customerId,c.full_name AS customerName,r.starts_at AS startsAt,
                    r.ends_at AS endsAt,r.status,r.players_count AS playersCount,r.notes,
                    r.created_at AS createdAt FROM gamenet_reservations r
                    JOIN game_devices d ON d.id=r.device_id
                    LEFT JOIN customers c ON c.id=r.customer_id
                    WHERE r.tenant_id=? ORDER BY r.starts_at DESC LIMIT 1000""",(user["tenant_id"],)).fetchall()
                return self.send_json(200,{"items":[dict(row) for row in rows]})
            if path == "/api/accounting/accounts":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("SELECT code,name,account_type AS accountType,normal_balance AS normalBalance FROM accounts WHERE tenant_id=? AND active=1 ORDER BY code",
                                  (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/reports/best-selling-today":
                if not module_enabled(db,user["tenant_id"],"reports"):
                    return self.send_json(403, {"error":"ماژول گزارش‌ها در لایسنس فعال نیست."})
                now=int(time.time())
                local=time.localtime(now)
                day_start=int(time.mktime((local.tm_year,local.tm_mon,local.tm_mday,0,0,0,0,0,-1)))
                day_end=int(time.mktime((local.tm_year,local.tm_mon,local.tm_mday+1,0,0,0,0,0,-1)))
                rows=db.execute("""SELECT i.product_id AS productId,i.product_name AS productName,
                    SUM(i.quantity) AS quantity,SUM(i.line_total-i.discount_amount) AS revenue,
                    COUNT(DISTINCT o.id) AS orderCount
                    FROM order_items i JOIN orders o ON o.id=i.order_id AND o.tenant_id=i.tenant_id
                    WHERE i.tenant_id=? AND o.status='paid' AND o.created_at>=? AND o.created_at<?
                    GROUP BY i.product_id,i.product_name
                    ORDER BY quantity DESC,revenue DESC,i.product_name COLLATE NOCASE LIMIT 10""",
                    (user["tenant_id"],day_start,day_end)).fetchall()
                return self.send_json(200, {"date":time.strftime("%Y-%m-%d",local),
                    "timezone":time.strftime("%Z",local),"items":[dict(row) for row in rows]})
            if path == "/api/reports/sales-by-product":
                if not module_enabled(db,user["tenant_id"],"reports"):
                    return self.send_json(403, {"error": "ماژول گزارش‌ها در لایسنس فعال نیست."})
                rows = db.execute("""SELECT i.product_id AS productId,i.product_name AS productName,
                    SUM(i.quantity) AS quantity,SUM(i.line_total-i.discount_amount) AS revenue
                    FROM order_items i JOIN orders o ON o.id=i.order_id AND o.tenant_id=i.tenant_id
                    WHERE i.tenant_id=? AND o.status='paid'
                    GROUP BY i.product_id,i.product_name ORDER BY revenue DESC LIMIT 100""",
                    (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/accounting/journal":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("""SELECT e.id,e.entry_date AS entryDate,e.memo,e.source_type AS sourceType,
                    e.source_id AS sourceId,COALESCE(SUM(l.debit),0) AS debit,COALESCE(SUM(l.credit),0) AS credit
                    FROM journal_entries e LEFT JOIN journal_lines l ON l.entry_id=e.id AND l.tenant_id=e.tenant_id
                    WHERE e.tenant_id=? GROUP BY e.id ORDER BY e.created_at DESC LIMIT 1000""",
                    (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/accounting/profit-loss":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("""SELECT a.account_type AS accountType,
                    COALESCE(SUM(l.debit),0) AS debit,COALESCE(SUM(l.credit),0) AS credit
                    FROM accounts a LEFT JOIN journal_lines l ON l.account_id=a.id AND l.tenant_id=a.tenant_id
                    WHERE a.tenant_id=? AND a.account_type IN ('revenue','expense')
                    GROUP BY a.account_type""", (user["tenant_id"],)).fetchall()
                totals = {row["accountType"]: row for row in rows}
                revenue = totals.get("revenue")["credit"] - totals.get("revenue")["debit"] if totals.get("revenue") else 0
                expenses = totals.get("expense")["debit"] - totals.get("expense")["credit"] if totals.get("expense") else 0
                return self.send_json(200, {"revenue": revenue, "expenses": expenses, "netProfit": revenue-expenses})
            if path == "/api/accounting/trial-balance":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("""SELECT a.code,a.name,a.account_type AS accountType,
                    a.normal_balance AS normalBalance,COALESCE(SUM(l.debit),0) AS debit,
                    COALESCE(SUM(l.credit),0) AS credit
                    FROM accounts a LEFT JOIN journal_lines l ON l.account_id=a.id AND l.tenant_id=a.tenant_id
                    WHERE a.tenant_id=? AND a.active=1 GROUP BY a.id ORDER BY a.code""",
                    (user["tenant_id"],)).fetchall()
                items = [dict(row) for row in rows]
                return self.send_json(200, {"items":items,
                    "totalDebit":sum(x["debit"] for x in items),
                    "totalCredit":sum(x["credit"] for x in items)})
            if path == "/api/accounting/balance-sheet":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                rows = db.execute("""SELECT a.code,a.name,a.account_type AS accountType,
                    COALESCE(SUM(l.debit),0) AS debit,COALESCE(SUM(l.credit),0) AS credit
                    FROM accounts a LEFT JOIN journal_lines l ON l.account_id=a.id AND l.tenant_id=a.tenant_id
                    WHERE a.tenant_id=? AND a.active=1 AND a.account_type IN ('asset','liability','equity')
                    GROUP BY a.id ORDER BY a.code""", (user["tenant_id"],)).fetchall()
                items = [dict(row) for row in rows]
                assets = sum(x["debit"]-x["credit"] for x in items if x["accountType"]=="asset")
                liabilities = sum(x["credit"]-x["debit"] for x in items if x["accountType"]=="liability")
                equity_accounts = sum(x["credit"]-x["debit"] for x in items if x["accountType"]=="equity")
                profit_rows = db.execute("""SELECT a.account_type AS accountType,
                    COALESCE(SUM(l.debit),0) AS debit,COALESCE(SUM(l.credit),0) AS credit
                    FROM accounts a LEFT JOIN journal_lines l ON l.account_id=a.id AND l.tenant_id=a.tenant_id
                    WHERE a.tenant_id=? AND a.account_type IN ('revenue','expense') GROUP BY a.account_type""",
                    (user["tenant_id"],)).fetchall()
                totals = {x["accountType"]:x for x in profit_rows}
                profit = (totals.get("revenue")["credit"]-totals.get("revenue")["debit"] if totals.get("revenue") else 0) - (totals.get("expense")["debit"]-totals.get("expense")["credit"] if totals.get("expense") else 0)
                equity = equity_accounts + profit
                return self.send_json(200, {"assets":assets,"liabilities":liabilities,"equity":equity,
                    "currentProfit":profit,"liabilitiesAndEquity":liabilities+equity,
                    "balanced":assets == liabilities+equity,"items":items})
            return self.send_json(404, {"error": "مسیر API پیدا نشد."})
        except sqlite3.IntegrityError:
            db.rollback()
            return self.send_json(409, {"error":"رکورد تکراری یا ناسازگار با اطلاعات موجود است."})
        except sqlite3.OperationalError as exc:
            db.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return self.send_json(503, {"error":"پایگاه داده مشغول است؛ چند لحظه بعد دوباره تلاش کنید."})
            return self.send_json(500, {"error":"خطای عملیاتی پایگاه داده رخ داد."})
        except sqlite3.Error:
            db.rollback()
            return self.send_json(500, {"error":"خطای پایگاه داده رخ داد."})
        finally: db.close()

    def do_DELETE(self):
        path = self.route()
        db = connect()
        try:
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path.startswith("/api/gamenet/devices/"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                device_id = path.rsplit("/", 1)[-1]
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error": "فقط مدیر می‌تواند دستگاه را غیرفعال کند."})
                db.execute("BEGIN IMMEDIATE")
                running = db.execute("SELECT 1 FROM game_sessions WHERE tenant_id=? AND device_id=? AND status='running'",
                                     (user["tenant_id"],device_id)).fetchone()
                if running:
                    db.rollback()
                    return self.send_json(409, {"error": "ابتدا نشست فعال دستگاه را پایان دهید."})
                changed = db.execute("UPDATE game_devices SET active=0 WHERE id=? AND tenant_id=? AND active=1",
                                     (device_id,user["tenant_id"])).rowcount
                if not changed:
                    db.rollback()
                    return self.send_json(404, {"error": "دستگاه فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"gamenet.device.deactivated","game_device",device_id)
                db.commit()
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/orders/"):
                if not module_enabled(db,user["tenant_id"],"cafe"):
                    return self.send_json(403, {"error": "ماژول کافه در لایسنس فعال نیست."})
                order_id = path.rsplit("/", 1)[-1]
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error": "فقط مدیر می‌تواند سفارش را برگشت بزند."})
                db.execute("BEGIN IMMEDIATE")
                order = db.execute("SELECT * FROM orders WHERE id=? AND tenant_id=?",
                                   (order_id,user["tenant_id"])).fetchone()
                if not order:
                    db.rollback()
                    return self.send_json(404, {"error": "سفارش پیدا نشد."})
                if order["status"]=="cancelled":
                    db.commit()
                    return self.send_json(200, {"ok":True,"cancelled":True,"duplicate":True})
                if order["status"]!="paid":
                    db.rollback()
                    return self.send_json(409, {"error":"فقط سفارش پرداخت‌شده قابل برگشت است."})
                now = int(time.time())
                lines = db.execute("SELECT product_id,quantity,unit_cost FROM order_items WHERE tenant_id=? AND order_id=?",
                                   (user["tenant_id"],order_id)).fetchall()
                for line in lines:
                    movement_id = secrets.token_urlsafe(16)
                    note = "برگشت سفارش "+order["order_number"]
                    db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (movement_id,user["tenant_id"],line["product_id"],user["id"],"adjustment",line["quantity"],line["unit_cost"],note,now))
                    post_inventory_movement_journal(db,user["tenant_id"],user["id"],movement_id,"adjustment",line["quantity"],line["unit_cost"],note)
                tx_id = "order-"+order_id
                db.execute("UPDATE transactions SET status='voided',voided_at=?,voided_by=? WHERE id=? AND tenant_id=? AND status='posted'",
                           (now,user["id"],tx_id,user["tenant_id"]))
                reverse_transaction_journal(db,user["tenant_id"],user["id"],tx_id)
                if order["payment_method"] == "credit" and order["customer_id"]:
                    db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                        VALUES(?,?,?,?,?,?,0,?,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],order["customer_id"],user["id"],
                        "order_reversal",order_id,order["total"],"برگشت فروش اعتباری "+order["order_number"],now))
                earned=db.execute("""SELECT points_delta FROM customer_loyalty_ledger
                    WHERE tenant_id=? AND source_type='order_earn' AND source_id=?""",
                    (user["tenant_id"],order_id)).fetchone()
                if earned and order["customer_id"]:
                    db.execute("""INSERT OR IGNORE INTO customer_loyalty_ledger
                        (id,tenant_id,customer_id,created_by,source_type,source_id,points_delta,note,created_at)
                        VALUES(?,?,?,?, 'order_reversal',?,?,?,?)""",
                        (secrets.token_urlsafe(18),user["tenant_id"],order["customer_id"],user["id"],
                         order_id,-int(earned["points_delta"]),"برگشت امتیاز سفارش "+order["order_number"],now))
                    audit(db,user["tenant_id"],user["id"],"loyalty.points.reversed","customer",order["customer_id"],
                        {"orderId":order_id,"points":int(earned["points_delta"])})
                db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND tenant_id=?", (order_id,user["tenant_id"]))
                audit(db,user["tenant_id"],user["id"],"order.cancelled","order",order_id)
                db.commit()
                return self.send_json(200, {"ok": True, "cancelled": True, "duplicate": False})
            if path.startswith("/api/users/"):
                target_id = path.rsplit("/", 1)[-1]
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند کاربران را غیرفعال کند."})
                if target_id == user["id"]:
                    return self.send_json(400, {"error": "امکان غیرفعال‌کردن حساب خودتان وجود ندارد."})
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE users SET active=0 WHERE id=? AND tenant_id=? AND active=1",
                                     (target_id,user["tenant_id"])).rowcount
                if not changed:
                    db.rollback()
                    return self.send_json(404, {"error": "کاربر فعال پیدا نشد."})
                db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
                audit(db,user["tenant_id"],user["id"],"user.deactivated","user",target_id)
                db.commit()
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/products/"):
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                product_id = path.rsplit("/", 1)[-1]
                if user["role"] not in ("owner", "manager"):
                    return self.send_json(403, {"error": "فقط مدیر می‌تواند کالا را غیرفعال کند."})
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE products SET active=0,updated_at=? WHERE id=? AND tenant_id=? AND active=1",
                                     (int(time.time()),product_id,user["tenant_id"])).rowcount
                if not changed:
                    db.rollback()
                    return self.send_json(404, {"error": "کالای فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"product.deactivated","product",product_id)
                db.commit()
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/product-addons/"):
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403,{"error":"ماژول کافه یا انبار فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403,{"error":"فقط مدیر می‌تواند افزودنی کالا را غیرفعال کند."})
                addon_id=path.rsplit("/",1)[-1]
                changed=db.execute("UPDATE product_addons SET active=0,updated_at=? WHERE id=? AND tenant_id=? AND active=1",
                                   (int(time.time()),addon_id,user["tenant_id"])).rowcount
                if not changed:
                    return self.send_json(404,{"error":"افزودنی فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"product.addon.deactivated","product_addon",addon_id)
                db.commit()
                return self.send_json(200,{"ok":True})
            if not path.startswith("/api/transactions/"):
                return self.send_json(404, {"error": "مسیر API پیدا نشد."})
            if not module_enabled(db,user["tenant_id"],"accounting"):
                return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
            tx_id = path.rsplit("/", 1)[-1]
            if user["role"] not in ("owner", "manager"):
                return self.send_json(403, {"error": "فقط مدیر می‌تواند تراکنش را حذف کند."})
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,status FROM transactions WHERE id=? AND tenant_id=?", (tx_id,user["tenant_id"])).fetchone()
            if not row or row["status"] != "posted":
                db.rollback()
                return self.send_json(404, {"error": "تراکنش ثبت‌شده پیدا نشد."})
            now = int(time.time())
            db.execute("UPDATE transactions SET status='voided',voided_at=?,voided_by=? WHERE id=? AND tenant_id=?",
                       (now,user["id"],tx_id,user["tenant_id"]))
            reverse_transaction_journal(db,user["tenant_id"],user["id"],tx_id)
            audit(db,user["tenant_id"],user["id"],"transaction.voided","transaction",tx_id)
            db.commit()
            return self.send_json(200, {"ok": True, "voided": True})
        except sqlite3.IntegrityError:
            db.rollback()
            return self.send_json(409, {"error":"عملیات با محدودیت‌های داده سازگار نیست."})
        except sqlite3.OperationalError as exc:
            db.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return self.send_json(503, {"error":"پایگاه داده مشغول است؛ چند لحظه بعد دوباره تلاش کنید."})
            return self.send_json(500, {"error":"خطای عملیاتی پایگاه داده رخ داد."})
        except sqlite3.Error:
            db.rollback()
            return self.send_json(500, {"error":"خطای پایگاه داده رخ داد."})
        finally: db.close()

    def do_POST(self):
        path = self.route()
        try: data = self.read_json()
        except ValueError as exc: return self.send_json(400, {"error": str(exc)})
        db = connect()
        try:
            if path == "/api/logout":
                user = self.authenticate(db)
                if not user:
                    return self.send_json(401, {"error": "نشست معتبر نیست یا منقضی شده است."})
                token = self.headers.get("Authorization", "")[7:].strip()
                db.execute("DELETE FROM sessions WHERE token_hash=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))
                db.commit()
                return self.send_json(200, {"ok": True})
            if path == "/api/setup":
                tenant_name = str(data.get("businessName", "")).strip()
                username = str(data.get("username", "")).strip()
                password = data.get("password", "")
                if not 1 <= len(tenant_name) <= 120: return self.send_json(400, {"error": "نام کسب‌وکار معتبر نیست."})
                if not 3 <= len(username) <= 64: return self.send_json(400, {"error": "نام کاربری باید ۳ تا ۶۴ نویسه باشد."})
                if not isinstance(password, str) or not 12 <= len(password) <= 256: return self.send_json(400, {"error": "رمز عبور باید بین ۱۲ تا ۲۵۶ نویسه باشد."})
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    db.rollback(); return self.send_json(409, {"error": "راه‌اندازی اولیه قبلاً انجام شده است."})
                tenant_id, user_id = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
                now = int(time.time())
                db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_id, tenant_name, now))
                db.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                           (user_id, tenant_id, username, password_hash(password), "owner", now))
                seed_accounts(db, tenant_id)
                seed_trial_modules(db, tenant_id)
                audit(db, tenant_id, user_id, "tenant.created", "tenant", tenant_id)
                db.commit()
                return self.create_session(db, user_id)
            if path == "/api/login":
                raw_username = data.get("username", "")
                if not isinstance(raw_username, str) or not 3 <= len(raw_username.strip()) <= 64:
                    return self.send_json(400, {"error": "نام کاربری معتبر نیست."})
                username, password = raw_username.strip(), data.get("password", "")
                if not isinstance(password, str) or len(password) > 256:
                    return self.send_json(400, {"error": "رمز عبور معتبر نیست؛ حداکثر طول مجاز ۲۵۶ نویسه است."})
                ip = self.client_ip()
                key = (ip, username.casefold())
                now = time.time()
                with LOGIN_LOCK:
                    attempts = [stamp for stamp in LOGIN_FAILURES.get(key, []) if now - stamp < 900]
                    ip_attempts = [stamp for stamp in LOGIN_IP_FAILURES.get(ip, []) if now - stamp < 900]
                    LOGIN_FAILURES[key] = attempts
                    LOGIN_IP_FAILURES[ip] = ip_attempts
                    if len(attempts) >= 8 or len(ip_attempts) >= 30:
                        return self.send_json(429, {"error": "تلاش‌های ورود بیش از حد است؛ ۱۵ دقیقه بعد دوباره امتحان کنید."})
                user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
                if not user or not isinstance(password, str) or not verify_password(password, user["password_hash"]):
                    with LOGIN_LOCK:
                        remember_login_failure(key, ip, now)
                    return self.send_json(401, {"error": "نام کاربری یا رمز عبور اشتباه است."})
                with LOGIN_LOCK:
                    # A successful login for one account must not erase failures
                    # accumulated against other accounts from the same source IP.
                    LOGIN_FAILURES.pop(key, None)
                return self.create_session(db, user["id"])
            if path == "/api/public/orders":
                ip = self.client_ip()
                now = time.time()
                with LOGIN_LOCK:
                    if not remember_public_order_attempt(ip, now):
                        return self.send_json(429, {"error": "تعداد سفارش‌های آنلاین بیش از حد است؛ بعداً دوباره تلاش کنید."})
                name, phone = data.get("customerName"), data.get("phone")
                items = data.get("items")
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 160:
                    return self.send_json(400, {"error": "نام مشتری معتبر نیست."})
                if not isinstance(phone,str) or not 5 <= len(phone.strip()) <= 32:
                    return self.send_json(400, {"error": "شماره تماس معتبر نیست."})
                if not isinstance(items,list) or not 1 <= len(items) <= 30:
                    return self.send_json(400, {"error": "سفارش باید حداقل یک قلم و حداکثر ۳۰ قلم داشته باشد."})
                normalized, normalized_lines = {}, {}
                for line in items:
                    if not isinstance(line,dict) or not isinstance(line.get("productId"),str) or type(line.get("quantity")) is not int or not 1 <= line["quantity"] <= 30:
                        return self.send_json(400, {"error": "قلم سفارش معتبر نیست."})
                    addon_ids=line.get("addons",[])
                    if (not isinstance(addon_ids,list) or len(addon_ids)>20
                            or any(not isinstance(addon_id,str) or not 1<=len(addon_id)<=100 for addon_id in addon_ids)
                            or len(set(addon_ids))!=len(addon_ids)):
                        return self.send_json(400,{"error":"فهرست افزودنی‌های سفارش آنلاین معتبر نیست."})
                    key=(line["productId"],tuple(sorted(addon_ids)))
                    normalized_lines[key]=normalized_lines.get(key,0)+line["quantity"]
                    normalized[line["productId"]]=normalized.get(line["productId"],0)+line["quantity"]
                tenants = db.execute("SELECT id FROM tenants ORDER BY created_at LIMIT 2").fetchall()
                if len(tenants) > 1:
                    return self.send_json(409, {"error": "برای پذیرش سفارش چند کسب‌وکار، ابتدا دامنه اختصاصی هر کسب‌وکار را پیکربندی کنید."})
                tenant = tenants[0] if tenants else None
                if not tenant or not module_enabled(db,tenant["id"],"cafe"):
                    return self.send_json(404, {"error": "ثبت سفارش آنلاین فعال نیست."})
                tenant_id = tenant["id"]
                client_order_id = data.get("clientOrderId")
                if client_order_id is not None and (not isinstance(client_order_id,str) or not 8 <= len(client_order_id) <= 100):
                    return self.send_json(400, {"error": "شناسه سفارش آنلاین معتبر نیست."})
                request_fingerprint = public_order_fingerprint(name.strip(), phone.strip(), normalized_lines)
                if client_order_id:
                    duplicate=public_order_duplicate(db,"public-"+client_order_id,tenant_id,name.strip(),phone.strip(),normalized_lines,request_fingerprint)
                    if duplicate is False:
                        return self.send_json(409,{"error":"شناسه سفارش آنلاین قبلاً با اطلاعات متفاوتی استفاده شده است."})
                    if duplicate:
                        return self.send_json(200,{"ok":True,"orderNumber":duplicate["orderNumber"],"total":duplicate["total"],"duplicate":True})
                actor = db.execute("SELECT id FROM users WHERE tenant_id=? AND active=1 ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END LIMIT 1",
                                   (tenant_id,)).fetchone()
                if not actor:
                    return self.send_json(503, {"error": "پذیرش سفارش در حال حاضر ممکن نیست."})
                db.execute("BEGIN IMMEDIATE")
                # Recheck idempotency under the write lock to serialize concurrent retries.
                if client_order_id:
                    duplicate=public_order_duplicate(db,"public-"+client_order_id,tenant_id,name.strip(),phone.strip(),normalized_lines,request_fingerprint)
                    if duplicate is False:
                        db.rollback()
                        return self.send_json(409,{"error":"شناسه سفارش آنلاین قبلاً با اطلاعات متفاوتی استفاده شده است."})
                    if duplicate:
                        db.rollback()
                        return self.send_json(200,{"ok":True,"orderNumber":duplicate["orderNumber"],"total":duplicate["total"],"duplicate":True})
                customer = db.execute("SELECT id FROM customers WHERE tenant_id=? AND phone=?",
                                      (tenant_id,phone.strip())).fetchone()
                customer_id = customer["id"] if customer else secrets.token_urlsafe(18)
                if not customer:
                    db.execute("""INSERT INTO customers(id,tenant_id,full_name,phone,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)""", (customer_id,tenant_id,name.strip(),phone.strip(),int(now),int(now)))
                products, subtotal, cogs_total, order_lines = {}, 0, 0, []
                for product_id,quantity in normalized.items():
                    product = db.execute("SELECT id,name,sell_price,cost_price FROM products WHERE id=? AND tenant_id=? AND active=1",
                                         (product_id,tenant_id)).fetchone()
                    if not product:
                        db.rollback()
                        return self.send_json(404, {"error": "یکی از کالاهای سفارش موجود نیست."})
                    stock = db.execute("""SELECT COALESCE(SUM(quantity_delta),0) AS quantity,
                        COALESCE(SUM(quantity_delta*unit_cost),0) AS stock_value
                        FROM inventory_movements WHERE tenant_id=? AND product_id=?""",
                        (tenant_id,product_id)).fetchone()
                    if stock["quantity"] < quantity:
                        db.rollback()
                        return self.send_json(409, {"error": "موجودی کالای %s کافی نیست." % product["name"]})
                    average_cost = max(0,stock["stock_value"]//stock["quantity"]) if stock["quantity"] else product["cost_price"]
                    line_cost = average_cost*quantity
                    if cogs_total+line_cost > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "بهای تمام‌شده سفارش بیش از حد مجاز است."})
                    cogs_total += line_cost
                    products[product_id]=(product,quantity,average_cost)
                for (product_id,addon_ids),quantity in normalized_lines.items():
                    product=products[product_id][0]
                    addon_rows=[]
                    if addon_ids:
                        placeholders=",".join("?" for _ in addon_ids)
                        addon_rows=db.execute("SELECT id,name,price FROM product_addons WHERE tenant_id=? AND product_id=? AND active=1 AND id IN ("+placeholders+")",
                                              (tenant_id,product_id,*addon_ids)).fetchall()
                        if len(addon_rows)!=len(addon_ids):
                            db.rollback()
                            return self.send_json(409,{"error":"یکی از افزودنی‌های انتخاب‌شده دیگر فعال یا معتبر نیست."})
                    addon_snapshot=[{"id":row["id"],"name":row["name"],"price":row["price"]} for row in addon_rows]
                    unit_price=product["sell_price"]+sum(row["price"] for row in addon_rows)
                    line_total=unit_price*quantity
                    if subtotal+line_total>9007199254740991:
                        db.rollback()
                        return self.send_json(400,{"error":"مبلغ سفارش بیش از حد مجاز است."})
                    subtotal+=line_total
                    order_lines.append((product_id,product,quantity,unit_price,products[product_id][2],line_total,addon_snapshot))
                if subtotal <= 0:
                    db.rollback()
                    return self.send_json(400, {"error": "مبلغ سفارش معتبر نیست."})
                order_id, created = ("public-"+client_order_id) if client_order_id else secrets.token_urlsafe(18), int(now)
                order_number = "MG-ON-%s-%s" % (time.strftime("%Y%m%d"),secrets.token_hex(3).upper())
                db.execute("""INSERT INTO orders(id,tenant_id,order_number,customer_id,created_by,status,subtotal,discount,total,payment_method,created_at,request_fingerprint)
                    VALUES(?,?,?,?,?,'paid',?,0,?,'credit',?,?)""", (order_id,tenant_id,order_number,customer_id,actor["id"],subtotal,subtotal,created,request_fingerprint if client_order_id else None))
                db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                    VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),tenant_id,customer_id,actor["id"],"order",order_id,subtotal,"سفارش آنلاین "+order_number,created))
                for product_id,product,quantity,unit_price,average_cost,line_total,addon_snapshot in order_lines:
                    db.execute("""INSERT INTO order_items(id,tenant_id,order_id,product_id,product_name,quantity,unit_price,unit_cost,line_total,addons_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (secrets.token_urlsafe(16),tenant_id,order_id,product_id,product["name"],quantity,unit_price,average_cost,line_total,json.dumps(addon_snapshot,ensure_ascii=False,separators=(",",":"))))
                for product_id,(product,quantity,average_cost) in products.items():
                    movement_id=secrets.token_urlsafe(16)
                    db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (movement_id,tenant_id,product_id,actor["id"],"sale",-quantity,average_cost,"سفارش آنلاین "+order_number,created))
                post_order_cogs_journal(db,tenant_id,actor["id"],order_id,cogs_total)
                tx_id="order-"+order_id
                title="سفارش آنلاین "+order_number
                occurred_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(created))
                db.execute("""INSERT INTO transactions(id,tenant_id,created_by,title,amount,kind,category,occurred_at,created_at)
                    VALUES(?,?,?,?,?,'income','سفارش آنلاین',?,?)""", (tx_id,tenant_id,actor["id"],title,subtotal,occurred_at,created))
                post_transaction_journal(db,tenant_id,actor["id"],tx_id,title,subtotal,"income",occurred_at,"1100")
                audit(db,tenant_id,actor["id"],"online_order.created","order",order_id,{"total":subtotal,"items":len(order_lines)})
                db.commit()
                return self.send_json(201, {"ok":True,"orderNumber":order_number,"total":subtotal})
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path == "/api/backup/restore":
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند نسخه پشتیبان را بازیابی کند."})
                if data.get("schema") != "mg-business-server-backup" or data.get("version") != 1:
                    return self.send_json(400, {"error": "نسخه یا ساختار فایل پشتیبان معتبر نیست."})
                tenant = data.get("tenant"); tables = data.get("tables")
                if not isinstance(tenant,dict) or tenant.get("id") != user["tenant_id"] or not isinstance(tables,dict):
                    return self.send_json(400, {"error": "این نسخه پشتیبان متعلق به فضای کاری فعلی نیست."})
                allowed = ("users","accounts","products","product_addons","customers","customer_ledger","loyalty_programs","customer_loyalty_ledger","game_devices","transactions","journal_entries",
                           "journal_lines","inventory_movements","suppliers","purchase_invoices","purchase_invoice_items","supplier_payments",
                           "orders","order_items","game_sessions","game_session_segments","gamenet_reservations","licenses","tenant_modules","audit_log")
                # A restore replaces the complete workspace dataset. Missing tables
                # must never be interpreted as empty lists, or a partial/corrupt
                # snapshot could silently erase otherwise valid business data.
                if set(tables) != set(allowed) or any(
                    not isinstance(tables.get(name),list) for name in allowed
                ):
                    return self.send_json(400, {"error": "ساختار جداول نسخه پشتیبان ناقص یا نامعتبر است."})
                total_rows = sum(len(tables.get(name,[])) for name in allowed)
                if total_rows > 500000:
                    return self.send_json(413, {"error": "تعداد رکوردهای نسخه پشتیبان بیش از حد مجاز است."})
                # Reject financially incomplete snapshots before deleting any live rows.
                required_accounts = {"1000","1010","1020","1100","1200","2000","3000",
                                     "4000","4100","5000","5100","5200"}
                account_rows = tables.get("accounts", [])
                available_accounts = {
                    row.get("code") for row in account_rows
                    if isinstance(row,dict) and row.get("active",1) in (1,True)
                }
                if not required_accounts.issubset(available_accounts):
                    return self.send_json(400, {"error": "کدینگ پایه حسابداری در نسخه پشتیبان ناقص است."})
                journal_totals = {}
                journal_counts = {}
                for line in tables.get("journal_lines", []):
                    if not isinstance(line,dict):
                        return self.send_json(400, {"error": "ردیف سند حسابداری در نسخه پشتیبان معتبر نیست."})
                    debit, credit, entry_id = line.get("debit"), line.get("credit"), line.get("entry_id")
                    if not isinstance(entry_id,str) or not 1 <= len(entry_id) <= 100:
                        return self.send_json(400, {"error": "شناسه سند در ردیف حسابداری نسخه پشتیبان معتبر نیست."})
                    if type(debit) is not int or type(credit) is not int or debit < 0 or credit < 0 or debit + credit <= 0:
                        return self.send_json(400, {"error": "مبالغ ردیف سند حسابداری در نسخه پشتیبان معتبر نیست."})
                    totals = journal_totals.setdefault(entry_id,[0,0])
                    totals[0] += debit
                    totals[1] += credit
                    journal_counts[entry_id] = journal_counts.get(entry_id,0) + 1
                for entry in tables.get("journal_entries", []):
                    if not isinstance(entry,dict):
                        return self.send_json(400, {"error": "سند حسابداری در نسخه پشتیبان معتبر نیست."})
                    entry_id = entry.get("id")
                    if not isinstance(entry_id,str) or not 1 <= len(entry_id) <= 100:
                        return self.send_json(400, {"error": "شناسه سند حسابداری در نسخه پشتیبان معتبر نیست."})
                    totals = journal_totals.get(entry_id)
                    if not totals or journal_counts.get(entry_id,0) < 2 or totals[0] != totals[1]:
                        return self.send_json(400, {"error": "تراز یکی از اسناد حسابداری نسخه پشتیبان برقرار نیست."})
                if set(journal_totals) - {row.get("id") for row in tables.get("journal_entries",[]) if isinstance(row,dict)}:
                    return self.send_json(400, {"error": "ردیف سند بدون سند حسابداری در نسخه پشتیبان وجود دارد."})
                tenant_id = user["tenant_id"]
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("DROP TRIGGER IF EXISTS journal_entries_no_delete")
                    db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
                    column_map = {}
                    for name in allowed:
                        column_map[name] = {row["name"] for row in db.execute("PRAGMA table_info("+name+")").fetchall()}
                        for record in tables.get(name,[]):
                            if not isinstance(record,dict) or record.get("tenant_id") != tenant_id:
                                db.rollback()
                                return self.send_json(400, {"error": "رکورد نامعتبر یا متعلق به فضای کاری دیگر در پشتیبان وجود دارد."})
                            if not set(record).issubset(column_map[name]):
                                db.rollback()
                                return self.send_json(400, {"error": "ساختار یکی از رکوردهای پشتیبان با نسخه فعلی سازگار نیست."})
                    delete_order = ("tenant_modules","licenses","gamenet_reservations","game_session_segments","game_sessions","order_items","orders","supplier_payments","purchase_invoice_items","purchase_invoices",
                                    "inventory_movements","journal_lines","journal_entries","transactions","customer_loyalty_ledger","loyalty_programs","customer_ledger",
                                    "product_addons","products","customers","suppliers","game_devices","accounts","audit_log")
                    for name in delete_order:
                        db.execute("DELETE FROM "+name+" WHERE tenant_id=?", (tenant_id,))
                    insert_order = ("accounts","products","product_addons","customers","loyalty_programs","customer_loyalty_ledger","customer_ledger","suppliers","game_devices","transactions",
                                    "journal_entries","journal_lines","inventory_movements","purchase_invoices","purchase_invoice_items",
                                    "supplier_payments","orders","order_items","game_sessions","game_session_segments","gamenet_reservations","licenses","tenant_modules","audit_log")
                    for name in insert_order:
                        records = tables.get(name,[])
                        if name == "journal_entries":
                            # Reversal entries have a self-referencing FK to the original entry.
                            # Restore originals first even when both entries share the same second.
                            records = sorted(records,key=lambda row: (bool(row.get("reversal_of")), row.get("created_at",0)))
                        for record in records:
                            columns = list(record.keys())
                            if not columns:
                                continue
                            placeholders = ",".join("?" for _ in columns)
                            sql = "INSERT INTO "+name+"("+",".join(columns)+") VALUES("+placeholders+")"
                            db.execute(sql,tuple(record[column] for column in columns))
                    # Older JSON backups predate segmented GameNet billing. Reconstruct
                    # one historical segment per session so restored running sessions
                    # remain adjustable and ended sessions retain their original rates.
                    if any("payment_method" not in row for row in tables.get("game_sessions",[])):
                        db.execute("""UPDATE game_sessions SET payment_method='credit'
                            WHERE tenant_id=? AND status='ended' AND customer_id IS NOT NULL AND amount_due>0
                            AND payment_method='cash'""",(tenant_id,))
                    db.execute("""INSERT INTO game_session_segments
                        (id,tenant_id,session_id,device_id,started_at,ended_at,players_count,rate_per_hour,is_free)
                        SELECT 'restore-'||s.id,s.tenant_id,s.id,s.device_id,s.started_at,s.ended_at,
                               s.players_count,s.rate_per_hour,s.is_free
                        FROM game_sessions s
                        WHERE s.tenant_id=? AND NOT EXISTS(
                            SELECT 1 FROM game_session_segments g WHERE g.session_id=s.id
                        )""",(tenant_id,))
                    db.execute("""CREATE TRIGGER journal_entries_no_delete BEFORE DELETE ON journal_entries
                        BEGIN SELECT RAISE(ABORT,'journal entries are immutable'); END""")
                    db.execute("""CREATE TRIGGER journal_lines_no_delete BEFORE DELETE ON journal_lines
                        BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
                    audit(db,tenant_id,user["id"],"backup.restored","tenant",tenant_id,{"rows":total_rows})
                    db.commit()
                    return self.send_json(200, {"ok":True,"restoredRows":total_rows})
                except (sqlite3.Error, TypeError, ValueError, OverflowError):
                    db.rollback()
                    return self.send_json(409, {"error": "بازیابی به‌دلیل ناسازگاری یا نوع نامعتبر داده‌های نسخه پشتیبان انجام نشد؛ اطلاعات قبلی حفظ شد."})
            if path == "/api/licenses/activate":
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند لایسنس را فعال کند."})
                try:
                    license = verify_license_code(data.get("code"))
                except ValueError as exc:
                    return self.send_json(400, {"error": str(exc)})
                code_hash = hashlib.sha256(data["code"].encode()).hexdigest()
                now, tenant_id = int(time.time()), user["tenant_id"]
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT tenant_id FROM licenses WHERE code_hash=?", (code_hash,)).fetchone()
                existing_id = db.execute("SELECT tenant_id FROM licenses WHERE id=?", (license["license_id"],)).fetchone()
                if (existing and existing["tenant_id"] != tenant_id) or (existing_id and existing_id["tenant_id"] != tenant_id):
                    db.rollback()
                    return self.send_json(409, {"error": "این کد یا شناسه لایسنس قبلاً برای فضای کاری دیگری فعال شده است."})
                db.execute("""INSERT OR REPLACE INTO licenses(id,tenant_id,code_hash,plan,expires_at,activated_at,status)
                    VALUES(?,?,?,?,?,?,'active')""", (license["license_id"],tenant_id,code_hash,license["plan"],license["expires_at"],now))
                db.execute("DELETE FROM tenant_modules WHERE tenant_id=?", (tenant_id,))
                entitlements = set(license["modules"]) | {"overview","settings"}
                for module in entitlements:
                    db.execute("""INSERT INTO tenant_modules(tenant_id,module_key,enabled,expires_at,license_id,updated_at)
                        VALUES(?,?,1,?,?,?)""", (tenant_id,module,license["expires_at"],license["license_id"],now))
                audit(db,tenant_id,user["id"],"license.activated","license",license["license_id"],
                      {"plan":license["plan"],"modules":sorted(entitlements)})
                db.commit()
                return self.send_json(200, {"ok":True,"plan":license["plan"],"modules":sorted(entitlements),
                                            "expiresAt":license["expires_at"]})
            if path == "/api/gamenet/devices":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error": "دسترسی مدیریت دستگاه‌ها ندارید."})
                name, device_type = data.get("name"), data.get("deviceType","pc")
                hourly_rate, is_free = data.get("hourlyRate",0), data.get("isFree",False)
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 100:
                    return self.send_json(400, {"error": "نام دستگاه معتبر نیست."})
                if device_type not in ("pc","console","other") or type(hourly_rate) is not int or hourly_rate < 0 or type(is_free) is not bool:
                    return self.send_json(400, {"error": "نوع یا تعرفه دستگاه معتبر نیست."})
                device_id, now = str(data.get("id") or secrets.token_urlsafe(18)), int(time.time())
                db.execute("INSERT INTO game_devices(id,tenant_id,name,device_type,hourly_rate,is_free,created_at) VALUES(?,?,?,?,?,?,?)",
                           (device_id,user["tenant_id"],name.strip(),device_type,hourly_rate,int(is_free),now))
                audit(db,user["tenant_id"],user["id"],"gamenet.device.created","game_device",device_id)
                db.commit()
                return self.send_json(201, {"id":device_id,"ok":True})
            if path.startswith("/api/gamenet/devices/"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error": "دسترسی ویرایش دستگاه‌ها ندارید."})
                device_id = path.rsplit("/",1)[-1]
                name, device_type = data.get("name"), data.get("deviceType")
                hourly_rate, is_free = data.get("hourlyRate"), data.get("isFree")
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 100 or device_type not in ("pc","console","other") or type(hourly_rate) is not int or hourly_rate < 0 or type(is_free) is not bool:
                    return self.send_json(400, {"error": "اطلاعات دستگاه معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("""UPDATE game_devices SET name=?,device_type=?,hourly_rate=?,is_free=?
                    WHERE id=? AND tenant_id=? AND active=1""",
                    (name.strip(),device_type,hourly_rate,int(is_free),device_id,user["tenant_id"])).rowcount
                if not changed:
                    db.rollback()
                    return self.send_json(404, {"error": "دستگاه فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"gamenet.device.updated","game_device",device_id)
                db.commit()
                return self.send_json(200, {"ok":True})
            if path == "/api/gamenet/reservations":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403,{"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403,{"error":"دسترسی ثبت رزرو ندارید."})
                device_id=data.get("deviceId"); customer_id=data.get("customerId")
                starts=data.get("startsAt"); ends=data.get("endsAt"); players=data.get("playersCount",1); notes=data.get("notes","")
                if (not isinstance(device_id,str) or not 1<=len(device_id)<=100 or
                        (customer_id is not None and (not isinstance(customer_id,str) or not 1<=len(customer_id)<=100)) or
                        not isinstance(starts,str) or not isinstance(ends,str)):
                    return self.send_json(400,{"error":"دستگاه، مشتری یا زمان رزرو معتبر نیست."})
                if type(players) is not int or not 1<=players<=16 or not isinstance(notes,str) or len(notes)>500:
                    return self.send_json(400,{"error":"تعداد بازیکنان یا یادداشت رزرو معتبر نیست."})
                from datetime import datetime
                try:
                    start_dt=datetime.fromisoformat(starts); end_dt=datetime.fromisoformat(ends)
                    if start_dt.tzinfo is None or start_dt.utcoffset() is None or end_dt.tzinfo is None or end_dt.utcoffset() is None:
                        return self.send_json(400,{"error":"زمان رزرو باید همراه با منطقه زمانی یا اختلاف UTC ارسال شود."})
                    start_ts=int(start_dt.timestamp()); end_ts=int(end_dt.timestamp())
                except (TypeError,ValueError,OverflowError):
                    return self.send_json(400,{"error":"زمان رزرو معتبر نیست."})
                now=int(time.time())
                if start_ts<now-60 or end_ts-start_ts<15*60 or end_ts-start_ts>24*60*60:
                    return self.send_json(400,{"error":"رزرو باید از ۱۵ دقیقه تا ۲۴ ساعت باشد و از بیش از یک دقیقه قبل شروع نشده باشد."})
                reservation_id=str(data.get("id") or secrets.token_urlsafe(18))
                if not 1<=len(reservation_id)<=100:
                    return self.send_json(400,{"error":"شناسه رزرو معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("""SELECT device_id,customer_id,starts_at,ends_at,players_count,notes,status
                    FROM gamenet_reservations WHERE id=? AND tenant_id=?""",(reservation_id,user["tenant_id"])).fetchone()
                if existing:
                    if (existing["device_id"]==device_id and existing["customer_id"]==customer_id and
                        existing["starts_at"]==start_ts and existing["ends_at"]==end_ts and
                        existing["players_count"]==players and existing["notes"]==notes and existing["status"]=="booked"):
                        db.commit()
                        return self.send_json(200,{"id":reservation_id,"startsAt":start_ts,"endsAt":end_ts,"status":"booked","duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه رزرو قبلاً برای اطلاعات متفاوت استفاده شده است."})
                device=db.execute("SELECT id FROM game_devices WHERE id=? AND tenant_id=? AND active=1",(device_id,user["tenant_id"])).fetchone()
                if not device:
                    db.rollback(); return self.send_json(404,{"error":"دستگاه فعال پیدا نشد."})
                running=db.execute("SELECT 1 FROM game_sessions WHERE tenant_id=? AND device_id=? AND status='running' LIMIT 1",
                    (user["tenant_id"],device_id)).fetchone()
                if running:
                    db.rollback(); return self.send_json(409,{"error":"تا پایان نشست فعال امکان رزرو این دستگاه وجود ندارد."})
                if customer_id is not None and not db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?",(customer_id,user["tenant_id"])).fetchone():
                    db.rollback(); return self.send_json(404,{"error":"مشتری رزرو پیدا نشد."})
                conflict=db.execute("""SELECT id FROM gamenet_reservations WHERE tenant_id=? AND device_id=? AND status='booked'
                    AND starts_at<? AND ends_at>? LIMIT 1""",(user["tenant_id"],device_id,end_ts,start_ts)).fetchone()
                if conflict:
                    db.rollback(); return self.send_json(409,{"error":"این دستگاه در بازه انتخاب‌شده رزرو دیگری دارد."})
                now=int(time.time())
                db.execute("""INSERT INTO gamenet_reservations(id,tenant_id,device_id,customer_id,created_by,starts_at,ends_at,status,players_count,notes,created_at)
                    VALUES(?,?,?,?,?,?,?,'booked',?,?,?)""",(reservation_id,user["tenant_id"],device_id,customer_id,user["id"],start_ts,end_ts,players,notes,now))
                audit(db,user["tenant_id"],user["id"],"gamenet.reservation.created","gamenet_reservation",reservation_id,
                    {"deviceId":device_id,"startsAt":start_ts,"endsAt":end_ts})
                db.commit(); return self.send_json(201,{"id":reservation_id,"startsAt":start_ts,"endsAt":end_ts,"status":"booked"})
            if path.startswith("/api/gamenet/reservations/") and path.endswith("/cancel"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403,{"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403,{"error":"دسترسی لغو رزرو ندارید."})
                reservation_id=path.split("/")[-2]
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("SELECT status FROM gamenet_reservations WHERE id=? AND tenant_id=?",
                                     (reservation_id,user["tenant_id"])).fetchone()
                if not existing:
                    db.rollback(); return self.send_json(404,{"error":"رزرو پیدا نشد."})
                if existing["status"]=="cancelled":
                    db.commit(); return self.send_json(200,{"ok":True,"status":"cancelled","duplicate":True})
                if existing["status"]!="booked":
                    db.rollback(); return self.send_json(409,{"error":"فقط رزرو فعال قابل لغو است."})
                db.execute("UPDATE gamenet_reservations SET status='cancelled' WHERE id=? AND tenant_id=? AND status='booked'",
                           (reservation_id,user["tenant_id"]))
                audit(db,user["tenant_id"],user["id"],"gamenet.reservation.cancelled","gamenet_reservation",reservation_id)
                db.commit(); return self.send_json(200,{"ok":True,"status":"cancelled","duplicate":False})
            if path == "/api/gamenet/sessions":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی شروع نشست بازی ندارید."})
                device_id, customer_id = data.get("deviceId"), data.get("customerId")
                players = data.get("playersCount",1)
                if (not isinstance(device_id,str) or not 1<=len(device_id)<=100 or
                        (customer_id is not None and (not isinstance(customer_id,str) or not 1<=len(customer_id)<=100)) or
                        type(players) is not int or not 1 <= players <= 16):
                    return self.send_json(400, {"error": "دستگاه، مشتری یا تعداد بازیکنان معتبر نیست."})
                session_id=str(data.get("id") or secrets.token_urlsafe(18))
                if not 1<=len(session_id)<=100: return self.send_json(400,{"error":"شناسه نشست معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("""SELECT device_id,customer_id,players_count,status,started_at FROM game_sessions
                    WHERE id=? AND tenant_id=?""",(session_id,user["tenant_id"])).fetchone()
                if existing:
                    if (existing["device_id"]==device_id and existing["customer_id"]==customer_id and
                        existing["players_count"]==players):
                        db.commit()
                        return self.send_json(200,{"id":session_id,"startedAt":existing["started_at"],"ok":True,"duplicate":True,"status":existing["status"]})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه نشست قبلاً برای اطلاعات متفاوت استفاده شده است."})
                device = db.execute("SELECT * FROM game_devices WHERE id=? AND tenant_id=? AND active=1",
                                    (device_id,user["tenant_id"])).fetchone()
                if not device:
                    db.rollback()
                    return self.send_json(404, {"error": "دستگاه فعال پیدا نشد."})
                if customer_id is not None and not db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?",
                                                               (customer_id,user["tenant_id"])).fetchone():
                    db.rollback()
                    return self.send_json(404, {"error": "مشتری انتخاب‌شده پیدا نشد."})
                now=int(time.time())
                active_reservation=db.execute("""SELECT id,customer_id FROM gamenet_reservations
                    WHERE tenant_id=? AND device_id=? AND status='booked' AND starts_at<=? AND ends_at>?
                    ORDER BY starts_at LIMIT 1""",(user["tenant_id"],device_id,now,now)).fetchone()
                if active_reservation and (active_reservation["customer_id"] is None or active_reservation["customer_id"]!=customer_id):
                    db.rollback(); return self.send_json(409,{"error":"این دستگاه در حال حاضر رزرو شده و برای این نشست قابل استفاده نیست."})
                if active_reservation:
                    completed=db.execute("""UPDATE gamenet_reservations SET status='completed'
                        WHERE id=? AND tenant_id=? AND status='booked'""",
                        (active_reservation["id"],user["tenant_id"]))
                    if completed.rowcount != 1:
                        db.rollback()
                        return self.send_json(409,{"error":"وضعیت رزرو تغییر کرده است؛ دوباره تلاش کنید."})
                db.execute("""INSERT INTO game_sessions(id,tenant_id,device_id,customer_id,started_by,started_at,status,
                    players_count,rate_per_hour,is_free,created_at) VALUES(?,?,?,?,?,?,'running',?,?,?,?)""",
                    (session_id,user["tenant_id"],device_id,customer_id,user["id"],now,players,device["hourly_rate"],device["is_free"],now))
                db.execute("""INSERT INTO game_session_segments
                    (id,tenant_id,session_id,device_id,started_at,players_count,rate_per_hour,is_free)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (secrets.token_urlsafe(18),user["tenant_id"],session_id,device_id,now,players,
                     device["hourly_rate"],device["is_free"]))
                audit(db,user["tenant_id"],user["id"],"gamenet.session.started","game_session",session_id,
                      {"deviceId":device_id,"playersCount":players})
                db.commit()
                return self.send_json(201, {"id":session_id,"startedAt":now,"ok":True})
            if path.startswith("/api/gamenet/sessions/") and path.endswith("/adjust"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error":"ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id=path.split("/")[-2]
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error":"دسترسی تغییر نشست بازی ندارید."})
                requested_device=data.get("deviceId")
                requested_players=data.get("playersCount")
                requested_free=data.get("isFree")
                if requested_device is not None and (not isinstance(requested_device,str) or not 1<=len(requested_device)<=100):
                    return self.send_json(400, {"error":"شناسه دستگاه معتبر نیست."})
                if requested_players is not None and (type(requested_players) is not int or not 1<=requested_players<=16):
                    return self.send_json(400, {"error":"تعداد بازیکنان باید بین ۱ تا ۱۶ باشد."})
                if requested_free is not None and type(requested_free) is not bool:
                    return self.send_json(400, {"error":"حالت رایگان/پولی معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                session=db.execute("SELECT * FROM game_sessions WHERE id=? AND tenant_id=?",
                    (session_id,user["tenant_id"])).fetchone()
                if not session:
                    db.rollback(); return self.send_json(404, {"error":"نشست پیدا نشد."})
                if session["status"]!="running":
                    db.rollback(); return self.send_json(409, {"error":"فقط نشست در حال اجرا قابل تغییر است."})
                target_id=requested_device or session["device_id"]
                target=db.execute("SELECT * FROM game_devices WHERE id=? AND tenant_id=? AND active=1",
                    (target_id,user["tenant_id"])).fetchone()
                if not target:
                    db.rollback(); return self.send_json(404, {"error":"دستگاه مقصد فعال پیدا نشد."})
                occupied=db.execute("""SELECT id FROM game_sessions
                    WHERE tenant_id=? AND device_id=? AND status='running' AND id<>?""",
                    (user["tenant_id"],target_id,session_id)).fetchone()
                if occupied:
                    db.rollback(); return self.send_json(409, {"error":"دستگاه مقصد در حال حاضر میزبان نشست دیگری است."})
                players=requested_players if requested_players is not None else session["players_count"]
                changed_device=target_id!=session["device_id"]
                now=int(time.time())
                if changed_device:
                    active_reservation=db.execute("""SELECT customer_id FROM gamenet_reservations
                        WHERE tenant_id=? AND device_id=? AND status='booked'
                        AND starts_at<=? AND ends_at>? ORDER BY starts_at LIMIT 1""",
                        (user["tenant_id"],target_id,now,now)).fetchone()
                    if active_reservation and (active_reservation["customer_id"] is None or active_reservation["customer_id"]!=session["customer_id"]):
                        db.rollback()
                        return self.send_json(409, {"error":"دستگاه مقصد در حال حاضر رزرو شده و برای این نشست قابل استفاده نیست."})
                rate=target["hourly_rate"] if changed_device else session["rate_per_hour"]
                free=target["is_free"] if changed_device else session["is_free"]
                if requested_free is not None: free=int(requested_free)
                if target_id==session["device_id"] and players==session["players_count"] and rate==session["rate_per_hour"] and int(free)==session["is_free"]:
                    db.commit()
                    return self.send_json(200, {"ok":True,"unchanged":True,"deviceId":target_id,
                        "playersCount":players,"ratePerHour":rate,"isFree":bool(free)})
                active_segment=db.execute("""SELECT id FROM game_session_segments
                    WHERE tenant_id=? AND session_id=? AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1""",(user["tenant_id"],session_id)).fetchone()
                if not active_segment:
                    db.rollback(); return self.send_json(409, {"error":"بخش فعال صورتحساب نشست پیدا نشد."})
                segment_closed=db.execute("UPDATE game_session_segments SET ended_at=? WHERE id=? AND tenant_id=? AND session_id=? AND ended_at IS NULL",
                    (max(now,session["started_at"]),active_segment["id"],user["tenant_id"],session_id)).rowcount
                if segment_closed != 1:
                    db.rollback()
                    return self.send_json(409, {"error":"بخش فعال نشست هم‌زمان تغییر کرده است؛ دوباره تلاش کنید."})
                db.execute("""INSERT INTO game_session_segments
                    (id,tenant_id,session_id,device_id,started_at,players_count,rate_per_hour,is_free)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (secrets.token_urlsafe(18),user["tenant_id"],session_id,target_id,now,players,rate,int(free)))
                session_changed=db.execute("""UPDATE game_sessions SET device_id=?,players_count=?,rate_per_hour=?,is_free=?
                    WHERE id=? AND tenant_id=? AND status='running'""",
                    (target_id,players,rate,int(free),session_id,user["tenant_id"])).rowcount
                if session_changed != 1:
                    db.rollback()
                    return self.send_json(409, {"error":"وضعیت نشست هم‌زمان تغییر کرده است؛ دوباره تلاش کنید."})
                audit(db,user["tenant_id"],user["id"],"gamenet.session.adjusted","game_session",session_id,
                    {"deviceId":target_id,"playersCount":players,"ratePerHour":rate,"isFree":bool(free)})
                db.commit()
                return self.send_json(200, {"ok":True,"deviceId":target_id,"deviceName":target["name"],
                    "playersCount":players,"ratePerHour":rate,"isFree":bool(free),"changedAt":now})
            if path.startswith("/api/gamenet/sessions/") and path.endswith("/end"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id = path.split("/")[-2]
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی پایان نشست بازی ندارید."})
                db.execute("BEGIN IMMEDIATE")
                session = db.execute("SELECT * FROM game_sessions WHERE id=? AND tenant_id=?",
                                     (session_id,user["tenant_id"])).fetchone()
                if not session:
                    db.rollback()
                    return self.send_json(404, {"error": "نشست پیدا نشد."})
                payment_method=data.get("paymentMethod","credit" if session["customer_id"] else "cash")
                if payment_method not in ("cash","card","online","credit"):
                    db.rollback()
                    return self.send_json(400,{"error":"روش تسویه نشست معتبر نیست."})
                if session["status"]=="ended":
                    if payment_method != session["payment_method"]:
                        db.rollback()
                        return self.send_json(409,{"error":"روش تسویه با پرداخت ثبت‌شده این نشست مطابقت ندارد."})
                    elapsed=max(0,int(session["ended_at"] or session["started_at"])-session["started_at"])
                    db.commit()
                    return self.send_json(200,{"ok":True,"amountDue":session["amount_due"],
                        "elapsedSeconds":elapsed,"paymentMethod":session["payment_method"],"duplicate":True})
                if session["status"]!="running":
                    db.rollback()
                    return self.send_json(409,{"error":"این نشست قابل پایان‌دادن نیست."})
                if payment_method=="credit" and not session["customer_id"]:
                    db.rollback()
                    return self.send_json(400,{"error":"برای تسویه اعتباری باید مشتری نشست مشخص باشد."})
                now = int(time.time())
                elapsed = max(0,now-session["started_at"])
                db.execute("""UPDATE game_session_segments SET ended_at=?
                    WHERE tenant_id=? AND session_id=? AND ended_at IS NULL""",
                    (max(now,session["started_at"]),user["tenant_id"],session_id))
                segments=db.execute("""SELECT started_at,ended_at,rate_per_hour,is_free
                    FROM game_session_segments WHERE tenant_id=? AND session_id=? ORDER BY started_at,id""",
                    (user["tenant_id"],session_id)).fetchall()
                amount=0
                for segment in segments:
                    segment_seconds=max(0,int(segment["ended_at"] or now)-int(segment["started_at"]))
                    segment_minutes=(segment_seconds+59)//60
                    if not segment["is_free"]:
                        amount += (int(segment["rate_per_hour"])*segment_minutes+59)//60
                if amount > 9007199254740991:
                    db.rollback()
                    return self.send_json(400, {"error":"مبلغ صورتحساب نشست بیش از حد مجاز است."})
                ended=db.execute("""UPDATE game_sessions SET status='ended',ended_at=?,amount_due=?,payment_method=?
                    WHERE id=? AND tenant_id=? AND status='running'""",
                    (now,amount,payment_method,session_id,user["tenant_id"]))
                if ended.rowcount != 1:
                    db.rollback()
                    return self.send_json(409,{"error":"وضعیت نشست تغییر کرده است؛ دوباره تلاش کنید."})
                if amount > 0:
                    tx_id = "game-"+session_id
                    title = "هزینه بازی "+session_id
                    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(now))
                    db.execute("""INSERT INTO transactions(id,tenant_id,created_by,title,amount,kind,category,occurred_at,created_at)
                        VALUES(?,?,?,?,?,'income','گیم‌نت',?,?)""", (tx_id,user["tenant_id"],user["id"],title,amount,occurred_at,now))
                    if payment_method=="credit":
                        db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                            VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],session["customer_id"],user["id"],
                            "game_session",session_id,amount,"هزینه بازی "+session_id,now))
                        post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title,amount,"income",occurred_at,"1100")
                    else:
                        payment_account={"cash":"1000","card":"1010","online":"1020"}[payment_method]
                        post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title,amount,"income",occurred_at,payment_account)
                audit(db,user["tenant_id"],user["id"],"gamenet.session.ended","game_session",session_id,
                      {"amount":amount,"elapsedSeconds":elapsed,"paymentMethod":payment_method})
                db.commit()
                return self.send_json(200, {"ok":True,"amountDue":amount,"elapsedSeconds":elapsed,"paymentMethod":payment_method})
            if path == "/api/loyalty/program":
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error":"ماژول مشتریان در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403, {"error":"فقط مالک یا مدیر می‌تواند تنظیمات باشگاه مشتریان را تغییر دهد."})
                enabled=data.get("enabled")
                spend_amount=data.get("spendAmount")
                points_awarded=data.get("pointsAwarded")
                if (type(enabled) is not bool or type(spend_amount) is not int or
                    not 10000<=spend_amount<=1000000000000 or type(points_awarded) is not int or
                    not 1<=points_awarded<=1000):
                    return self.send_json(400, {"error":"وضعیت، مبلغ مبنا یا تعداد امتیاز معتبر نیست."})
                now=int(time.time())
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO loyalty_programs(tenant_id,enabled,spend_amount,points_awarded,created_at,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET
                    enabled=excluded.enabled,spend_amount=excluded.spend_amount,
                    points_awarded=excluded.points_awarded,updated_at=excluded.updated_at""",
                    (user["tenant_id"],int(enabled),spend_amount,points_awarded,now,now))
                audit(db,user["tenant_id"],user["id"],"loyalty.program.updated","loyalty_program",user["tenant_id"],
                    {"enabled":enabled,"spendAmount":spend_amount,"pointsAwarded":points_awarded})
                db.commit()
                return self.send_json(200, {"ok":True,"program":{"enabled":enabled,
                    "spendAmount":spend_amount,"pointsAwarded":points_awarded,"updatedAt":now}})
            if path == "/api/orders":
                if not module_enabled(db,user["tenant_id"],"cafe"):
                    return self.send_json(403, {"error": "ماژول کافه در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی ثبت سفارش ندارید."})
                items = data.get("items"); discount = data.get("discount",0)
                payment = data.get("paymentMethod","cash"); customer_id = data.get("customerId")
                if not isinstance(items,list) or not 1 <= len(items) <= 100:
                    return self.send_json(400, {"error": "سفارش باید حداقل یک قلم و حداکثر ۱۰۰ قلم داشته باشد."})
                if type(discount) is not int or discount < 0 or payment not in ("cash","card","online","credit"):
                    return self.send_json(400, {"error": "تخفیف یا روش پرداخت معتبر نیست."})
                if customer_id is not None and not isinstance(customer_id,str):
                    return self.send_json(400, {"error": "شناسه مشتری معتبر نیست."})
                normalized = {}
                normalized_lines = {}
                for line in items:
                    if not isinstance(line,dict): return self.send_json(400, {"error": "قلم سفارش معتبر نیست."})
                    product_id, quantity = line.get("productId"), line.get("quantity")
                    addon_ids = line.get("addons", [])
                    if not isinstance(product_id,str) or type(quantity) is not int or not 1 <= quantity <= 100000:
                        return self.send_json(400, {"error": "کالا یا تعداد سفارش معتبر نیست."})
                    if (not isinstance(addon_ids,list) or len(addon_ids)>20
                            or any(not isinstance(addon_id,str) or not 1<=len(addon_id)<=100 for addon_id in addon_ids)
                            or len(set(addon_ids)) != len(addon_ids)):
                        return self.send_json(400, {"error": "فهرست افزودنی‌های سفارش معتبر نیست."})
                    addon_key = tuple(sorted(addon_ids))
                    line_key = (product_id,addon_key)
                    normalized_lines[line_key] = normalized_lines.get(line_key,0) + quantity
                    normalized[product_id] = normalized.get(product_id,0) + quantity
                order_id=data.get("id") or secrets.token_urlsafe(18)
                if not isinstance(order_id,str) or not 1<=len(order_id)<=100:
                    return self.send_json(400,{"error":"شناسه سفارش معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("""SELECT id,order_number,customer_id,discount,total,payment_method,status
                    FROM orders WHERE id=? AND tenant_id=?""",(order_id,user["tenant_id"])).fetchone()
                if existing:
                    old_items={}
                    stored_order_lines=db.execute("""SELECT product_id,quantity,unit_cost,addons_json
                        FROM order_items WHERE tenant_id=? AND order_id=?""",
                        (user["tenant_id"],order_id)).fetchall()
                    for row in stored_order_lines:
                        try:
                            saved_addons=json.loads(row["addons_json"] or "[]")
                            saved_ids=tuple(sorted(item["id"] for item in saved_addons if isinstance(item,dict) and isinstance(item.get("id"),str)))
                        except (TypeError,ValueError):
                            saved_ids=()
                        old_key=(row["product_id"],saved_ids)
                        old_items[old_key]=old_items.get(old_key,0)+row["quantity"]
                    if (old_items==normalized_lines and existing["customer_id"]==customer_id and
                        existing["discount"]==discount and existing["payment_method"]==payment and
                        existing["status"]=="paid"):
                        expected_payment_account={"cash":"1000","card":"1010","online":"1020","credit":"1100"}[payment]
                        transaction_id="order-"+order_id
                        tx=db.execute("""SELECT amount,kind FROM transactions
                            WHERE tenant_id=? AND id=?""",(user["tenant_id"],transaction_id)).fetchone()
                        sales_rows=db.execute("""SELECT a.code,l.debit,l.credit FROM journal_entries e
                            JOIN journal_lines l ON l.entry_id=e.id AND l.tenant_id=e.tenant_id
                            JOIN accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                            WHERE e.tenant_id=? AND e.source_type='transaction' AND e.source_id=?""",
                            (user["tenant_id"],transaction_id)).fetchall()
                        expected_sales=[(expected_payment_account,existing["total"],0),("4000",0,existing["total"])]
                        actual_sales=sorted((row["code"],row["debit"],row["credit"]) for row in sales_rows)
                        expected_sales_cost=sum(row["unit_cost"]*row["quantity"] for row in stored_order_lines)
                        cogs_entry=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                            AND source_type='order_cogs' AND source_id=?""",(user["tenant_id"],order_id)).fetchone()
                        cogs_rows=[]
                        if cogs_entry:
                            cogs_rows=db.execute("""SELECT a.code,l.debit,l.credit FROM journal_lines l
                                JOIN accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                                WHERE l.tenant_id=? AND l.entry_id=?""",
                                (user["tenant_id"],cogs_entry["id"])).fetchall()
                        expected_cogs=sorted([("5100",expected_sales_cost,0),("1200",0,expected_sales_cost)]) if expected_sales_cost else []
                        actual_cogs=sorted((row["code"],row["debit"],row["credit"]) for row in cogs_rows)
                        journals_valid=(tx is not None and tx["amount"]==existing["total"] and
                            tx["kind"]=="income" and len(sales_rows)==2 and actual_sales==sorted(expected_sales) and
                            ((expected_sales_cost==0 and cogs_entry is None) or
                             (expected_sales_cost>0 and cogs_entry is not None and len(cogs_rows)==2 and actual_cogs==expected_cogs)))
                        if journals_valid:
                            db.commit()
                            return self.send_json(200,{"id":existing["id"],"orderNumber":existing["order_number"],
                                "total":existing["total"],"duplicate":True})
                        db.rollback()
                        return self.send_json(409,{"error":"سندهای حسابداری سفارش ناقص یا نامعتبر هستند؛ تکرار سفارش پذیرفته نشد."})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه سفارش قبلاً برای اطلاعات متفاوت یا سفارش برگشت‌خورده استفاده شده است."})
                if customer_id and not db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?", (customer_id,user["tenant_id"])).fetchone():
                    db.rollback()
                    return self.send_json(404, {"error": "مشتری انتخاب‌شده پیدا نشد."})
                if payment == "credit" and not customer_id:
                    db.rollback()
                    return self.send_json(400, {"error": "برای فروش اعتباری باید مشتری مشخص شود."})
                products, subtotal, cogs_total, order_lines = {}, 0, 0, []
                for product_id, quantity in normalized.items():
                    product = db.execute("SELECT id,name,sell_price,cost_price FROM products WHERE id=? AND tenant_id=? AND active=1",
                                          (product_id,user["tenant_id"])).fetchone()
                    if not product:
                        db.rollback()
                        return self.send_json(404, {"error": "یکی از کالاهای سفارش پیدا نشد."})
                    stock_row = db.execute("""SELECT COALESCE(SUM(quantity_delta),0) AS quantity,
                        COALESCE(SUM(quantity_delta*unit_cost),0) AS stock_value
                        FROM inventory_movements WHERE tenant_id=? AND product_id=?""",
                        (user["tenant_id"],product_id)).fetchone()
                    stock, stock_value = stock_row["quantity"], stock_row["stock_value"]
                    if stock < quantity:
                        db.rollback()
                        return self.send_json(409, {"error": "موجودی کالای %s کافی نیست؛ موجودی: %s" % (product["name"],stock)})
                    average_cost = max(0, stock_value // stock) if stock else product["cost_price"]
                    line_cost = average_cost * quantity
                    if line_cost > 9007199254740991 or cogs_total + line_cost > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "بهای تمام‌شده سفارش بیش از حد مجاز است."})
                    cogs_total += line_cost
                    products[product_id] = (product,quantity,average_cost)
                for (product_id,addon_ids),quantity in normalized_lines.items():
                    product = products[product_id][0]
                    addon_rows = []
                    if addon_ids:
                        placeholders = ",".join("?" for _ in addon_ids)
                        addon_rows = db.execute("SELECT id,name,price FROM product_addons WHERE tenant_id=? AND product_id=? AND active=1 AND id IN ("+placeholders+")",
                                                (user["tenant_id"],product_id,*addon_ids)).fetchall()
                        if len(addon_rows) != len(addon_ids):
                            db.rollback()
                            return self.send_json(409, {"error":"یکی از افزودنی‌های انتخاب‌شده برای این کالا دیگر فعال یا معتبر نیست."})
                    addon_snapshot = [{"id":row["id"],"name":row["name"],"price":row["price"]} for row in addon_rows]
                    unit_price = product["sell_price"] + sum(row["price"] for row in addon_rows)
                    line_total = unit_price * quantity
                    if line_total > 9007199254740991 or subtotal + line_total > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "مبلغ سفارش بیش از حد مجاز است."})
                    subtotal += line_total
                    order_lines.append((product_id,product,quantity,unit_price,products[product_id][2],line_total,addon_snapshot))
                if discount >= subtotal:
                    db.rollback()
                    return self.send_json(400, {"error": "تخفیف باید از مبلغ سفارش کمتر باشد."})
                total = subtotal - discount
                line_discounts=allocate_proportional_discount([line[5] for line in order_lines],discount)
                now = int(time.time())
                order_number = "MG-%s-%s" % (time.strftime("%Y%m%d"), secrets.token_hex(3).upper())
                db.execute("""INSERT INTO orders(id,tenant_id,order_number,customer_id,created_by,status,subtotal,discount,total,payment_method,created_at)
                    VALUES(?,?,?,?,?,'paid',?,?,?,?,?)""", (order_id,user["tenant_id"],order_number,customer_id,user["id"],subtotal,discount,total,payment,now))
                # Credit orders accrue only after settlement; cash/card/online sales earn immediately.
                earned_points=0 if payment=="credit" else award_customer_loyalty_points(
                    db,user["tenant_id"],customer_id,user["id"],order_id,total,now)
                if payment == "credit":
                    db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                        VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],customer_id,user["id"],
                        "order",order_id,total,"فروش اعتباری "+order_number,now))
                for line_index,(product_id,product,quantity,unit_price,average_cost,line_total,addon_snapshot) in enumerate(order_lines):
                    db.execute("""INSERT INTO order_items(id,tenant_id,order_id,product_id,product_name,quantity,unit_price,unit_cost,line_total,discount_amount,addons_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],order_id,product_id,product["name"],quantity,unit_price,average_cost,line_total,line_discounts[line_index],json.dumps(addon_snapshot,ensure_ascii=False,separators=(",",":"))))
                for product_id,(product,quantity,average_cost) in products.items():
                    db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],product_id,user["id"],"sale",-quantity,average_cost,"سفارش "+order_number,now))
                post_order_cogs_journal(db,user["tenant_id"],user["id"],order_id,cogs_total)
                tx_id = "order-"+order_id
                title = "فروش سفارش "+order_number
                occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(now))
                db.execute("""INSERT INTO transactions(id,tenant_id,created_by,title,amount,kind,category,occurred_at,created_at)
                    VALUES(?,?,?,?,?,'income','فروش کافه',?,?)""", (tx_id,user["tenant_id"],user["id"],title,total,occurred_at,now))
                payment_account = {"cash":"1000","card":"1010","online":"1020","credit":"1100"}[payment]
                post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title,total,"income",occurred_at,payment_account)
                audit(db,user["tenant_id"],user["id"],"order.created","order",order_id,{"total":total,"items":len(order_lines)})
                db.commit()
                return self.send_json(201, {"id":order_id,"orderNumber":order_number,"subtotal":subtotal,"discount":discount,"total":total})
            if path == "/api/account/password":
                current_password = data.get("currentPassword")
                new_password = data.get("newPassword")
                if not isinstance(current_password,str) or len(current_password) > 256 or not isinstance(new_password,str) or not 12 <= len(new_password) <= 256:
                    return self.send_json(400,{"error":"رمز فعلی و رمز جدید معتبر الزامی است؛ رمز جدید باید ۱۲ تا ۲۵۶ نویسه باشد."})
                if not verify_password(current_password,user["password_hash"]):
                    return self.send_json(403,{"error":"رمز عبور فعلی صحیح نیست."})
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE users SET password_hash=? WHERE id=? AND tenant_id=?",
                           (password_hash(new_password),user["id"],user["tenant_id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?",(user["id"],))
                audit(db,user["tenant_id"],user["id"],"user.password.changed","user",user["id"])
                db.commit()
                return self.create_session(db,user["id"])
            if path.startswith("/api/users/") and path.endswith("/role"):
                if user["role"] != "owner":
                    return self.send_json(403,{"error":"فقط مالک می‌تواند نقش کاربران را تغییر دهد."})
                target_id=path.split("/")[-2]
                new_role=data.get("role")
                if new_role not in ("manager","cashier","viewer"):
                    return self.send_json(400,{"error":"نقش کاربر معتبر نیست."})
                if target_id==user["id"]:
                    return self.send_json(400,{"error":"تغییر نقش حساب مالک از این مسیر مجاز نیست."})
                db.execute("BEGIN IMMEDIATE")
                target=db.execute("SELECT id,role,active FROM users WHERE id=? AND tenant_id=?",
                                  (target_id,user["tenant_id"])).fetchone()
                if not target:
                    db.rollback(); return self.send_json(404,{"error":"کاربر پیدا نشد."})
                if not target["active"]:
                    db.rollback(); return self.send_json(409,{"error":"نقش کاربر غیرفعال قابل تغییر نیست."})
                if target["role"]==new_role:
                    db.rollback(); return self.send_json(200,{"ok":True,"changed":False,"sessionsRevoked":False})
                db.execute("UPDATE users SET role=? WHERE id=? AND tenant_id=?",
                           (new_role,target_id,user["tenant_id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?",(target_id,))
                audit(db,user["tenant_id"],user["id"],"user.role.changed","user",target_id,
                      {"from":target["role"],"to":new_role})
                db.commit()
                return self.send_json(200,{"ok":True,"changed":True,"sessionsRevoked":True})
            if path.startswith("/api/users/") and path.endswith("/password"):
                if user["role"] != "owner":
                    return self.send_json(403,{"error":"فقط مالک می‌تواند رمز کاربران را بازنشانی کند."})
                target_id=path.split("/")[-2]
                new_password=data.get("password")
                if not isinstance(new_password,str) or len(new_password)<12 or len(new_password)>256:
                    return self.send_json(400,{"error":"رمز جدید باید بین ۱۲ تا ۲۵۶ نویسه باشد."})
                db.execute("BEGIN IMMEDIATE")
                target=db.execute("SELECT id,active,role FROM users WHERE id=? AND tenant_id=?",
                                  (target_id,user["tenant_id"])).fetchone()
                if not target:
                    db.rollback(); return self.send_json(404,{"error":"کاربر پیدا نشد."})
                if not target["active"]:
                    db.rollback(); return self.send_json(409,{"error":"رمز کاربر غیرفعال قابل تغییر نیست."})
                if target["role"] == "owner":
                    db.rollback(); return self.send_json(403,{"error":"بازنشانی رمز حساب مالک از این مسیر مجاز نیست."})
                db.execute("UPDATE users SET password_hash=? WHERE id=? AND tenant_id=?",
                           (password_hash(new_password),target_id,user["tenant_id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?",(target_id,))
                audit(db,user["tenant_id"],user["id"],"user.password.reset","user",target_id)
                db.commit()
                return self.send_json(200,{"ok":True,"sessionsRevoked":True})
            if path == "/api/users":
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند کاربر جدید بسازد."})
                username, password, role = data.get("username"), data.get("password"), data.get("role")
                if not isinstance(username,str) or not 3 <= len(username.strip()) <= 64:
                    return self.send_json(400, {"error": "نام کاربری باید ۳ تا ۶۴ نویسه باشد."})
                if not isinstance(password,str) or not 12 <= len(password) <= 256:
                    return self.send_json(400, {"error": "رمز عبور باید بین ۱۲ تا ۲۵۶ نویسه باشد."})
                if role not in ("manager","cashier","viewer"):
                    return self.send_json(400, {"error": "نقش کاربر معتبر نیست."})
                normalized_username=username.strip()
                target_id, now = secrets.token_urlsafe(18), int(time.time())
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                               (target_id,user["tenant_id"],normalized_username,password_hash(password),role,now))
                except sqlite3.IntegrityError:
                    db.rollback()
                    return self.send_json(409, {"error":"این نام کاربری قبلاً در سامانه ثبت شده است."})
                audit(db,user["tenant_id"],user["id"],"user.created","user",target_id,{"role":role})
                db.commit()
                return self.send_json(201, {"id": target_id, "ok": True})
            if path == "/api/product-addons":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403,{"error":"ماژول کافه یا انبار فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403,{"error":"فقط مدیر می‌تواند افزودنی کالا را مدیریت کند."})
                product_id=data.get("productId"); name=data.get("name"); price=data.get("price")
                if not isinstance(product_id,str) or not isinstance(name,str) or not 1<=len(name.strip())<=80 or type(price) is not int or price<0 or price>9007199254740991:
                    return self.send_json(400,{"error":"کالا، نام افزودنی یا قیمت معتبر نیست."})
                product=db.execute("SELECT id FROM products WHERE id=? AND tenant_id=? AND active=1",
                                   (product_id,user["tenant_id"])).fetchone()
                if not product:
                    return self.send_json(404,{"error":"کالای فعال پیدا نشد."})
                addon_id=str(data.get("id") or secrets.token_urlsafe(18)); now=int(time.time())
                if not 1<=len(addon_id)<=100:
                    return self.send_json(400,{"error":"شناسه افزودنی معتبر نیست."})
                if db.execute("SELECT 1 FROM product_addons WHERE tenant_id=? AND product_id=? AND name=?",
                              (user["tenant_id"],product_id,name.strip())).fetchone():
                    return self.send_json(409,{"error":"این نام افزودنی قبلاً برای کالا استفاده شده است."})
                db.execute("INSERT INTO product_addons(id,tenant_id,product_id,name,price,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (addon_id,user["tenant_id"],product_id,name.strip(),price,now,now))
                audit(db,user["tenant_id"],user["id"],"product.addon.created","product_addon",addon_id,{"productId":product_id,"price":price})
                db.commit()
                return self.send_json(201,{"id":addon_id,"ok":True})
            if path.startswith("/api/product-addons/"):
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403,{"error":"ماژول کافه یا انبار فعال نیست."})
                if user["role"] not in ("owner","manager"):
                    return self.send_json(403,{"error":"فقط مدیر می‌تواند افزودنی کالا را مدیریت کند."})
                addon_id=path.rsplit("/",1)[-1]; name=data.get("name"); price=data.get("price")
                if not isinstance(name,str) or not 1<=len(name.strip())<=80 or type(price) is not int or price<0 or price>9007199254740991:
                    return self.send_json(400,{"error":"نام یا قیمت افزودنی معتبر نیست."})
                changed=db.execute("""UPDATE product_addons SET name=?,price=?,updated_at=?
                    WHERE id=? AND tenant_id=? AND active=1""",
                    (name.strip(),price,int(time.time()),addon_id,user["tenant_id"])).rowcount
                if not changed:
                    return self.send_json(404,{"error":"افزودنی فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"product.addon.updated","product_addon",addon_id,{"price":price})
                db.commit()
                return self.send_json(200,{"ok":True})
            if path == "/api/products":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                if user["role"] not in ("owner", "manager"):
                    return self.send_json(403, {"error": "دسترسی مدیریت کالا ندارید."})
                name = data.get("name"); sku = data.get("sku"); category = data.get("category", "عمومی")
                sell_price = data.get("sellPrice"); cost_price = data.get("costPrice", 0)
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 160:
                    return self.send_json(400, {"error": "نام کالا معتبر نیست."})
                if sku is not None and (not isinstance(sku,str) or len(sku.strip()) > 80):
                    return self.send_json(400, {"error": "کد کالا معتبر نیست."})
                if not isinstance(category,str) or len(category) > 80:
                    return self.send_json(400, {"error": "دسته‌بندی کالا معتبر نیست."})
                if type(sell_price) is not int or sell_price < 0 or type(cost_price) is not int or cost_price < 0:
                    return self.send_json(400, {"error": "قیمت فروش و خرید باید عدد صحیح و غیرمنفی باشند."})
                product_id, now = str(data.get("id") or secrets.token_urlsafe(18)), int(time.time())
                if not 1<=len(product_id)<=100: return self.send_json(400, {"error": "شناسه کالا معتبر نیست."})
                normalized_sku=sku.strip() if sku and sku.strip() else None
                db.execute("BEGIN IMMEDIATE")
                if normalized_sku and db.execute("SELECT 1 FROM products WHERE tenant_id=? AND sku=?",
                                                 (user["tenant_id"],normalized_sku)).fetchone():
                    db.rollback()
                    return self.send_json(409, {"error":"این کد کالا قبلاً ثبت شده است."})
                db.execute("""INSERT INTO products(id,tenant_id,name,sku,category,sell_price,cost_price,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (product_id,user["tenant_id"],name.strip(),normalized_sku,category.strip() or "عمومی",sell_price,cost_price,now,now))
                audit(db,user["tenant_id"],user["id"],"product.created","product",product_id,{"sellPrice":sell_price})
                db.commit()
                return self.send_json(201, {"id": product_id, "ok": True})
            if path == "/api/suppliers":
                if not module_enabled(db,user["tenant_id"],"inventory"):
                    return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                if user["role"] not in ("owner","manager"): return self.send_json(403,{"error":"دسترسی مدیریت تأمین‌کنندگان ندارید."})
                name=data.get("name"); phone=data.get("phone"); email=data.get("email"); notes=data.get("notes","")
                if not isinstance(name,str) or not 1<=len(name.strip())<=160: return self.send_json(400,{"error":"نام تأمین‌کننده معتبر نیست."})
                if phone is not None and (not isinstance(phone,str) or len(phone)>32): return self.send_json(400,{"error":"شماره تماس معتبر نیست."})
                if email is not None and (not isinstance(email,str) or len(email)>254): return self.send_json(400,{"error":"ایمیل معتبر نیست."})
                if not isinstance(notes,str) or len(notes)>2000: return self.send_json(400,{"error":"یادداشت معتبر نیست."})
                sid=str(data.get("id") or secrets.token_urlsafe(18)); now=int(time.time())
                if not 1<=len(sid)<=100: return self.send_json(400,{"error":"شناسه تأمین‌کننده معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM suppliers WHERE tenant_id=? AND name=?",(user["tenant_id"],name.strip())).fetchone():
                    db.rollback(); return self.send_json(409,{"error":"این نام تأمین‌کننده قبلاً ثبت شده است."})
                db.execute("INSERT INTO suppliers(id,tenant_id,name,phone,email,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (sid,user["tenant_id"],name.strip(),phone.strip() if phone and phone.strip() else None,email.strip() if email and email.strip() else None,notes,now,now))
                audit(db,user["tenant_id"],user["id"],"supplier.created","supplier",sid); db.commit()
                return self.send_json(201,{"id":sid,"ok":True})
            if path == "/api/purchase-invoices":
                if not module_enabled(db,user["tenant_id"],"inventory"): return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                if user["role"] not in ("owner","manager"): return self.send_json(403,{"error":"دسترسی ثبت فاکتور خرید ندارید."})
                sid=data.get("supplierId"); number=data.get("invoiceNumber"); inv_date=data.get("invoiceDate")
                items=data.get("items"); discount=data.get("discount",0)
                if not isinstance(sid,str) or not isinstance(number,str) or not 1<=len(number.strip())<=80: return self.send_json(400,{"error":"تأمین‌کننده یا شماره فاکتور معتبر نیست."})
                from datetime import date
                try: date.fromisoformat(inv_date)
                except (TypeError,ValueError): return self.send_json(400,{"error":"تاریخ فاکتور باید YYYY-MM-DD باشد."})
                if not isinstance(items,list) or not 1<=len(items)<=500 or type(discount) is not int or discount<0: return self.send_json(400,{"error":"اقلام یا تخفیف فاکتور معتبر نیست."})
                invoice_id=str(data.get("id") or secrets.token_urlsafe(18)); now=int(time.time())
                if not 1<=len(invoice_id)<=100: return self.send_json(400,{"error":"شناسه فاکتور معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM suppliers WHERE id=? AND tenant_id=? AND active=1",(sid,user["tenant_id"])).fetchone():
                    db.rollback(); return self.send_json(404,{"error":"تأمین‌کننده پیدا نشد."})
                prepared=[]; subtotal=0
                for item in items:
                    if not isinstance(item,dict): db.rollback(); return self.send_json(400,{"error":"قلم فاکتور نامعتبر است."})
                    pid=item.get("productId"); qty=item.get("quantity"); cost=item.get("unitCost")
                    if not isinstance(pid,str) or type(qty) is not int or not 1<=qty<=100000000 or type(cost) is not int or not 0<=cost<=9007199254740991:
                        db.rollback(); return self.send_json(400,{"error":"کالا، تعداد یا بهای خرید معتبر نیست."})
                    product=db.execute("SELECT id,name FROM products WHERE id=? AND tenant_id=? AND active=1",(pid,user["tenant_id"])).fetchone()
                    if not product: db.rollback(); return self.send_json(404,{"error":"کالای فاکتور پیدا نشد."})
                    line=qty*cost
                    if line>9007199254740991 or subtotal+line>9007199254740991: db.rollback(); return self.send_json(400,{"error":"مبلغ فاکتور بیش از حد مجاز است."})
                    subtotal+=line; prepared.append((product,qty,cost,line))
                if subtotal<=0 or discount>=subtotal:
                    db.rollback(); return self.send_json(400,{"error":"تخفیف باید کمتر از مبلغ فاکتور باشد."})
                total=subtotal-discount
                # Allocate the invoice discount proportionally by each line's gross value.
                # Largest-remainder allocation preserves every toman without floating-point rounding.
                allocations=[]; remainders=[]
                for index,(product,qty,cost,line) in enumerate(prepared):
                    allocated,remainder=divmod(line*discount,subtotal)
                    allocations.append(allocated); remainders.append((remainder,index))
                undistributed=discount-sum(allocations)
                for remainder,index in sorted(remainders,key=lambda item:(item[0],-item[1]),reverse=True)[:undistributed]:
                    allocations[index]+=1
                adjusted=[]
                for (product,qty,cost,line),line_discount in zip(prepared,allocations):
                    per_unit_discount,discounted_units=divmod(line_discount,qty)
                    regular_cost=cost-per_unit_discount
                    if regular_cost<0 or (discounted_units and regular_cost==0):
                        db.rollback(); return self.send_json(400,{"error":"تخفیف تخصیص‌یافته از بهای خرید اقلام بیشتر است."})
                    regular_units=qty-discounted_units
                    if regular_units:
                        adjusted.append((product,regular_units,regular_cost,regular_units*regular_cost))
                    if discounted_units:
                        adjusted.append((product,discounted_units,regular_cost-1,discounted_units*(regular_cost-1)))
                prepared=adjusted
                if sum(row[3] for row in prepared)!=total:
                    db.rollback(); return self.send_json(400,{"error":"تخصیص تخفیف فاکتور با جمع اقلام سازگار نیست."})
                existing=db.execute("""SELECT * FROM purchase_invoices WHERE tenant_id=?
                    AND (id=? OR invoice_number=?)""",(user["tenant_id"],invoice_id,number.strip())).fetchone()
                if existing:
                    existing_items=db.execute("""SELECT product_id,quantity,unit_cost FROM purchase_invoice_items
                        WHERE tenant_id=? AND invoice_id=? ORDER BY product_id,quantity,unit_cost""",
                        (user["tenant_id"],existing["id"])).fetchall()
                    requested_items=sorted((product["id"],qty,cost) for product,qty,cost,line in prepared)
                    stored_items=[(row["product_id"],row["quantity"],row["unit_cost"]) for row in existing_items]
                    if (existing["id"]==invoice_id and existing["supplier_id"]==sid and
                        existing["invoice_number"]==number.strip() and existing["invoice_date"]==inv_date and
                        existing["subtotal"]==subtotal and existing["discount"]==discount and
                        existing["total"]==total and stored_items==requested_items):
                        db.commit()
                        return self.send_json(200,{"id":invoice_id,"total":total,
                            "paidAmount":existing["paid_amount"],"status":existing["status"],"duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شماره یا شناسه فاکتور قبلاً برای اطلاعات متفاوت استفاده شده است."})
                db.execute("""INSERT INTO purchase_invoices(id,tenant_id,supplier_id,created_by,invoice_number,invoice_date,subtotal,discount,total,paid_amount,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,0,'unpaid',?)""",(invoice_id,user["tenant_id"],sid,user["id"],number.strip(),inv_date,subtotal,discount,total,now))
                for product,qty,cost,line in prepared:
                    db.execute("INSERT INTO purchase_invoice_items(id,tenant_id,invoice_id,product_id,product_name,quantity,unit_cost,line_total) VALUES(?,?,?,?,?,?,?,?)",
                        (secrets.token_urlsafe(16),user["tenant_id"],invoice_id,product["id"],product["name"],qty,cost,line))
                    mid=secrets.token_urlsafe(16); note="فاکتور خرید "+number.strip()
                    db.execute("INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (mid,user["tenant_id"],product["id"],user["id"],"purchase",qty,cost,note,now))
                    post_inventory_movement_journal(db,user["tenant_id"],user["id"],mid,"purchase",qty,cost,note)
                audit(db,user["tenant_id"],user["id"],"purchase_invoice.created","purchase_invoice",invoice_id,{"total":total,"items":len(prepared)})
                db.commit(); return self.send_json(201,{"id":invoice_id,"subtotal":subtotal,"discount":discount,"total":total,"paidAmount":0,"status":"unpaid"})
            if path.startswith("/api/purchase-invoices/") and path.endswith("/payments"):
                if not module_enabled(db,user["tenant_id"],"inventory"): return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"): return self.send_json(403,{"error":"دسترسی پرداخت ندارید."})
                iid=path.split("/")[-2]; amount=data.get("amount"); method=data.get("paymentMethod","cash")
                payid=str(data.get("id") or secrets.token_urlsafe(18))
                if type(amount) is not int or amount<=0 or amount>9007199254740991 or method not in ("cash","card","online"):
                    return self.send_json(400,{"error":"مبلغ یا روش پرداخت معتبر نیست."})
                if not 1<=len(payid)<=100: return self.send_json(400,{"error":"شناسه پرداخت معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                inv=db.execute("SELECT * FROM purchase_invoices WHERE id=? AND tenant_id=?",(iid,user["tenant_id"])).fetchone()
                if not inv: db.rollback(); return self.send_json(404,{"error":"فاکتور خرید پیدا نشد."})
                existing=db.execute("SELECT invoice_id,amount,payment_method FROM supplier_payments WHERE id=? AND tenant_id=?",
                                    (payid,user["tenant_id"])).fetchone()
                if existing:
                    journal_rows=db.execute("""SELECT a.code,l.debit,l.credit FROM journal_entries e
                        JOIN journal_lines l ON l.entry_id=e.id AND l.tenant_id=e.tenant_id
                        JOIN accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                        WHERE e.tenant_id=? AND e.source_type='supplier_payment' AND e.source_id=?""",
                        (user["tenant_id"],payid)).fetchall()
                    expected_account={"cash":"1000","card":"1010","online":"1020"}[method]
                    journal_matches=(len(journal_rows)==2 and
                        any(row["code"]==expected_account and row["debit"]==0 and row["credit"]==amount for row in journal_rows) and
                        any(row["code"]=="2000" and row["debit"]==amount and row["credit"]==0 for row in journal_rows))
                    if (existing["invoice_id"]==iid and existing["amount"]==amount and
                        existing["payment_method"]==method and journal_matches):
                        db.commit()
                        return self.send_json(200,{"paymentId":payid,"paidAmount":inv["paid_amount"],
                            "remainingBalance":inv["total"]-inv["paid_amount"],"status":inv["status"],"duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه پرداخت قبلاً برای اطلاعات متفاوت استفاده شده است."})
                remaining=inv["total"]-inv["paid_amount"]
                if amount>remaining: db.rollback(); return self.send_json(409,{"error":"پرداخت از مانده فاکتور بیشتر است."})
                now=int(time.time())
                db.execute("INSERT INTO supplier_payments(id,tenant_id,invoice_id,supplier_id,created_by,payment_method,amount,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (payid,user["tenant_id"],iid,inv["supplier_id"],user["id"],method,amount,now))
                paid=inv["paid_amount"]+amount; status="paid" if paid==inv["total"] else "partial"
                updated=db.execute("UPDATE purchase_invoices SET paid_amount=?,status=? WHERE id=? AND tenant_id=?",(paid,status,iid,user["tenant_id"]))
                if updated.rowcount != 1:
                    db.rollback()
                    return self.send_json(409,{"error":"وضعیت فاکتور برای ثبت پرداخت تغییر کرده است؛ دوباره تلاش کنید."})
                post_supplier_payment_journal(db,user["tenant_id"],user["id"],payid,amount,method)
                audit(db,user["tenant_id"],user["id"],"supplier.payment.created","purchase_invoice",iid,{"amount":amount})
                db.commit(); return self.send_json(201,{"paymentId":payid,"paidAmount":paid,"remainingBalance":inv["total"]-paid,"status":status})
            if path == "/api/inventory/movements":
                if not require_any_module(db,user["tenant_id"],"inventory","cafe"):
                    return self.send_json(403, {"error": "ماژول انبار یا کافه در لایسنس فعال نیست."})
                if user["role"] not in ("owner", "manager"):
                    return self.send_json(403, {"error": "دسترسی ثبت گردش انبار ندارید."})
                product_id = data.get("productId"); delta = data.get("quantityDelta")
                movement_type = data.get("movementType"); unit_cost = data.get("unitCost", 0)
                note = data.get("note", "")
                if not isinstance(product_id,str) or not 1 <= len(product_id) <= 100:
                    return self.send_json(400, {"error": "شناسه کالا معتبر نیست."})
                if type(delta) is not int or delta == 0 or abs(delta) > 100000000:
                    return self.send_json(400, {"error": "تعداد باید عدد صحیح و غیرصفر باشد."})
                if movement_type not in ("purchase","sale","adjustment","waste"):
                    return self.send_json(400, {"error": "نوع گردش انبار معتبر نیست."})
                if movement_type == "purchase" and delta < 0 or movement_type in ("sale","waste") and delta > 0:
                    return self.send_json(400, {"error": "جهت تعداد با نوع گردش انبار سازگار نیست."})
                if type(unit_cost) is not int or not 0 <= unit_cost <= 9007199254740991:
                    return self.send_json(400, {"error": "بهای واحد معتبر نیست."})
                if abs(delta) * unit_cost > 9007199254740991:
                    return self.send_json(400, {"error": "ارزش کل گردش انبار از محدوده مجاز بیشتر است."})
                if not isinstance(note,str) or len(note) > 300:
                    return self.send_json(400, {"error": "یادداشت بیش از حد طولانی است."})
                movement_id=str(data.get("id") or secrets.token_urlsafe(18))
                if not 1<=len(movement_id)<=100: return self.send_json(400,{"error":"شناسه گردش انبار معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("""SELECT product_id,movement_type,quantity_delta,unit_cost,note
                    FROM inventory_movements WHERE id=? AND tenant_id=?""",(movement_id,user["tenant_id"])).fetchone()
                if existing:
                    if (existing["product_id"]==product_id and existing["movement_type"]==movement_type and
                        existing["quantity_delta"]==delta and existing["unit_cost"]==unit_cost and existing["note"]==note):
                        expected_value=abs(delta)*unit_cost
                        journal=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                            AND source_type='inventory_movement' AND source_id=?""",
                            (user["tenant_id"],movement_id)).fetchone()
                        if expected_value > 0:
                            if not journal:
                                db.rollback()
                                return self.send_json(409,{"error":"سند حسابداری گردش انبار ناقص است؛ ثبت مجدد پذیرفته نشد."})
                            journal_lines=db.execute("""SELECT debit,credit FROM journal_lines
                                WHERE tenant_id=? AND entry_id=?""",(user["tenant_id"],journal["id"])).fetchall()
                            debit_total=sum(line["debit"] for line in journal_lines)
                            credit_total=sum(line["credit"] for line in journal_lines)
                            if len(journal_lines)<2 or debit_total!=expected_value or credit_total!=expected_value:
                                db.rollback()
                                return self.send_json(409,{"error":"سند حسابداری گردش انبار ناقص یا نامتوازن است."})
                        elif journal:
                            db.rollback()
                            return self.send_json(409,{"error":"برای گردش بدون ارزش، سند حسابداری غیرمنتظره ثبت شده است."})
                        db.commit()
                        return self.send_json(200,{"id":movement_id,"ok":True,"duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه گردش انبار قبلاً برای اطلاعات متفاوت استفاده شده است."})
                product = db.execute("SELECT id FROM products WHERE id=? AND tenant_id=? AND active=1",
                                     (product_id,user["tenant_id"])).fetchone()
                if not product:
                    db.rollback()
                    return self.send_json(404, {"error": "کالای فعال پیدا نشد."})
                current = db.execute("SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE tenant_id=? AND product_id=?",
                                     (user["tenant_id"],product_id)).fetchone()[0]
                if current + delta < 0:
                    db.rollback()
                    return self.send_json(409, {"error": "موجودی کافی نیست؛ موجودی فعلی: %s" % current})
                if current + delta > 9007199254740991:
                    db.rollback()
                    return self.send_json(409, {"error": "موجودی نهایی از محدوده عددی مجاز بیشتر است."})
                now = int(time.time())
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (movement_id,user["tenant_id"],product_id,user["id"],movement_type,delta,unit_cost,note,now))
                post_inventory_movement_journal(db,user["tenant_id"],user["id"],movement_id,movement_type,delta,unit_cost,note)
                audit(db,user["tenant_id"],user["id"],"inventory.movement.created","inventory_movement",movement_id,
                      {"productId":product_id,"quantityDelta":delta,"movementType":movement_type})
                db.commit()
                return self.send_json(201, {"id": movement_id, "ok": True})
            if path.startswith("/api/customers/") and path.endswith("/payments"):
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error": "ماژول مشتریان در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی دریافت از مشتری ندارید."})
                customer_id = path.split("/")[-2]
                amount, payment_method = data.get("amount"), data.get("paymentMethod","cash")
                payment_id=str(data.get("id") or secrets.token_urlsafe(18))
                if type(amount) is not int or amount <= 0 or amount > 9007199254740991 or payment_method not in ("cash","card","online"):
                    return self.send_json(400, {"error": "مبلغ یا روش دریافت معتبر نیست."})
                if not 1<=len(payment_id)<=100: return self.send_json(400,{"error":"شناسه دریافت معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                customer = db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?", (customer_id,user["tenant_id"])).fetchone()
                if not customer:
                    db.rollback()
                    return self.send_json(404, {"error": "مشتری پیدا نشد."})
                existing=db.execute("SELECT customer_id,credit FROM customer_ledger WHERE tenant_id=? AND source_type='payment' AND source_id=?",
                                    (user["tenant_id"],payment_id)).fetchone()
                if existing:
                    journal_rows=db.execute("""SELECT a.code,l.debit,l.credit FROM journal_entries e
                        JOIN journal_lines l ON l.entry_id=e.id AND l.tenant_id=e.tenant_id
                        JOIN accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                        WHERE e.tenant_id=? AND e.source_type='customer_payment' AND e.source_id=?""",
                        (user["tenant_id"],payment_id)).fetchall()
                    expected_account={"cash":"1000","card":"1010","online":"1020"}[payment_method]
                    payment_matches=(len(journal_rows)==2 and
                        any(row["code"]==expected_account and row["debit"]==amount and row["credit"]==0 for row in journal_rows) and
                        any(row["code"]=="1100" and row["debit"]==0 and row["credit"]==amount for row in journal_rows))
                    if (existing["customer_id"]==customer_id and existing["credit"]==amount and payment_matches):
                        balance=db.execute("SELECT COALESCE(SUM(debit-credit),0) FROM customer_ledger WHERE tenant_id=? AND customer_id=?",
                                           (user["tenant_id"],customer_id)).fetchone()[0]
                        db.commit()
                        return self.send_json(200,{"ok":True,"paymentId":payment_id,"remainingBalance":balance,"duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه دریافت قبلاً با اطلاعات پرداخت متفاوت استفاده شده است."})
                balance = db.execute("SELECT COALESCE(SUM(debit-credit),0) FROM customer_ledger WHERE tenant_id=? AND customer_id=?",
                                     (user["tenant_id"],customer_id)).fetchone()[0]
                if amount > balance:
                    db.rollback()
                    return self.send_json(409, {"error": "مبلغ دریافت از مانده بدهی مشتری بیشتر است."})
                now = int(time.time())
                db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                    VALUES(?,?,?,?,?,?,0,?,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],customer_id,user["id"],
                    "payment",payment_id,amount,"دریافت از مشتری",now))
                post_customer_payment_journal(db,user["tenant_id"],user["id"],payment_id,customer_id,amount,payment_method)
                audit(db,user["tenant_id"],user["id"],"customer.payment.received","customer",customer_id,{"amount":amount})
                db.commit()
                return self.send_json(201, {"ok":True,"paymentId":payment_id,"remainingBalance":balance-amount})
            if path == "/api/customers":
                if not require_any_module(db,user["tenant_id"],"customers","cafe","gamenet"):
                    return self.send_json(403, {"error": "ماژول مشتریان در لایسنس فعال نیست."})
                if user["role"] not in ("owner", "manager", "cashier"):
                    return self.send_json(403, {"error": "دسترسی مدیریت مشتریان ندارید."})
                name, phone = data.get("fullName"), data.get("phone")
                email, notes = data.get("email"), data.get("notes", "")
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 160:
                    return self.send_json(400, {"error": "نام مشتری معتبر نیست."})
                if phone is not None and (not isinstance(phone,str) or len(phone.strip()) > 32):
                    return self.send_json(400, {"error": "شماره تماس معتبر نیست."})
                if email is not None and (not isinstance(email,str) or len(email) > 254):
                    return self.send_json(400, {"error": "ایمیل معتبر نیست."})
                if not isinstance(notes,str) or len(notes) > 2000:
                    return self.send_json(400, {"error": "یادداشت بیش از حد طولانی است."})
                customer_id, now = str(data.get("id") or secrets.token_urlsafe(18)), int(time.time())
                if not 1<=len(customer_id)<=100: return self.send_json(400, {"error": "شناسه مشتری معتبر نیست."})
                normalized_phone=phone.strip() if phone and phone.strip() else None
                db.execute("BEGIN IMMEDIATE")
                if normalized_phone and db.execute("SELECT 1 FROM customers WHERE tenant_id=? AND phone=?",
                                                   (user["tenant_id"],normalized_phone)).fetchone():
                    db.rollback()
                    return self.send_json(409, {"error": "این شماره تماس قبلاً برای مشتری دیگری ثبت شده است."})
                db.execute("""INSERT INTO customers(id,tenant_id,full_name,phone,email,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (customer_id,user["tenant_id"],name.strip(),normalized_phone,email.strip() if email else None,notes,now,now))
                audit(db,user["tenant_id"],user["id"],"customer.created","customer",customer_id)
                db.commit()
                return self.send_json(201, {"id": customer_id, "ok": True})
            if path == "/api/transactions":
                if not module_enabled(db,user["tenant_id"],"accounting"):
                    return self.send_json(403, {"error": "ماژول حسابداری در لایسنس فعال نیست."})
                if user["role"] not in ("owner", "manager", "cashier"):
                    return self.send_json(403, {"error": "دسترسی ثبت تراکنش ندارید."})
                title = data.get("title"); amount = data.get("amount"); kind = data.get("kind")
                category = data.get("category", "عمومی"); occurred_at = data.get("createdAt")
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160: return self.send_json(400, {"error": "عنوان تراکنش معتبر نیست."})
                if type(amount) is not int or amount <= 0 or amount > 9007199254740991: return self.send_json(400, {"error": "مبلغ باید عدد صحیح و مثبت باشد."})
                if kind not in ("income", "expense"): return self.send_json(400, {"error": "نوع تراکنش معتبر نیست."})
                if not isinstance(category, str) or len(category) > 80: return self.send_json(400, {"error": "دسته‌بندی معتبر نیست."})
                if not isinstance(occurred_at, str) or len(occurred_at) > 40: return self.send_json(400, {"error": "تاریخ تراکنش معتبر نیست."})
                from datetime import datetime
                try: datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                except ValueError: return self.send_json(400, {"error": "تاریخ تراکنش معتبر نیست."})
                tx_id = str(data.get("id") or secrets.token_urlsafe(18))
                if len(tx_id) > 100: return self.send_json(400, {"error": "شناسه تراکنش معتبر نیست."})
                if not 1<=len(tx_id)<=100: return self.send_json(400, {"error": "شناسه تراکنش معتبر نیست."})
                now = int(time.time())
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("SELECT title,amount,kind,category,occurred_at,status FROM transactions WHERE id=? AND tenant_id=?",
                                    (tx_id,user["tenant_id"])).fetchone()
                if existing:
                    if (existing["title"]==title.strip() and existing["amount"]==amount and existing["kind"]==kind and
                        existing["category"]==(category.strip() or "عمومی") and existing["occurred_at"]==occurred_at and
                        existing["status"]=="posted"):
                        journal_rows=db.execute("""SELECT a.code,l.debit,l.credit FROM journal_entries e
                            JOIN journal_lines l ON l.entry_id=e.id AND l.tenant_id=e.tenant_id
                            JOIN accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                            WHERE e.tenant_id=? AND e.source_type='transaction' AND e.source_id=?""",
                            (user["tenant_id"],tx_id)).fetchall()
                        expected_rows=(
                            [("1000",amount,0),("4000",0,amount)] if kind=="income"
                            else [("5000",amount,0),("1000",0,amount)]
                        )
                        actual_rows=sorted((row["code"],row["debit"],row["credit"]) for row in journal_rows)
                        if len(journal_rows)==2 and actual_rows==sorted(expected_rows):
                            db.commit()
                            return self.send_json(200,{"id":tx_id,"ok":True,"duplicate":True})
                        db.rollback()
                        return self.send_json(409,{"error":"سند حسابداری تراکنش ناقص یا نامتوازن است؛ تکرار عملیات پذیرفته نشد."})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه تراکنش قبلاً برای اطلاعات متفاوت یا برگشت‌خورده استفاده شده است."})
                db.execute("""INSERT INTO transactions(id,tenant_id,created_by,title,amount,kind,category,occurred_at,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (tx_id,user["tenant_id"],user["id"],title.strip(),amount,kind,category.strip() or "عمومی",occurred_at,now))
                post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title.strip(),amount,kind,occurred_at)
                audit(db,user["tenant_id"],user["id"],"transaction.created","transaction",tx_id,{"kind":kind,"amount":amount})
                db.commit()
                return self.send_json(201, {"id": tx_id, "ok": True})
            return self.send_json(404, {"error": "مسیر API پیدا نشد."})
        except sqlite3.IntegrityError:
            db.rollback()
            return self.send_json(409, {"error": "این شناسه قبلاً ثبت شده یا داده با محدودیت‌های پایگاه داده سازگار نیست."})
        except Exception:
            db.rollback()
            self.log_error("Unhandled API exception")
            return self.send_json(500, {"error": "خطای داخلی سرور رخ داد."})
        finally: db.close()

    def create_session(self, db, user_id):
        token = secrets.token_urlsafe(40)
        now = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at<=?",(now,))
        db.execute("""DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN
            (SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 9)""",
            (user_id,user_id))
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, now + 30 * 60, now))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self.send_json(200, {"token": token, "expiresAt": now + 30 * 60, "user": public_user(row)})

    def log_error(self, message, *args):
        rendered = message % args if args else message
        print(rendered, flush=True)

def backup_scheduler(stop_event=None):
    retry_delay = 60
    while stop_event is None or not stop_event.is_set():
        try:
            snapshot = create_snapshot(DB_PATH, BACKUP_DIR, BACKUP_RETENTION)
            print("MG scheduled backup created: %s" % snapshot, flush=True)
            retry_delay = 60
            delay = BACKUP_INTERVAL_SECONDS
        except Exception as exc:
            print("MG scheduled backup failed: %s" % exc, flush=True)
            delay = retry_delay
            retry_delay = min(retry_delay * 2, 3600)
        if stop_event is None:
            time.sleep(delay)
        elif stop_event.wait(delay):
            break


def main():
    connect().close()
    threading.Thread(target=backup_scheduler, name="mg-backup-scheduler", daemon=True).start()
    print("MG Business listening on http://%s:%s (database: %s)" % (HOST, PORT, DB_PATH), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
