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
dynamic = {"moduleAdd", "authForm", "ledgerSearch", "cafeCart", "cartSubtotal", "submitOrder", "cafeProduct", "cafeQuantity"}
missing = sorted(referenced - parser.ids - dynamic)
if missing:
    raise SystemExit("missing DOM ids: " + ", ".join(missing))
for asset in ("styles.css", "app.js", "core.js", "server.py", "license_tool.py", "manifest.webmanifest", "service-worker.js", "icon.svg", "order.html", "public-order.js", "tests/test_server.py"):
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
if "def translate_path(self, path):" not in server or "def list_directory(self, path):" not in server:
    raise SystemExit("static server must block private files and directory listing")
if "def verify_license_code(code):" not in server or 'if path == "/api/backup/restore":' not in server:
    raise SystemExit("license verification or backup restoration endpoint is missing")
get_handler = server.split("    def do_GET(self):", 1)[1].split("    def do_DELETE(self):", 1)[0]
post_handler = server.split("    def do_POST(self):", 1)[1].split("    def create_session", 1)[0]
if re.search(r"\bdata(?:\.get|\[)", get_handler):
    raise SystemExit("GET handler must not read mutation request data")
if 'if path.startswith("/api/gamenet/devices/")' not in post_handler:
    raise SystemExit("game device updates must be routed through POST")
if 'if path == "/api/suppliers":' not in server or 'if path == "/api/purchase-invoices":' not in server or 'path.endswith("/payments")' not in server:
    raise SystemExit("supplier and purchase invoice workflows are missing")
if "def make_license" not in (root / "tests/test_server.py").read_text(encoding="utf-8"):
    raise SystemExit("signed license integration tests are missing")
print(f"Project smoke checks passed: {len(parser.ids)} unique DOM ids, {len(referenced)} JS references.")
