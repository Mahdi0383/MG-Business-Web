from html.parser import HTMLParser
from pathlib import Path
import re
root = Path(__file__).resolve().parents[1]
class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
    def handle_starttag(self, tag, attrs):
        value = dict(attrs).get("id")
        if value:
            if value in self.ids:
                raise SystemExit(f"duplicate HTML id: {value}")
            self.ids.add(value)
parser = IdParser()
html = (root / "index.html").read_text(encoding="utf-8")
parser.feed(html)
js = (root / "app.js").read_text(encoding="utf-8")
referenced = set(re.findall(r"\$\(['\"]([A-Za-z][A-Za-z0-9_-]*)['\"]\)", js))
dynamic = {"moduleAdd", "authForm", "ledgerSearch", "cafeCart", "cartSubtotal", "submitOrder", "cafeProduct", "cafeQuantity", "purchaseLines", "customerLedgerPanel", "supplierLedgerPanel", "cafeAddonChoices", "orderDetailsPanel", "gameSegmentHistoryPanel", "customerLoyaltyPanel"}
missing = sorted(referenced - parser.ids - dynamic)
if missing:
    raise SystemExit("missing DOM ids: " + ", ".join(missing))
for asset in ("styles.css", "app.js", "core.js", "server.py", "license_tool.py", "backup_manager.py", "manifest.webmanifest", "service-worker.js", "icon.svg", "order.html", "public-order.js", "tests/test_server.py", "tests/test_backup_manager.py"):
    if not (root / asset).is_file():
        raise SystemExit(f"missing required asset: {asset}")
if "./core.js" not in html:
    raise SystemExit("core.js must load before app.js")
if "importData" not in parser.ids or "importFile" not in parser.ids:
    raise SystemExit("backup restore controls are missing")
worker = (root / "service-worker.js").read_text(encoding="utf-8")
if "url.pathname.startsWith('/api/')" not in worker:
    raise SystemExit("service worker must never cache API responses")
if "confirmOverlay" not in parser.ids or "toastRegion" not in parser.ids:
    raise SystemExit("branded feedback dialogs are missing")
if "window.alert=showToast" not in js or "confirm(" in js:
    raise SystemExit("native dialogs must be replaced by branded feedback UI")
server = (root / "server.py").read_text(encoding="utf-8")
if "data-game-segments" not in js or "/api/gamenet/sessions/" not in js:
    raise SystemExit("GameNet billing segment history UI is missing")
if "game-payment-method" not in js or "paymentMethod" not in server:
    raise SystemExit("GameNet settlement payment-method UI/API is missing")
if "allocate_proportional_discount" not in server or "discount_amount" not in server:
    raise SystemExit("Order discounts must be allocated to product lines for net sales reports")
if "SUM(i.line_total-i.discount_amount)" not in server:
    raise SystemExit("Product sales reports must use net line revenue")
if "def log_error(self, message, *args):" not in server:
    raise SystemExit("HTTP handler must safely format standard-library error log arguments")
if 'resolved.suffix.lower() in (' not in server or '"deploy", ".github"' not in server or '".py", ".md", ".yml"' not in server:
    raise SystemExit("private backend, docs, tests, and deployment assets must not be statically served")
if "def translate_path(self, path):" not in server or "def list_directory(self, path):" not in server:
    raise SystemExit("static server must block private files and directory listing")
if "moduleAccess?.has('accounting')?await apiRequest('/api/transactions'):{items:[]}" not in js:
    raise SystemExit("backup restore must not require the accounting module to refresh the workspace")

if "def verify_license_code(code):" not in server or 'if path == "/api/backup/restore":' not in server:
    raise SystemExit("license verification or backup restoration endpoint is missing")
get_handler = server.split("    def do_GET(self):", 1)[1].split("    def do_DELETE(self):", 1)[0]
post_handler = server.split("    def do_POST(self):", 1)[1].split("    def create_session", 1)[0]
if re.search(r"\bdata(?:\.get|\[)", get_handler):
    raise SystemExit("GET handler must not read mutation request data")
if 'if path.startswith("/api/gamenet/devices/")' not in post_handler:
    raise SystemExit("game device updates must be routed through POST")
