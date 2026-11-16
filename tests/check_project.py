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
dynamic = {"moduleAdd"}
missing = sorted(referenced - parser.ids - dynamic)
if missing:
    raise SystemExit("missing DOM ids: " + ", ".join(missing))
for asset in ("styles.css", "app.js", "core.js", "manifest.webmanifest", "service-worker.js"):
    if not (root / asset).is_file():
        raise SystemExit(f"missing required asset: {asset}")
if "./core.js" not in html:
    raise SystemExit("core.js must load before app.js")
if "importData" not in parser.ids or "importFile" not in parser.ids:
    raise SystemExit("backup restore controls are missing")
print(f"Project smoke checks passed: {len(parser.ids)} unique DOM ids, {len(referenced)} JS references.")
