#!/usr/bin/env python3
"""MG Business local-first HTTP application and SQLite API (stdlib only)."""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, sqlite3, time, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MG_DATA_DIR", Path.home() / ".mg-business-data")).resolve()
DB_PATH = Path(os.environ.get("MG_DB_PATH", DATA_DIR / "mg-business.sqlite3")).resolve()
HOST = os.environ.get("MG_HOST", "127.0.0.1")
PORT = int(os.environ.get("MG_PORT", "8080"))
SESSION_SECONDS = 60 * 60 * 24 * 7
PBKDF2_ROUNDS = 310_000
MAX_BODY = 1024 * 1024
LOGIN_FAILURES = {}
LOGIN_LOCK = threading.Lock()

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
CREATE TABLE IF NOT EXISTS customers(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 full_name TEXT NOT NULL CHECK(length(full_name) BETWEEN 1 AND 160),
 phone TEXT, email TEXT, notes TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(tenant_id,phone)
);
CREATE INDEX IF NOT EXISTS idx_customers_tenant_name ON customers(tenant_id,full_name);
CREATE TABLE IF NOT EXISTS audit_log(
 id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 actor_id TEXT REFERENCES users(id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT, details_json TEXT NOT NULL DEFAULT '{}',
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_date ON audit_log(tenant_id, created_at DESC);
"""

def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
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
        ("1000", "صندوق و بانک", "asset", "debit"),
        ("1200", "موجودی کالا", "asset", "debit"),
        ("2000", "حساب‌های پرداختنی", "liability", "credit"),
        ("3000", "سرمایه", "equity", "credit"),
        ("4000", "درآمد فروش", "revenue", "credit"),
        ("5000", "هزینه‌های عملیاتی", "expense", "debit"),
    ]
    for code, name, account_type, normal in defaults:
        db.execute("INSERT OR IGNORE INTO accounts(id,tenant_id,code,name,account_type,normal_balance) VALUES(?,?,?,?,?,?)",
                   (secrets.token_urlsafe(16),tenant_id,code,name,account_type,normal))

def post_transaction_journal(db, tenant_id, user_id, tx_id, title, amount, kind, occurred_at):
    accounts = {row["code"]: row["id"] for row in db.execute("SELECT id,code FROM accounts WHERE tenant_id=? AND active=1", (tenant_id,))}
    cash, counterpart = accounts["1000"], accounts["4000"] if kind == "income" else accounts["5000"]
    entry_id, now = secrets.token_urlsafe(18), int(time.time())
    db.execute("""INSERT INTO journal_entries(id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (entry_id,tenant_id,occurred_at[:10],title,"transaction",tx_id,user_id,now))
    lines = ((cash,amount,0),(counterpart,0,amount)) if kind == "income" else ((counterpart,amount,0),(cash,0,amount))
    for account_id, debit, credit in lines:
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

def public_user(row):
    return {"id": row["id"], "username": row["username"], "role": row["role"], "tenantId": row["tenant_id"]}

def audit(db, tenant_id, actor_id, action, entity_type, entity_id=None, details=None):
    db.execute("INSERT INTO audit_log(tenant_id,actor_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
               (tenant_id, actor_id, action, entity_type, entity_id, json.dumps(details or {}, ensure_ascii=False), int(time.time())))

class Handler(SimpleHTTPRequestHandler):
    server_version = "MGBusiness/0.1"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
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
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path == "/api/me":
                tenant = db.execute("SELECT id,name FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
                return self.send_json(200, {"user": public_user(user), "tenant": dict(tenant)})
            if path == "/api/transactions":
                rows = db.execute("""SELECT id,title,amount,kind,category,occurred_at AS createdAt
                    FROM transactions WHERE tenant_id=? ORDER BY created_at DESC LIMIT 5000""",
                    (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/products":
                rows = db.execute("""SELECT id,name,sku,category,sell_price AS sellPrice,cost_price AS costPrice,active
                    FROM products WHERE tenant_id=? ORDER BY name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/inventory":
                rows = db.execute("""SELECT p.id AS productId,p.name,p.sku,p.category,
                    COALESCE(SUM(m.quantity_delta),0) AS quantity,p.cost_price AS costPrice
                    FROM products p LEFT JOIN inventory_movements m
                    ON m.product_id=p.id AND m.tenant_id=p.tenant_id
                    WHERE p.tenant_id=? AND p.active=1
                    GROUP BY p.id ORDER BY p.name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/inventory/movements":
                rows = db.execute("""SELECT m.id,m.product_id AS productId,p.name AS productName,
                    m.movement_type AS movementType,m.quantity_delta AS quantityDelta,
                    m.unit_cost AS unitCost,m.note,m.created_at AS createdAt
                    FROM inventory_movements m JOIN products p ON p.id=m.product_id
                    WHERE m.tenant_id=? ORDER BY m.created_at DESC LIMIT 1000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            if path == "/api/customers":
                rows = db.execute("""SELECT id,full_name AS fullName,phone,email,notes,created_at AS createdAt
                    FROM customers WHERE tenant_id=? ORDER BY full_name LIMIT 5000""", (user["tenant_id"],)).fetchall()
                return self.send_json(200, {"items": [dict(row) for row in rows]})
            return self.send_json(404, {"error": "مسیر API پیدا نشد."})
        finally: db.close()

    def do_DELETE(self):
        path = self.route()
        db = connect()
        try:
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path.startswith("/api/products/"):
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
            tx_id = path.rsplit("/", 1)[-1]
            if user["role"] not in ("owner", "manager"):
                return self.send_json(403, {"error": "فقط مدیر می‌تواند تراکنش را حذف کند."})
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM transactions WHERE id=? AND tenant_id=?", (tx_id,user["tenant_id"])).fetchone()
            if not row:
                db.rollback()
                return self.send_json(404, {"error": "تراکنش پیدا نشد."})
            db.execute("DELETE FROM transactions WHERE id=? AND tenant_id=?", (tx_id,user["tenant_id"]))
            audit(db,user["tenant_id"],user["id"],"transaction.deleted","transaction",tx_id)
            db.commit()
            return self.send_json(200, {"ok": True})
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
            user = self.authenticate(db)
            if not user: return self.send_json(401, {"error": "برای ادامه وارد حساب شوید."})
            if path == "/api/logout":
                token = self.headers.get("Authorization", "")[7:].strip()
                db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                db.commit()
                return self.send_json(200, {"ok": True})
            if path == "/api/products":
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
            if path == "/api/inventory/movements":
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
                audit(db,user["tenant_id"],user["id"],"inventory.movement.created","inventory_movement",movement_id,
                      {"productId":product_id,"quantityDelta":delta,"movementType":movement_type})
                db.commit()
                return self.send_json(201, {"id": movement_id, "ok": True})
            if path == "/api/customers":
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
                if len(customer_id) > 100: return self.send_json(400, {"error": "شناسه مشتری معتبر نیست."})
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO customers(id,tenant_id,full_name,phone,email,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (customer_id,user["tenant_id"],name.strip(),phone.strip() if phone else None,email.strip() if email else None,notes,now,now))
                audit(db,user["tenant_id"],user["id"],"customer.created","customer",customer_id)
                db.commit()
                return self.send_json(201, {"id": customer_id, "ok": True})
            if path == "/api/transactions":
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

    def log_error(self, message):
        print(message, flush=True)

def main():
    connect().close()
    print("MG Business listening on http://%s:%s (database: %s)" % (HOST, PORT, DB_PATH), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