if 'if path == "/api/suppliers":' not in server or 'path.endswith("/ledger")' not in server or 'if path == "/api/purchase-invoices":' not in server or 'path.endswith("/payments")' not in server:
    raise SystemExit("supplier and purchase invoice workflows are missing")
if any(name not in server for name in ("purchase_invoice_items","supplier_payments","suppliers")):
    raise SystemExit("purchase records must be included in backup support")
app = (root / "app.js").read_text(encoding="utf-8")
if "purchaseLines" not in app or "supplierPaymentForm" not in app:
    raise SystemExit("multi-line purchase invoice and supplier payment UI are missing")
if "supplierLedgerPanel" not in app or "supplierStatementForm" not in app or "data-supplier-ledger-close" not in app:
    raise SystemExit("Supplier account statement UI is missing")
if "function persist(){if(apiMode)return true;" not in app:
    raise SystemExit("Server-backed financial records must not be written to browser local storage.")
if "apiMode=true;localStorage.removeItem(key);" not in app:
    raise SystemExit("Legacy browser financial cache must be cleared after server authentication.")

public_order = (root / "public-order.js").read_text(encoding="utf-8")
if "data-public-addon" not in public_order or "addons:x.addons.map" not in public_order:
    raise SystemExit("Public QR ordering must support optional product add-ons")
if "publicOrderFingerprint" not in public_order or "mg-public-order-pending" not in public_order:
    raise SystemExit("Public QR order retry keys must survive reload without storing customer details")
if "startsDate.toISOString()" not in app or "start_dt.utcoffset() is None" not in server:
    raise SystemExit("Game reservations must use explicit timezone-aware timestamps")
if '"duplicate":True' not in server or 'if order["status"]=="cancelled"' not in server:
    raise SystemExit("Cafe order reversal must be idempotent")
if 'if path == "/api/ready":' not in server or "PRAGMA quick_check" not in server or "/api/ready" not in (root / "tests/test_server.py").read_text(encoding="utf-8"):
    raise SystemExit("Database readiness checks are missing")
if "undistributed=discount-sum(allocations)" not in server or '"discount":30001' not in (root / "tests/test_server.py").read_text(encoding="utf-8"):
    raise SystemExit("Purchase invoice discounts must be allocated proportionally without rounding loss")
if '"duplicate":True' not in server or "reservation-cancel" not in (root / "tests/test_server.py").read_text(encoding="utf-8"):
    raise SystemExit("Game reservation cancellation must be retry-safe")
if "def public_order_duplicate" not in server or 'item["addons"]=addons_by_product.get' not in server:
    raise SystemExit("Public order add-on snapshots and retry validation are missing")
if "def public_order_fingerprint" not in server or "request_fingerprint" not in server or "version=13" not in server:
    raise SystemExit("Public order retries must use a persisted canonical request fingerprint")
if "data-supplier-ledger-export" not in app or "text/csv;charset=utf-8" not in app:
    raise SystemExit("Supplier account statement CSV export is missing")
if "const safe=" not in app or "[=+@-]" not in app:
    raise SystemExit("Supplier CSV export must neutralize spreadsheet formula injection")
if "result.totalPurchases" not in app or "item.balanceAfter" not in app:
    raise SystemExit("Supplier statement export must include totals and running balances")
if 'if path == "/api/gamenet/reservations":' not in server or "gamenet_reservations" not in server:
    raise SystemExit("GameNet reservation API and backup support are missing")
if 'if path == "/api/product-addons":' not in server or "addons_json" not in server or 'path.startswith("/api/orders/")' not in server:
    raise SystemExit("Product add-ons and detailed order receipt API are missing")
if "data-order-details" not in app or "data-order-print" not in app:
    raise SystemExit("Cafe order detail and print receipt UI are missing")
if "size:58mm 200mm" not in (root / "styles.css").read_text(encoding="utf-8"):
    raise SystemExit("Thermal receipt print layout must support 58mm printers")
if "productAddonForm" not in app or "data-cafe-addon" not in app or "addons:(x.addons||[]).map" not in app:
    raise SystemExit("Product add-on management and POS selection UI are missing")
if "stableRequestId" not in app:
    raise SystemExit("Idempotency keys must remain stable for retryable forms")
