#!/usr/bin/env python3
"""MG Business local-first HTTP application and SQLite API (stdlib only)."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, sqlite3, time, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).resolve()
DB_PATH = Path(os.environ.get("MG_DB_PATH", DATA_DIR / "mg-business.sqlite3")).resolve()
if DATA_DIR == ROOT or ROOT in DATA_DIR.parents or DB_PATH == ROOT or ROOT in DB_PATH.parents:
    raise RuntimeError("MG_DATA_DIR and MG_DB_PATH must be outside the public web root.")
HOST = os.environ.get("MG_HOST", "127.0.0.1")
PORT = int(os.environ.get("MG_PORT", "8080"))
SESSION_SECONDS = 60 * 60 * 24 * 7
PBKDF2_ROUNDS = 310_000
MAX_BODY = 50 * 1024 * 1024
MODULE_KEYS = ('cafe','gamenet','accounting','customers','inventory','reports','settings')
LOGIN_FAILURES = {}
LOGIN_LOCK = threading.Lock()
PUBLIC_ORDER_FAILURES = {}

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
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_sessions_tenant_start ON game_sessions(tenant_id,started_at DESC);
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
    db.commit()
    return db

def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), digest.hex())

def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
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
        return
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
        blocked = blocked or resolved.name in ("server.py","license_tool.py","README.md")
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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
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
        if length <= 0 or length > MAX_BODY: raise ValueError("حجم درخواست نامعتبر است.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError): raise ValueError("JSON درخواست معتبر نیست.")
        if not isinstance(value, dict): raise ValueError("بدنه درخواست باید یک شیء JSON باشد.")
        return value

    def route(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def authenticate(self, db):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "): return None
        token = header[7:].strip()
        if len(token) < 32: return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute("""SELECT u.*,s.expires_at FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (token_hash, int(time.time()))).fetchone()
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
            if path == "/api/setup-status":
                configured = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
                return self.send_json(200, {"configured": configured})
            if path == "/api/public/menu":
                tenant = db.execute("SELECT id,name FROM tenants ORDER BY created_at LIMIT 1").fetchone()
                if not tenant or not module_enabled(db,tenant["id"],"cafe"):
                    return self.send_json(404, {"error": "منوی آنلاین این کسب‌وکار فعال نیست."})
                rows = db.execute("""SELECT p.id,p.name,p.category,p.sell_price AS sellPrice,
                    COALESCE(SUM(m.quantity_delta),0) AS available
                    FROM products p LEFT JOIN inventory_movements m
                    ON m.product_id=p.id AND m.tenant_id=p.tenant_id
                    WHERE p.tenant_id=? AND p.active=1 GROUP BY p.id ORDER BY p.category,p.name""",
                    (tenant["id"],)).fetchall()
                return self.send_json(200, {"businessName":tenant["name"],"items":[dict(row) for row in rows if row["available"]>0]})
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
                    "customers": "SELECT * FROM customers WHERE tenant_id=?",
                    "customer_ledger": "SELECT * FROM customer_ledger WHERE tenant_id=?",
                    "inventory_movements": "SELECT * FROM inventory_movements WHERE tenant_id=?",
                    "suppliers": "SELECT * FROM suppliers WHERE tenant_id=?",
                    "purchase_invoices": "SELECT * FROM purchase_invoices WHERE tenant_id=?",
                    "purchase_invoice_items": "SELECT * FROM purchase_invoice_items WHERE tenant_id=?",
                    "supplier_payments": "SELECT * FROM supplier_payments WHERE tenant_id=?",
                    "orders": "SELECT * FROM orders WHERE tenant_id=?",
                    "order_items": "SELECT * FROM order_items WHERE tenant_id=?",
                    "game_devices": "SELECT * FROM game_devices WHERE tenant_id=?",
                    "game_sessions": "SELECT * FROM game_sessions WHERE tenant_id=?",
                    "gamenet_reservations": "SELECT * FROM gamenet_reservations WHERE tenant_id=?",
                    "audit_log": "SELECT * FROM audit_log WHERE tenant_id=?"
                }
                snapshot = {"schema":"mg-business-server-backup","version":1,
                            "exportedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                            "tenant":dict(db.execute("SELECT id,name,created_at FROM tenants WHERE id=?",(tenant_id,)).fetchone()),
                            "tables":{}}
                for name, query in tables.items():
                    snapshot["tables"][name] = [dict(row) for row in db.execute(query,(tenant_id,)).fetchall()]
                db.commit()
                return self.send_json(200, snapshot)
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
            if path == "/api/purchase-invoices":
                if not module_enabled(db,user["tenant_id"],"inventory"):
                    return self.send_json(403,{"error":"ماژول انبار فعال نیست."})
                rows=db.execute("""SELECT i.id,i.invoice_number AS invoiceNumber,i.invoice_date AS invoiceDate,i.subtotal,i.discount,i.total,
                    i.paid_amount AS paidAmount,i.status,s.id AS supplierId,s.name AS supplierName
                    FROM purchase_invoices i JOIN suppliers s ON s.id=i.supplier_id WHERE i.tenant_id=?
                    ORDER BY i.invoice_date DESC,i.created_at DESC LIMIT 2000""",(user["tenant_id"],)).fetchall()
                return self.send_json(200,{"items":[dict(r) for r in rows]})
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
                    gs.started_at AS startedAt,gs.players_count AS playersCount
                    FROM game_devices d LEFT JOIN game_sessions gs
                    ON gs.device_id=d.id AND gs.tenant_id=d.tenant_id AND gs.status='running'
                    LEFT JOIN customers c ON c.id=gs.customer_id AND c.tenant_id=gs.tenant_id
                    WHERE d.tenant_id=? ORDER BY d.name""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/gamenet/sessions":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                rows = db.execute("""SELECT s.id,s.device_id AS deviceId,d.name AS deviceName,
                    s.customer_id AS customerId,c.full_name AS customerName,s.started_at AS startedAt,
                    s.ended_at AS endedAt,s.status,s.players_count AS playersCount,
                    s.rate_per_hour AS ratePerHour,s.is_free AS isFree,s.amount_due AS amountDue
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
            if path == "/api/reports/sales-by-product":
                if not module_enabled(db,user["tenant_id"],"reports"):
                    return self.send_json(403, {"error": "ماژول گزارش‌ها در لایسنس فعال نیست."})
                rows = db.execute("""SELECT i.product_id AS productId,i.product_name AS productName,
                    SUM(i.quantity) AS quantity,SUM(i.line_total) AS revenue
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
                order = db.execute("SELECT * FROM orders WHERE id=? AND tenant_id=? AND status='paid'",
                                   (order_id,user["tenant_id"])).fetchone()
                if not order:
                    db.rollback()
                    return self.send_json(404, {"error": "سفارش پرداخت‌شده پیدا نشد."})
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
                db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND tenant_id=?", (order_id,user["tenant_id"]))
                audit(db,user["tenant_id"],user["id"],"order.cancelled","order",order_id)
                db.commit()
                return self.send_json(200, {"ok": True, "cancelled": True})
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
        finally: db.close()

    def do_POST(self):
        path = self.route()
        try: data = self.read_json()
        except ValueError as exc: return self.send_json(400, {"error": str(exc)})
        db = connect()
        try:
            if path == "/api/setup":
                tenant_name = str(data.get("businessName", "")).strip()
                username = str(data.get("username", "")).strip()
                password = data.get("password", "")
                if not 1 <= len(tenant_name) <= 120: return self.send_json(400, {"error": "نام کسب‌وکار معتبر نیست."})
                if not 3 <= len(username) <= 64: return self.send_json(400, {"error": "نام کاربری باید ۳ تا ۶۴ نویسه باشد."})
                if not isinstance(password, str) or len(password) < 12: return self.send_json(400, {"error": "رمز عبور باید حداقل ۱۲ نویسه باشد."})
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
                username, password = str(data.get("username", "")).strip(), data.get("password", "")
                key = (self.client_address[0], username.casefold())
                now = time.time()
                with LOGIN_LOCK:
                    attempts = [stamp for stamp in LOGIN_FAILURES.get(key, []) if now - stamp < 900]
                    LOGIN_FAILURES[key] = attempts
                    if len(attempts) >= 8:
                        return self.send_json(429, {"error": "تلاش‌های ورود بیش از حد است؛ ۱۵ دقیقه بعد دوباره امتحان کنید."})
                user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
                if not user or not isinstance(password, str) or not verify_password(password, user["password_hash"]):
                    with LOGIN_LOCK:
                        LOGIN_FAILURES.setdefault(key, []).append(now)
                    return self.send_json(401, {"error": "نام کاربری یا رمز عبور اشتباه است."})
                with LOGIN_LOCK:
                    LOGIN_FAILURES.pop(key, None)
                return self.create_session(db, user["id"])
            if path == "/api/public/orders":
                ip = self.client_address[0]
                now = time.time()
                with LOGIN_LOCK:
                    attempts = [stamp for stamp in PUBLIC_ORDER_FAILURES.get(ip,[]) if now-stamp < 900]
                    if len(attempts) >= 5:
                        return self.send_json(429, {"error": "تعداد سفارش‌های آنلاین بیش از حد است؛ بعداً دوباره تلاش کنید."})
                    attempts.append(now)
                    PUBLIC_ORDER_FAILURES[ip] = attempts
                name, phone = data.get("customerName"), data.get("phone")
                items = data.get("items")
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 160:
                    return self.send_json(400, {"error": "نام مشتری معتبر نیست."})
                if not isinstance(phone,str) or not 5 <= len(phone.strip()) <= 32:
                    return self.send_json(400, {"error": "شماره تماس معتبر نیست."})
                if not isinstance(items,list) or not 1 <= len(items) <= 30:
                    return self.send_json(400, {"error": "سفارش باید حداقل یک قلم و حداکثر ۳۰ قلم داشته باشد."})
                normalized = {}
                for line in items:
                    if not isinstance(line,dict) or not isinstance(line.get("productId"),str) or type(line.get("quantity")) is not int or not 1 <= line["quantity"] <= 30:
                        return self.send_json(400, {"error": "قلم سفارش معتبر نیست."})
                    normalized[line["productId"]] = normalized.get(line["productId"],0)+line["quantity"]
                tenant = db.execute("SELECT id FROM tenants ORDER BY created_at LIMIT 1").fetchone()
                if not tenant or not module_enabled(db,tenant["id"],"cafe"):
                    return self.send_json(404, {"error": "ثبت سفارش آنلاین فعال نیست."})
                tenant_id = tenant["id"]
                client_order_id = data.get("clientOrderId")
                if client_order_id is not None and (not isinstance(client_order_id,str) or not 8 <= len(client_order_id) <= 100):
                    return self.send_json(400, {"error": "شناسه سفارش آنلاین معتبر نیست."})
                if client_order_id:
                    existing_order = db.execute("SELECT id,order_number AS orderNumber,total FROM orders WHERE id=? AND tenant_id=?",
                                                 ("public-"+client_order_id,tenant_id)).fetchone()
                    if existing_order:
                        return self.send_json(200, {"ok":True,"orderNumber":existing_order["orderNumber"],"total":existing_order["total"],"duplicate":True})
                actor = db.execute("SELECT id FROM users WHERE tenant_id=? AND active=1 ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END LIMIT 1",
                                   (tenant_id,)).fetchone()
                if not actor:
                    return self.send_json(503, {"error": "پذیرش سفارش در حال حاضر ممکن نیست."})
                db.execute("BEGIN IMMEDIATE")
                # Recheck idempotency under the write lock to serialize concurrent retries.
                if client_order_id:
                    existing_order = db.execute("SELECT id,order_number AS orderNumber,total FROM orders WHERE id=? AND tenant_id=?",
                                                 ("public-"+client_order_id,tenant_id)).fetchone()
                    if existing_order:
                        db.rollback()
                        return self.send_json(200, {"ok":True,"orderNumber":existing_order["orderNumber"],"total":existing_order["total"],"duplicate":True})
                customer = db.execute("SELECT id FROM customers WHERE tenant_id=? AND phone=?",
                                      (tenant_id,phone.strip())).fetchone()
                customer_id = customer["id"] if customer else secrets.token_urlsafe(18)
                if not customer:
                    db.execute("""INSERT INTO customers(id,tenant_id,full_name,phone,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)""", (customer_id,tenant_id,name.strip(),phone.strip(),int(now),int(now)))
                products, subtotal, cogs_total = {}, 0, 0
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
                    line_total = product["sell_price"]*quantity
                    line_cost = average_cost*quantity
                    if subtotal+line_total > 9007199254740991 or cogs_total+line_cost > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "مبلغ سفارش بیش از حد مجاز است."})
                    subtotal += line_total
                    cogs_total += line_cost
                    products[product_id]=(product,quantity,line_total,average_cost)
                if subtotal <= 0:
                    db.rollback()
                    return self.send_json(400, {"error": "مبلغ سفارش معتبر نیست."})
                order_id, created = ("public-"+client_order_id) if client_order_id else secrets.token_urlsafe(18), int(now)
                order_number = "MG-ON-%s-%s" % (time.strftime("%Y%m%d"),secrets.token_hex(3).upper())
                db.execute("""INSERT INTO orders(id,tenant_id,order_number,customer_id,created_by,status,subtotal,discount,total,payment_method,created_at)
                    VALUES(?,?,?,?,?,'paid',?,0,?,'credit',?)""", (order_id,tenant_id,order_number,customer_id,actor["id"],subtotal,subtotal,created))
                db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                    VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),tenant_id,customer_id,actor["id"],"order",order_id,subtotal,"سفارش آنلاین "+order_number,created))
                for product_id,(product,quantity,line_total,average_cost) in products.items():
                    db.execute("""INSERT INTO order_items(id,tenant_id,order_id,product_id,product_name,quantity,unit_price,unit_cost,line_total)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (secrets.token_urlsafe(16),tenant_id,order_id,product_id,product["name"],quantity,product["sell_price"],average_cost,line_total))
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
                audit(db,tenant_id,actor["id"],"online_order.created","order",order_id,{"total":subtotal,"items":len(products)})
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
                allowed = ("users","accounts","products","customers","customer_ledger","game_devices","transactions","journal_entries",
                           "journal_lines","inventory_movements","suppliers","purchase_invoices","purchase_invoice_items","supplier_payments",
                           "orders","order_items","game_sessions","gamenet_reservations","audit_log")
                if any(name not in allowed or not isinstance(tables.get(name,[]),list) for name in tables):
                    return self.send_json(400, {"error": "ساختار جداول نسخه پشتیبان معتبر نیست."})
                total_rows = sum(len(tables.get(name,[])) for name in allowed)
                if total_rows > 500000:
                    return self.send_json(413, {"error": "تعداد رکوردهای نسخه پشتیبان بیش از حد مجاز است."})
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
                    delete_order = ("gamenet_reservations","game_sessions","order_items","orders","supplier_payments","purchase_invoice_items","purchase_invoices",
                                    "inventory_movements","journal_lines","journal_entries","transactions","customer_ledger",
                                    "products","customers","suppliers","game_devices","accounts","audit_log")
                    for name in delete_order:
                        db.execute("DELETE FROM "+name+" WHERE tenant_id=?", (tenant_id,))
                    insert_order = ("accounts","products","customers","customer_ledger","suppliers","game_devices","transactions",
                                    "journal_entries","journal_lines","inventory_movements","purchase_invoices","purchase_invoice_items",
                                    "supplier_payments","orders","order_items","game_sessions","gamenet_reservations","audit_log")
                    for name in insert_order:
                        records = tables.get(name,[])
                        if name == "journal_entries":
                            records = sorted(records,key=lambda row: row.get("created_at",0))
                        for record in records:
                            columns = list(record.keys())
                            if not columns:
                                continue
                            placeholders = ",".join("?" for _ in columns)
                            sql = "INSERT INTO "+name+"("+",".join(columns)+") VALUES("+placeholders+")"
                            db.execute(sql,tuple(record[column] for column in columns))
                    db.execute("""CREATE TRIGGER journal_entries_no_delete BEFORE DELETE ON journal_entries
                        BEGIN SELECT RAISE(ABORT,'journal entries are immutable'); END""")
                    db.execute("""CREATE TRIGGER journal_lines_no_delete BEFORE DELETE ON journal_lines
                        BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
                    audit(db,tenant_id,user["id"],"backup.restored","tenant",tenant_id,{"rows":total_rows})
                    db.commit()
                    return self.send_json(200, {"ok":True,"restoredRows":total_rows})
                except sqlite3.IntegrityError:
                    db.rollback()
                    return self.send_json(409, {"error": "بازیابی به‌دلیل ناسازگاری ارتباط رکوردها انجام نشد؛ اطلاعات قبلی حفظ شد."})
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
            if path == "/api/logout":
                token = self.headers.get("Authorization", "")[7:].strip()
                db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                db.commit()
                return self.send_json(200, {"ok": True})
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
                if not isinstance(device_id,str) or not isinstance(starts,str) or not isinstance(ends,str):
                    return self.send_json(400,{"error":"دستگاه و زمان شروع و پایان رزرو الزامی است."})
                if type(players) is not int or not 1<=players<=16 or not isinstance(notes,str) or len(notes)>500:
                    return self.send_json(400,{"error":"تعداد بازیکنان یا یادداشت رزرو معتبر نیست."})
                from datetime import datetime
                try:
                    start_dt=datetime.fromisoformat(starts); end_dt=datetime.fromisoformat(ends)
                    start_ts=int(start_dt.timestamp()); end_ts=int(end_dt.timestamp())
                except (TypeError,ValueError,OverflowError):
                    return self.send_json(400,{"error":"زمان رزرو معتبر نیست."})
                now=int(time.time())
                if start_ts<now-60 or end_ts-start_ts<15*60 or end_ts-start_ts>24*60*60:
                    return self.send_json(400,{"error":"رزرو باید از ۱۵ دقیقه تا ۲۴ ساعت باشد و از بیش از یک دقیقه قبل شروع نشده باشد."})
                db.execute("BEGIN IMMEDIATE")
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
                reservation_id=str(data.get("id") or secrets.token_urlsafe(18)); now=int(time.time())
                if not 1<=len(reservation_id)<=100:
                    db.rollback(); return self.send_json(400,{"error":"شناسه رزرو معتبر نیست."})
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
                changed=db.execute("""UPDATE gamenet_reservations SET status='cancelled'
                    WHERE id=? AND tenant_id=? AND status='booked'""",(reservation_id,user["tenant_id"])).rowcount
                if not changed:
                    db.rollback(); return self.send_json(404,{"error":"رزرو فعال پیدا نشد."})
                audit(db,user["tenant_id"],user["id"],"gamenet.reservation.cancelled","gamenet_reservation",reservation_id)
                db.commit(); return self.send_json(200,{"ok":True,"status":"cancelled"})
            if path == "/api/gamenet/sessions":
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی شروع نشست بازی ندارید."})
                device_id, customer_id = data.get("deviceId"), data.get("customerId")
                players = data.get("playersCount",1)
                if not isinstance(device_id,str) or type(players) is not int or not 1 <= players <= 16:
                    return self.send_json(400, {"error": "دستگاه یا تعداد بازیکنان معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
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
                if active_reservation and active_reservation["customer_id"]!=customer_id:
                    db.rollback(); return self.send_json(409,{"error":"این دستگاه در حال حاضر برای مشتری دیگری رزرو شده است."})
                if active_reservation:
                    db.execute("UPDATE gamenet_reservations SET status='completed' WHERE id=?",(active_reservation["id"],))
                session_id = str(data.get("id") or secrets.token_urlsafe(18))
                db.execute("""INSERT INTO game_sessions(id,tenant_id,device_id,customer_id,started_by,started_at,status,
                    players_count,rate_per_hour,is_free,created_at) VALUES(?,?,?,?,?,?,'running',?,?,?,?)""",
                    (session_id,user["tenant_id"],device_id,customer_id,user["id"],now,players,device["hourly_rate"],device["is_free"],now))
                audit(db,user["tenant_id"],user["id"],"gamenet.session.started","game_session",session_id,
                      {"deviceId":device_id,"playersCount":players})
                db.commit()
                return self.send_json(201, {"id":session_id,"startedAt":now,"ok":True})
            if path.startswith("/api/gamenet/sessions/") and path.endswith("/end"):
                if not module_enabled(db,user["tenant_id"],"gamenet"):
                    return self.send_json(403, {"error": "ماژول گیم‌نت در لایسنس فعال نیست."})
                session_id = path.split("/")[-2]
                if user["role"] not in ("owner","manager","cashier"):
                    return self.send_json(403, {"error": "دسترسی پایان نشست بازی ندارید."})
                db.execute("BEGIN IMMEDIATE")
                session = db.execute("SELECT * FROM game_sessions WHERE id=? AND tenant_id=? AND status='running'",
                                     (session_id,user["tenant_id"])).fetchone()
                if not session:
                    db.rollback()
                    return self.send_json(404, {"error": "نشست فعال پیدا نشد."})
                now = int(time.time())
                elapsed = max(0,now-session["started_at"])
                billed_minutes = (elapsed+59)//60
                amount = 0 if session["is_free"] else (session["rate_per_hour"]*billed_minutes+59)//60
                db.execute("UPDATE game_sessions SET status='ended',ended_at=?,amount_due=? WHERE id=? AND tenant_id=?",
                           (now,amount,session_id,user["tenant_id"]))
                if amount > 0:
                    tx_id = "game-"+session_id
                    title = "هزینه بازی "+session_id
                    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(now))
                    db.execute("""INSERT INTO transactions(id,tenant_id,created_by,title,amount,kind,category,occurred_at,created_at)
                        VALUES(?,?,?,?,?,'income','گیم‌نت',?,?)""", (tx_id,user["tenant_id"],user["id"],title,amount,occurred_at,now))
                    if session["customer_id"]:
                        db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                            VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],session["customer_id"],user["id"],
                            "game_session",session_id,amount,"هزینه بازی "+session_id,now))
                        post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title,amount,"income",occurred_at,"1100")
                    else:
                        post_transaction_journal(db,user["tenant_id"],user["id"],tx_id,title,amount,"income",occurred_at)
                audit(db,user["tenant_id"],user["id"],"gamenet.session.ended","game_session",session_id,
                      {"amount":amount,"elapsedSeconds":elapsed})
                db.commit()
                return self.send_json(200, {"ok":True,"amountDue":amount,"elapsedSeconds":elapsed})
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
                for line in items:
                    if not isinstance(line,dict): return self.send_json(400, {"error": "قلم سفارش معتبر نیست."})
                    product_id, quantity = line.get("productId"), line.get("quantity")
                    if not isinstance(product_id,str) or type(quantity) is not int or not 1 <= quantity <= 100000:
                        return self.send_json(400, {"error": "کالا یا تعداد سفارش معتبر نیست."})
                    normalized[product_id] = normalized.get(product_id,0) + quantity
                order_id=data.get("id") or secrets.token_urlsafe(18)
                if not isinstance(order_id,str) or not 1<=len(order_id)<=100:
                    return self.send_json(400,{"error":"شناسه سفارش معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                existing=db.execute("""SELECT id,order_number,customer_id,discount,total,payment_method
                    FROM orders WHERE id=? AND tenant_id=?""",(order_id,user["tenant_id"])).fetchone()
                if existing:
                    old_items={row["product_id"]:row["quantity"] for row in db.execute(
                        "SELECT product_id,quantity FROM order_items WHERE tenant_id=? AND order_id=?",
                        (user["tenant_id"],order_id)).fetchall()}
                    if (old_items==normalized and existing["customer_id"]==customer_id and
                        existing["discount"]==discount and existing["payment_method"]==payment):
                        db.commit()
                        return self.send_json(200,{"id":existing["id"],"orderNumber":existing["order_number"],
                            "total":existing["total"],"duplicate":True})
                    db.rollback()
                    return self.send_json(409,{"error":"شناسه سفارش قبلاً برای اطلاعات متفاوتی استفاده شده است."})
                if customer_id and not db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?", (customer_id,user["tenant_id"])).fetchone():
                    db.rollback()
                    return self.send_json(404, {"error": "مشتری انتخاب‌شده پیدا نشد."})
                if payment == "credit" and not customer_id:
                    db.rollback()
                    return self.send_json(400, {"error": "برای فروش اعتباری باید مشتری مشخص شود."})
                products, subtotal, cogs_total = {}, 0, 0
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
                    line_total = product["sell_price"] * quantity
                    if line_total > 9007199254740991 or subtotal + line_total > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "مبلغ سفارش بیش از حد مجاز است."})
                    average_cost = max(0, stock_value // stock) if stock else product["cost_price"]
                    line_cost = average_cost * quantity
                    if line_cost > 9007199254740991 or cogs_total + line_cost > 9007199254740991:
                        db.rollback()
                        return self.send_json(400, {"error": "بهای تمام‌شده سفارش بیش از حد مجاز است."})
                    subtotal += line_total
                    cogs_total += line_cost
                    products[product_id] = (product,quantity,line_total,average_cost)
                if discount >= subtotal:
                    db.rollback()
                    return self.send_json(400, {"error": "تخفیف باید از مبلغ سفارش کمتر باشد."})
                total = subtotal - discount
                now = int(time.time())
                order_number = "MG-%s-%s" % (time.strftime("%Y%m%d"), secrets.token_hex(3).upper())
                db.execute("""INSERT INTO orders(id,tenant_id,order_number,customer_id,created_by,status,subtotal,discount,total,payment_method,created_at)
                    VALUES(?,?,?,?,?,'paid',?,?,?,?,?)""", (order_id,user["tenant_id"],order_number,customer_id,user["id"],subtotal,discount,total,payment,now))
                if payment == "credit":
                    db.execute("""INSERT INTO customer_ledger(id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,note,created_at)
                        VALUES(?,?,?,?,?,?,?,0,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],customer_id,user["id"],
                        "order",order_id,total,"فروش اعتباری "+order_number,now))
                for product_id,(product,quantity,line_total,average_cost) in products.items():
                    db.execute("""INSERT INTO order_items(id,tenant_id,order_id,product_id,product_name,quantity,unit_price,unit_cost,line_total)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (secrets.token_urlsafe(16),user["tenant_id"],order_id,product_id,product["name"],quantity,product["sell_price"],average_cost,line_total))
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
                audit(db,user["tenant_id"],user["id"],"order.created","order",order_id,{"total":total,"items":len(products)})
                db.commit()
                return self.send_json(201, {"id":order_id,"orderNumber":order_number,"subtotal":subtotal,"discount":discount,"total":total})
            if path == "/api/users":
                if user["role"] != "owner":
                    return self.send_json(403, {"error": "فقط مالک می‌تواند کاربر جدید بسازد."})
                username, password, role = data.get("username"), data.get("password"), data.get("role")
                if not isinstance(username,str) or not 3 <= len(username.strip()) <= 64:
                    return self.send_json(400, {"error": "نام کاربری باید ۳ تا ۶۴ نویسه باشد."})
                if not isinstance(password,str) or len(password) < 12:
                    return self.send_json(400, {"error": "رمز عبور باید حداقل ۱۲ نویسه باشد."})
                if role not in ("manager","cashier","viewer"):
                    return self.send_json(400, {"error": "نقش کاربر معتبر نیست."})
                target_id, now = secrets.token_urlsafe(18), int(time.time())
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                           (target_id,user["tenant_id"],username.strip(),password_hash(password),role,now))
                audit(db,user["tenant_id"],user["id"],"user.created","user",target_id,{"role":role})
                db.commit()
                return self.send_json(201, {"id": target_id, "ok": True})
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
                if len(product_id) > 100: return self.send_json(400, {"error": "شناسه کالا معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO products(id,tenant_id,name,sku,category,sell_price,cost_price,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (product_id,user["tenant_id"],name.strip(),sku.strip() if sku else None,category.strip() or "عمومی",sell_price,cost_price,now,now))
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
                if db.execute("SELECT 1 FROM purchase_invoices WHERE tenant_id=? AND invoice_number=?",
                              (user["tenant_id"],number.strip())).fetchone():
                    db.rollback(); return self.send_json(409,{"error":"این شماره فاکتور خرید قبلاً ثبت شده است."})
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
                total_units=sum(row[1] for row in prepared)
                if subtotal<=0 or discount>=subtotal or discount%total_units:
                    db.rollback(); return self.send_json(400,{"error":"تخفیف باید کمتر از مبلغ فاکتور و قابل تقسیم بر تعداد کل واحدها باشد."})
                discount_per_unit=discount//total_units
                if any(row[2]<discount_per_unit for row in prepared):
                    db.rollback(); return self.send_json(400,{"error":"تخفیف واحد نمی‌تواند از بهای خرید هیچ کالا بیشتر باشد."})
                prepared=[(product,qty,cost-discount_per_unit,qty*(cost-discount_per_unit)) for product,qty,cost,line in prepared]
                total=subtotal-discount
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
                if type(amount) is not int or amount<=0 or method not in ("cash","card","online"): return self.send_json(400,{"error":"مبلغ یا روش پرداخت معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                inv=db.execute("SELECT * FROM purchase_invoices WHERE id=? AND tenant_id=?",(iid,user["tenant_id"])).fetchone()
                if not inv: db.rollback(); return self.send_json(404,{"error":"فاکتور خرید پیدا نشد."})
                remaining=inv["total"]-inv["paid_amount"]
                if amount>remaining: db.rollback(); return self.send_json(409,{"error":"پرداخت از مانده فاکتور بیشتر است."})
                payid=secrets.token_urlsafe(18); now=int(time.time())
                db.execute("INSERT INTO supplier_payments(id,tenant_id,invoice_id,supplier_id,created_by,payment_method,amount,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (payid,user["tenant_id"],iid,inv["supplier_id"],user["id"],method,amount,now))
                paid=inv["paid_amount"]+amount; status="paid" if paid==inv["total"] else "partial"
                db.execute("UPDATE purchase_invoices SET paid_amount=?,status=? WHERE id=?",(paid,status,iid))
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
                if type(unit_cost) is not int or unit_cost < 0:
                    return self.send_json(400, {"error": "بهای واحد معتبر نیست."})
                if not isinstance(note,str) or len(note) > 300:
                    return self.send_json(400, {"error": "یادداشت بیش از حد طولانی است."})
                db.execute("BEGIN IMMEDIATE")
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
                movement_id, now = str(data.get("id") or secrets.token_urlsafe(18)), int(time.time())
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
                if type(amount) is not int or amount <= 0 or amount > 9007199254740991 or payment_method not in ("cash","card","online"):
                    return self.send_json(400, {"error": "مبلغ یا روش دریافت معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                customer = db.execute("SELECT id FROM customers WHERE id=? AND tenant_id=?", (customer_id,user["tenant_id"])).fetchone()
                if not customer:
                    db.rollback()
                    return self.send_json(404, {"error": "مشتری پیدا نشد."})
                balance = db.execute("SELECT COALESCE(SUM(debit-credit),0) FROM customer_ledger WHERE tenant_id=? AND customer_id=?",
                                     (user["tenant_id"],customer_id)).fetchone()[0]
                if amount > balance:
                    db.rollback()
                    return self.send_json(409, {"error": "مبلغ دریافت از مانده بدهی مشتری بیشتر است."})
                payment_id, now = secrets.token_urlsafe(18), int(time.time())
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
                now = int(time.time())
                db.execute("BEGIN IMMEDIATE")
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
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, now + SESSION_SECONDS, now))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self.send_json(200, {"token": token, "expiresAt": now + SESSION_SECONDS, "user": public_user(row)})

    def log_error(self, message, *args):
        rendered = message % args if args else message
        print(rendered, flush=True)

def main():
    connect().close()
    print("MG Business listening on http://%s:%s (database: %s)" % (HOST, PORT, DB_PATH), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