if "offline-card" not in app or "همگام‌سازی آفلاین هنوز فعال نیست" not in app:
    raise SystemExit("Offline writes must be blocked clearly when the API is unavailable")
if "audit.items" not in app or "/api/audit-log" not in app:
    raise SystemExit("Audit history UI is missing")
if "(['owner','manager'].includes(apiUser?.role)?apiRequest('/api/users'):Promise.resolve({items:[]}))" not in app:
    raise SystemExit("Non-admin users must be able to access their own account settings.")

if "userPasswordForm" not in app or "/password" not in app:
    raise SystemExit("User password reset UI is missing")
if "userRoleForm" not in app or "data-user-change-role" not in app or "'/role'" not in app:
    raise SystemExit("Owner user-role editing UI is missing")
if ".userRoleForm" not in (root / "styles.css").read_text(encoding="utf-8"):
    raise SystemExit("User role editor responsive styles are missing")
if 'path.endswith("/role")' not in server or '"user.role.changed"' not in server:
    raise SystemExit("Server-side user-role change enforcement is missing")
if any(fragment in app for fragment in (
    "value=\"'+d.hourlyRate+'\"",
    "value=\"'+d.playersCount+'\"",
    "value=\"'+program.spendAmount+'\"",
    "value=\"'+program.pointsAwarded+'\"",
    "value=\"'+x.price+'\"",
    "max=\"'+(x.total-x.paidAmount)+'\"",
)):
    raise SystemExit("Dynamic values in generated form attributes must be HTML-escaped")
if any(fragment not in app for fragment in (
    "value=\"'+escapeHtml(d.hourlyRate)+'\"",
    "value=\"'+escapeHtml(d.playersCount)+'\"",
    "value=\"'+escapeHtml(program.spendAmount)+'\"",
    "value=\"'+escapeHtml(program.pointsAwarded)+'\"",
    "value=\"'+escapeHtml(x.price)+'\"",
    "max=\"'+escapeHtml(x.total-x.paidAmount)+'\"",
)):
    raise SystemExit("Generated form attributes are missing contextual HTML escaping")
if "customerLedgerPanel" not in app or "data-customer-ledger" not in app:
    raise SystemExit("Customer account statement UI is missing")
if "customerLoyaltyPanel" not in app or "data-customer-loyalty" not in app or "/api/loyalty/program" not in app:
    raise SystemExit("Customer loyalty club UI is missing")
if "customer_loyalty_ledger" not in server or "/loyalty" not in server:
    raise SystemExit("Customer loyalty points ledger API is missing")
if "reservationForm" not in app or "data-reservation-cancel" not in app:
    raise SystemExit("GameNet reservation management UI is missing")
if "duplicate" not in server or "mg-business.pending-order" not in app:
    raise SystemExit("POS order retry idempotency is missing")
if 'id="accountingDateFilter"' not in app or 'id="reportsDateFilter"' not in app:
    raise SystemExit("Accounting and management report date filters are missing")
if "apiRequest('/api/accounting/journal'+accountingReportQuery())" not in app:
    raise SystemExit("The general ledger journal must follow the accounting report date range")
if "day>=accountingFromDate" not in app or "day<=accountingToDate" not in app:
    raise SystemExit("The transaction ledger list must follow the accounting report date range")
if 'data-accounting-export' not in app or "mg-accounting-" not in app:
    raise SystemExit("Dated accounting report CSV export is missing")
if "text/csv;charset=utf-8" not in app or String not in app:
    raise SystemExit("Accounting report CSV export must use UTF-8 CSV output")
if "accountingReportQuery()" not in app or "reportsDateQuery()" not in app:
    raise SystemExit("Report date filters must be passed to the financial API endpoints")
if "data-clear-accounting-dates" not in app or "data-clear-reports-dates" not in app:
    raise SystemExit("Report date filters must provide a way to restore all-time reporting")
if "تاریخ شروع نمی‌تواند بعد از تاریخ پایان باشد." not in app:
    raise SystemExit("Report date filters must reject reversed date ranges before requesting data")
if "def make_license" not in (root / "tests/test_server.py").read_text(encoding="utf-8"):
    raise SystemExit("signed license integration tests are missing")
print(f"Project smoke checks passed: {len(parser.ids)} unique DOM ids, {len(referenced)} JS references.")
