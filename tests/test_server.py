import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


def make_license(modules, license_id):
    payload = {"license_id": license_id, "modules": modules, "plan": "integration",
               "expires_at": int(time.time()) + 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":"),sort_keys=True).encode()).decode().rstrip("=")
    signed = ("MG1." + encoded).encode()
    secret = os.environ["MG_LICENSE_SECRET"]
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(),signed,hashlib.sha256).digest()).decode().rstrip("=")
    return "MG1." + encoded + "." + signature


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['MG_LICENSE_SECRET'] = 'integration-test-secret-which-is-long-enough-123456'
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name)
        server.DB_PATH = Path(cls.temp.name) / "test.sqlite3"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, method, path, payload=None, token=None):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_public_routes_fail_closed_when_multiple_tenants_exist(self):
        tenant_ids = ("tenant-public-a", "tenant-public-b")
        db = server.connect()
        try:
            now = int(time.time())
            db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_ids[0],"Public A",now))
            db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_ids[1],"Public B",now+1))
            db.commit()
        finally:
            db.close()

        try:
            status, result = self.request("GET", "/api/public/menu")
            self.assertEqual(status, 409)
            self.assertIn("دامنه اختصاصی", result["error"])

            status, result = self.request("POST", "/api/public/orders", {
                "customerName": "Test Customer",
                "phone": "+49123456789",
                "items": [{"productId": "unknown-product", "quantity": 1}]
            })
            self.assertEqual(status, 409)
            self.assertIn("دامنه اختصاصی", result["error"])
        finally:
            # The integration server shares one temporary database across tests.
            # Remove only fixtures created here so test order cannot affect setup.
            db = server.connect()
            try:
                db.execute("DELETE FROM tenants WHERE id IN (?,?)", tenant_ids)
                db.commit()
            finally:
                db.close()
            # Keep the process-wide public-order rate limiter isolated between tests.
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1", None)

    def test_setup_auth_transaction_audit_and_isolation(self):
        status, result = self.request("GET", "/api/setup-status")
        self.assertEqual((status, result["configured"]), (200, False))

        status, result = self.request("POST", "/api/setup", {
            "businessName": "MG Test Shop", "username": "owner",
            "password": "correct-horse-battery"
        })
        self.assertEqual(status, 200)
        token = result["token"]
        self.assertEqual(result["user"]["role"], "owner")

        status, result = self.request("POST", "/api/setup", {
            "businessName": "Second Shop", "username": "other",
            "password": "correct-horse-battery"
        })
        self.assertEqual(status, 409)

        status, result = self.request("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["tenant"]["name"], "MG Test Shop")

        valid_code = make_license(["cafe"], "bad-license")
        parts = valid_code.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        bad_code = ".".join(parts)
        status, result = self.request("POST", "/api/licenses/activate", {"code": bad_code}, token)
        self.assertEqual(status, 400)
        limited_code = make_license(["cafe","accounting","inventory","customers","reports"], "license-limited")
        status, result = self.request("POST", "/api/licenses/activate", {"code": limited_code}, token)
        self.assertEqual(status, 200)
        self.assertNotIn("gamenet", result["modules"])
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(status, 403)
        full_code = make_license(["cafe","gamenet","accounting","customers","inventory","reports"], "license-full")
        status, result = self.request("POST", "/api/licenses/activate", {"code": full_code}, token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/modules", token=token)
        self.assertIn("gamenet", result["modules"])

        status, result = self.request("POST", "/api/users", {
            "username": "cashier", "password": "cashier-long-password", "role": "cashier"
        }, token)
        self.assertEqual(status, 201)
        cashier_id = result["id"]
        status, result = self.request("POST", "/api/login", {
            "username": "cashier", "password": "cashier-long-password"
        })
        self.assertEqual(status, 200)
        cashier_token = result["token"]
        status, denied_reset = self.request("POST", "/api/users/"+cashier_id+"/password", {
            "password":"cashier-new-long-password"
        }, cashier_token)
        self.assertEqual(status,403)
        status, reset = self.request("POST", "/api/users/"+cashier_id+"/password", {
            "password":"cashier-new-long-password"
        }, token)
        self.assertEqual(status,200)
        status, old_session = self.request("GET", "/api/me", token=cashier_token)
        self.assertEqual(status,401)
        status, login_new = self.request("POST", "/api/login", {
            "username":"cashier","password":"cashier-new-long-password"
        })
        self.assertEqual(status,200)
        cashier_token = login_new["token"]
        status, result = self.request("POST", "/api/products", {
            "name": "Forbidden", "sellPrice": 100, "costPrice": 20
        }, cashier_token)
        self.assertEqual(status, 403)
        status, result = self.request("DELETE", "/api/users/" + cashier_id, token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/me", token=cashier_token)
        self.assertEqual(status, 401)

        status, result = self.request("POST", "/api/transactions", {
            "id": "tx-test-1", "title": "Cafe sale", "amount": 150000,
            "kind": "income", "category": "Cafe",
            "createdAt": "2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_tx = self.request("POST", "/api/transactions", {
            "id":"tx-test-1","title":"Cafe sale","amount":150000,
            "kind":"income","category":"Cafe","createdAt":"2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_tx["duplicate"])
        status, conflict_tx = self.request("POST", "/api/transactions", {
            "id":"tx-test-1","title":"Different sale","amount":150000,
            "kind":"income","category":"Cafe","createdAt":"2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status,409)

        status, audit = self.request("GET", "/api/audit-log", token=token)
        self.assertEqual(status,200)
        self.assertTrue(any(x["action"]=="transaction.created" for x in audit["items"]))
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["amount"], 150000)

        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["revenue"], 150000)
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(trial["totalDebit"], trial["totalCredit"])
        status, balance_sheet = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_sheet["balanced"])
        db = server.connect()
        try:
            totals = db.execute("""SELECT SUM(l.debit),SUM(l.credit) FROM journal_lines l
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE e.tenant_id=(SELECT tenant_id FROM users WHERE username='owner')
                  AND e.source_type='transaction' AND e.source_id='tx-test-1'""").fetchone()
            self.assertEqual((totals[0], totals[1]), (150000, 150000))
            journal_id = db.execute("SELECT id FROM journal_entries WHERE source_type='transaction' AND source_id='tx-test-1'").fetchone()[0]
            with self.assertRaises(Exception):
                db.execute("UPDATE journal_entries SET memo='tampered' WHERE id=?", (journal_id,))
                db.commit()
            db.rollback()
        finally:
            db.close()

        status, result = self.request("POST", "/api/transactions", {
            "id": "tx-bad", "title": "Invalid amount", "amount": True,
            "kind": "income", "createdAt": "2026-11-16T12:00:00Z"
        }, token)
        self.assertEqual(status, 400)

        status, result = self.request("POST", "/api/products", {
            "id": "product-1", "name": "Espresso", "sku": "ESP-01",
            "category": "Coffee", "sellPrice": 85000, "costPrice": 30000
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("GET", "/api/products", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["sellPrice"], 85000)
        status, result = self.request("POST", "/api/products", {
            "id": "product-2", "name": "Duplicate SKU", "sku": "ESP-01",
            "sellPrice": 90000, "costPrice": 30000
        }, token)
        self.assertEqual(status, 409)
        status, duplicate_product_id = self.request("POST", "/api/products", {
            "id":"product-1","name":"Duplicate product ID","sku":"ESP-02",
            "sellPrice":100000,"costPrice":40000
        }, token)
        self.assertEqual(status,409)

        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-in-1", "productId": "product-1", "movementType": "purchase",
            "quantityDelta": 20, "unitCost": 30000
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_movement = self.request("POST", "/api/inventory/movements", {
            "id":"move-in-1","productId":"product-1","movementType":"purchase",
            "quantityDelta":20,"unitCost":30000
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_movement["duplicate"])
        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-out-1", "productId": "product-1", "movementType": "sale",
            "quantityDelta": -5, "unitCost": 30000
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-out-2", "productId": "product-1", "movementType": "sale",
            "quantityDelta": -16, "unitCost": 30000
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["quantity"], 15)

        status, result = self.request("POST", "/api/customers", {
            "id": "customer-1", "fullName": "Test Customer",
            "phone": "+491234567", "email": "test@example.com"
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_customer = self.request("POST", "/api/customers", {
            "id":"customer-duplicate","fullName":"Duplicate Customer",
            "phone":"+491234567"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["fullName"], "Test Customer")

        status, result = self.request("POST", "/api/orders", {
            "id": "order-test-1", "customerId": "customer-1", "discount": 5000,
            "paymentMethod": "card",
            "items": [{"productId": "product-1", "quantity": 2}]
        }, token)
        self.assertEqual(status, 201)
        self.assertEqual(result["total"], 165000)
        status, daily_best = self.request("GET","/api/reports/best-selling-today",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(daily_best["items"]),1)
        self.assertEqual(daily_best["items"][0]["productId"],"product-1")
        self.assertEqual(daily_best["items"][0]["quantity"],2)
        self.assertEqual(daily_best["items"][0]["revenue"],170000)  # Product line revenue is before invoice-level discount allocation.
        self.assertEqual(daily_best["items"][0]["orderCount"],1)
        status, duplicate_order = self.request("POST", "/api/orders", {
            "id":"order-test-1","customerId":"customer-1","discount":5000,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_order["duplicate"])
        self.assertEqual(duplicate_order["orderNumber"],result["orderNumber"])
        status, conflicting_order = self.request("POST", "/api/orders", {
            "id":"order-test-1","customerId":"customer-1","discount":0,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]
        }, token)
        self.assertEqual(status,409)
        db = server.connect()
        try:
            card_receivable = db.execute("""SELECT SUM(l.debit) FROM journal_lines l
                JOIN accounts a ON a.id=l.account_id
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE e.source_type='transaction' AND e.source_id='order-order-test-1' AND a.code='1010'""").fetchone()[0]
            self.assertEqual(card_receivable, 165000)
        finally:
            db.close()
        order_number = result["orderNumber"]
        status, result = self.request("GET", "/api/orders", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["orderNumber"], order_number)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(result["items"][0]["quantity"], 13)

        status, result = self.request("POST", "/api/orders", {
            "id": "order-credit-1", "customerId": "customer-1", "discount": 0,
            "paymentMethod": "credit",
            "items": [{"productId": "product-1", "quantity": 1}]
        }, token)
        self.assertEqual(status, 201)
        self.assertEqual(result["total"], 85000)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(result["items"][0]["balance"], 85000)
        status, result = self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["remainingBalance"],35000)
        status, duplicate_payment = self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_payment["duplicate"])
        self.assertEqual(duplicate_payment["remainingBalance"],35000)
        status, result = self.request("POST", "/api/customers/customer-1/payments", {
            "amount": 40000, "paymentMethod": "cash"
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(result["items"][0]["balance"], 35000)
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial["totalDebit"], trial["totalCredit"])
        status, balance_sheet = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_sheet["balanced"])

        status, result = self.request("GET", "/api/public/menu")
        self.assertEqual(status, 200)
        self.assertTrue(any(x["id"] == "product-1" for x in result["items"]))
        status, result = self.request("POST", "/api/public/orders", {
            "clientOrderId": "public-order-01", "customerName": "Online Guest", "phone": "+491111111",
            "items": [{"productId": "product-1", "quantity": 1}]
        })
        self.assertEqual(status, 201)
        self.assertTrue(result["orderNumber"].startswith("MG-ON-"))
        db = server.connect()
        try:
            self.assertIsNotNone(db.execute("SELECT 1 FROM orders WHERE id='public-public-order-01'").fetchone())
        finally:
            db.close()
        order_number = result["orderNumber"]
        status, result = self.request("POST", "/api/public/orders", {
            "clientOrderId": "public-order-01", "customerName": "Online Guest", "phone": "+491111111",
            "items": [{"productId": "product-1", "quantity": 1}]
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["orderNumber"], order_number)
        for _ in range(3):
            status, result = self.request("POST", "/api/public/orders", {})
            self.assertEqual(status, 400)
        status, result = self.request("POST", "/api/public/orders", {})
        self.assertEqual(status, 429)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(result["items"][0]["quantity"], 11)

        status, result = self.request("DELETE", "/api/products/product-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/products", token=token)
        self.assertEqual(result["items"][0]["active"], 0)

        status, result = self.request("DELETE", "/api/orders/order-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/orders", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "order-test-1")["status"], "cancelled")
        status, repeated_return = self.request("DELETE", "/api/orders/order-test-1", token=token)
        self.assertEqual(status,200)
        self.assertTrue(repeated_return["duplicate"])
        status, missing_return = self.request("DELETE", "/api/orders/not-found", token=token)
        self.assertEqual(status,404)
        db = server.connect()
        try:
            restored = db.execute("SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE product_id='product-1'").fetchone()[0]
            self.assertEqual(restored, 13)
            # A returned order must reverse both revenue and cost of goods sold.
            returned_order = db.execute("SELECT tenant_id,order_number FROM orders WHERE id='order-test-1'").fetchone()
            tenant_id, returned_order_number = returned_order["tenant_id"], returned_order["order_number"]
            cogs_net = db.execute("""SELECT COALESCE(SUM(l.debit-l.credit),0)
                FROM journal_lines l JOIN accounts a ON a.id=l.account_id
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE a.tenant_id=? AND a.code='5100' AND (
                    (e.source_type='order_cogs' AND e.source_id='order-test-1') OR
                    (e.source_type='inventory_movement' AND e.source_id IN
                        (SELECT id FROM inventory_movements WHERE tenant_id=? AND note=?))
                )""", (tenant_id,tenant_id,"برگشت سفارش "+returned_order_number)).fetchone()[0]
            self.assertEqual(cogs_net, 0)
        finally:
            db.close()
        status, trial_after_return = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial_after_return["totalDebit"], trial_after_return["totalCredit"])
        status, balance_after_return = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_after_return["balanced"])

        status, result = self.request("DELETE", "/api/transactions/tx-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("order-order-credit-1", [x["id"] for x in result["items"]])
        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(result["netProfit"], -40000)
        status, result = self.request("GET", "/api/accounting/journal", token=token)
        self.assertEqual(len(result["items"]), 13)
        self.assertTrue(all(item["debit"] == item["credit"] for item in result["items"]))

        db = server.connect()
        try:
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 3)
        finally:
            db.close()

        status, result = self.request("GET", "/api/backup", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["schema"], "mg-business-server-backup")
        self.assertIn("transactions", result["tables"])
        self.assertIn("suppliers", result["tables"])
        self.assertIn("purchase_invoices", result["tables"])
        self.assertIn("supplier_payments", result["tables"])
        self.assertIn("gamenet_reservations", result["tables"])
        self.assertIn("game_session_segments", result["tables"])
        self.assertNotIn("password_hash", result["tables"]["users"][0])

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-01", "name": "PC-01", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": False
        }, token)
        self.assertEqual(status, 201)
        # Device updates must be handled only by POST; GET must never attempt a write.
        status, result = self.request("GET", "/api/gamenet/devices/pc-01", token=token)
        self.assertEqual(status, 404)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-01", "deviceId": "pc-01", "customerId": "customer-1", "playersCount": 2
        }, token)
        self.assertEqual(status, 201)
        status, retry_session = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-01","deviceId":"pc-01","customerId":"customer-1","playersCount":2
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_session["duplicate"])
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-02", "deviceId": "pc-01", "playersCount": 1
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("POST", "/api/gamenet/devices", {
            "id":"pc-02","name":"PC-02","deviceType":"pc","hourlyRate":180000,"isFree":False
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/devices/pc-01", {
            "name": "PC-01 Updated", "deviceType": "pc", "hourlyRate": 130000, "isFree": False
        }, token)
        self.assertEqual(status, 200)
        db = server.connect()
        try:
            old_start=int(__import__('time').time())-1200
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-01'", (old_start,))
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01'", (old_start,))
            db.commit()
        finally:
            db.close()
        status, moved = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "deviceId":"pc-02","playersCount":3,"isFree":True
        }, token)
        self.assertEqual(status,200)
        self.assertEqual(moved["deviceId"],"pc-02")
        self.assertEqual(moved["playersCount"],3)
        self.assertTrue(moved["isFree"])
        db = server.connect()
        try:
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01' AND ended_at IS NULL",
                (int(__import__('time').time())-600,))
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["amountDue"], 39000)
        self.assertLess(result["amountDue"], 41000)
        game_charge = result["amountDue"]
        status, retry_end = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_end["duplicate"])
        self.assertEqual(retry_end["amountDue"],game_charge)
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(result["items"][0]["name"], "PC-01 Updated")
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "customer-1")["balance"], 35000 + game_charge)
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["status"], "ended")
        status, segments = self.request("GET","/api/gamenet/sessions/session-01/segments",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(segments["items"]),2)
        self.assertEqual(segments["items"][0]["deviceId"],"pc-01")
        self.assertEqual(segments["items"][1]["deviceId"],"pc-02")
        self.assertEqual(segments["items"][0]["playersCount"],2)
        self.assertEqual(segments["items"][1]["playersCount"],3)
        self.assertTrue(segments["items"][1]["isFree"])

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-free", "name": "Free PC", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": True
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-free", "deviceId": "pc-free", "playersCount": 1
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions/session-free/end", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result["amountDue"], 0)

        # Supplier invoices must increase stock, create payable journal entries, and settle atomically.
        status, result = self.request("POST", "/api/products", {
            "id":"product-purchase-test","name":"Milk","sku":"MILK-01",
            "sellPrice":40000,"costPrice":20000
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/products", {
            "id":"product-purchase-test-2","name":"Coffee Beans","sku":"BEANS-01",
            "sellPrice":80000,"costPrice":35000
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/suppliers", {
            "id":"supplier-1","name":"Test Dairy","phone":"+49123456789"
        }, token)
        self.assertEqual(status,201)
        status, duplicate_supplier = self.request("POST", "/api/suppliers", {
            "id":"supplier-duplicate","name":"Test Dairy"
        }, token)
        self.assertEqual(status,409)
        status, duplicate_supplier_id = self.request("POST", "/api/suppliers", {
            "id":"supplier-1","name":"Second Supplier"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30001,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["total"],419999)
        db=server.connect()
        try:
            discounted_lines=db.execute("""SELECT product_id,SUM(quantity) AS quantity,
                SUM(line_total) AS line_total FROM purchase_invoice_items
                WHERE tenant_id=(SELECT tenant_id FROM users WHERE username=?)
                AND invoice_id='purchase-invoice-1' GROUP BY product_id""",("owner",)).fetchall()
            by_product={row["product_id"]:row for row in discounted_lines}
            self.assertEqual(by_product["product-purchase-test"]["line_total"],233333)
            self.assertEqual(by_product["product-purchase-test-2"]["line_total"],186666)
            self.assertEqual(sum(row["line_total"] for row in discounted_lines),419999)
            self.assertEqual(sum(row["quantity"] for row in discounted_lines),15)
        finally:
            db.close()
        status, retry_invoice = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30001,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_invoice["duplicate"])
        status, duplicate_invoice = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-duplicate","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":0,
            "items":[{"productId":"product-purchase-test","quantity":1,"unitCost":25000}]
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(next(x for x in result["items"] if x["productId"]=="product-purchase-test")["quantity"],10)
        self.assertEqual(next(x for x in result["items"] if x["productId"]=="product-purchase-test-2")["quantity"],5)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["remainingBalance"],319999)
        status, partial_statement = self.request("GET", "/api/suppliers/supplier-1/ledger", token=token)
        self.assertEqual(status,200)
        self.assertEqual(partial_statement["totalPurchases"],419999)
        self.assertEqual(partial_statement["totalPayments"],100000)
        self.assertEqual(partial_statement["balance"],319999)
        status, unknown_supplier = self.request("GET", "/api/suppliers/missing-supplier/ledger", token=token)
        self.assertEqual(status,404)
        status, duplicate_supplier_payment = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_supplier_payment["duplicate"])
        self.assertEqual(duplicate_supplier_payment["remainingBalance"],319999)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":319999,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["status"],"paid")
        self.assertEqual(result["remainingBalance"],0)
        status, statement = self.request("GET", "/api/suppliers/supplier-1/ledger", token=token)
        self.assertEqual(status,200)
        self.assertEqual(statement["totalPurchases"],419999)
        self.assertEqual(statement["totalPayments"],419999)
        self.assertEqual(statement["balance"],0)
        self.assertEqual(len(statement["items"]),3)
        self.assertEqual([row["balanceAfter"] for row in statement["items"]],[0,319999,419999])
        self.assertEqual(statement["items"][0]["sourceType"],"payment")
        self.assertEqual(statement["items"][0]["balanceAfter"],0)
        self.assertEqual(statement["items"][-1]["sourceType"],"invoice")
        self.assertEqual(statement["items"][-1]["balanceAfter"],419999)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":1,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/purchase-invoices", token=token)
        self.assertEqual(result["items"][0]["status"],"paid")
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial["totalDebit"],trial["totalCredit"])

        # GameNet reservations prevent overlapping bookings and bind check-in to the reserved customer.
        datetime_module=__import__("datetime")
        start_at=datetime_module.datetime.fromtimestamp(time.time()-10,datetime_module.timezone.utc).isoformat(timespec="seconds")
        end_at=datetime_module.datetime.fromtimestamp(time.time()+3600,datetime_module.timezone.utc).isoformat(timespec="seconds")
        naive_start=datetime_module.datetime.fromtimestamp(time.time()+7200).isoformat(timespec="seconds")
        naive_end=datetime_module.datetime.fromtimestamp(time.time()+10800).isoformat(timespec="seconds")
        status, invalid_timezone=self.request("POST","/api/gamenet/reservations",{
            "id":"reservation-naive-time","deviceId":"pc-01","startsAt":naive_start,"endsAt":naive_end
        },token)
        self.assertEqual(status,400)
        status, reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-01","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":2,"notes":"Birthday booking"
        }, token)
        self.assertEqual(status,201)
        status, retry_reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-01","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":2,"notes":"Birthday booking"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_reservation["duplicate"])
        status, conflict = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-02","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":1
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-wrong-customer","deviceId":"pc-free","playersCount":1
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-reserved","deviceId":"pc-free","customerId":"customer-1","playersCount":2
        }, token)
        self.assertEqual(status,201)
        status, reservations = self.request("GET", "/api/gamenet/reservations", token=token)
        self.assertEqual(status,200)
        self.assertEqual(next(x for x in reservations["items"] if x["id"]=="reservation-01")["status"],"completed")
        status, result = self.request("POST", "/api/gamenet/sessions/session-reserved/end", {}, token)
        self.assertEqual(status,200)
        self.assertEqual(result["amountDue"],0)
        future_start=datetime_module.datetime.fromtimestamp(time.time()+7200,datetime_module.timezone.utc).isoformat(timespec="seconds")
        future_end=datetime_module.datetime.fromtimestamp(time.time()+10800,datetime_module.timezone.utc).isoformat(timespec="seconds")
        status, result = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-cancel","deviceId":"pc-01","startsAt":future_start,"endsAt":future_end
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/reservations/reservation-cancel/cancel", {}, token)
        self.assertEqual(status,200)
        self.assertFalse(result["duplicate"])
        status, retry_cancel = self.request("POST", "/api/gamenet/reservations/reservation-cancel/cancel", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_cancel["duplicate"])
        status, missing_cancel = self.request("POST", "/api/gamenet/reservations/not-found/cancel", {}, token)
        self.assertEqual(status,404)
        status, snapshot = self.request("GET", "/api/backup", token=token)
        self.assertEqual(status, 200)
        altered = json.loads(json.dumps(snapshot))
        altered["tenant"]["id"] = "different-workspace"
        status, result = self.request("POST", "/api/backup/restore", altered, token)
        self.assertEqual(status, 400)
        missing_chart = json.loads(json.dumps(snapshot))
        missing_chart["tables"]["accounts"] = [
            row for row in missing_chart["tables"]["accounts"] if row["code"] != "5100"
        ]
        status, result = self.request("POST", "/api/backup/restore", missing_chart, token)
        self.assertEqual(status, 400)
        unbalanced = json.loads(json.dumps(snapshot))
        unbalanced["tables"]["journal_lines"][0]["debit"] += 1
        status, result = self.request("POST", "/api/backup/restore", unbalanced, token)
        self.assertEqual(status, 400)
        broken = json.loads(json.dumps(snapshot))
        broken["tables"]["order_items"].append({
            "id": "broken-backup-item", "tenant_id": broken["tenant"]["id"],
            "order_id": "missing-order", "product_id": "missing-product",
            "product_name": "Broken", "quantity": 1, "unit_price": 1,
            "unit_cost": 0, "line_total": 1
        })
        status, result = self.request("POST", "/api/backup/restore", broken, token)
        self.assertEqual(status, 409)
        # A failed restore must leave the pre-restore workspace intact.
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["items"]), 2)
        # Reversal rows may share a timestamp with their source. Deliberately
        # reverse snapshot order to prove restore honors the self-referencing FK.
        journal_rows = snapshot["tables"]["journal_entries"]
        self.assertTrue(any(row.get("reversal_of") for row in journal_rows))
        snapshot["tables"]["journal_entries"] = list(reversed(journal_rows))
        status, result = self.request("POST", "/api/backup/restore", snapshot, token)
        self.assertEqual(status, 200)
        self.assertGreater(result["restoredRows"], 0)
        db = server.connect()
        try:
            journal_id = db.execute("SELECT id FROM journal_entries LIMIT 1").fetchone()[0]
            with self.assertRaises(Exception):
                db.execute("UPDATE journal_entries SET memo='tampered-after-restore' WHERE id=?", (journal_id,))
                db.commit()
            db.rollback()
        finally:
            db.close()
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["items"]), 2)
        status, restored_purchases = self.request("GET", "/api/purchase-invoices", token=token)
        self.assertEqual(status,200)
        self.assertEqual(restored_purchases["items"][0]["status"],"paid")
        status, restored_reservations = self.request("GET", "/api/gamenet/reservations", token=token)
        self.assertEqual(status,200)
        self.assertEqual(next(x for x in restored_reservations["items"] if x["id"]=="reservation-01")["status"],"completed")
        self.assertEqual(next(x for x in restored_reservations["items"] if x["id"]=="reservation-cancel")["status"],"cancelled")

        status, result = self.request("GET", "/api/me")
        self.assertEqual(status, 401)

    def test_public_orders_support_addons_and_reject_reused_ids_with_changed_content(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "public-addons-test.sqlite3"
        with server.LOGIN_LOCK:
            server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName":"Online Addon Cafe","username":"online-addon-owner",
                "password":"online-addon-owner-password-2026"
            })
            self.assertEqual(status,200)
            token=setup["token"]
            code=make_license(["cafe","inventory","accounting"],"public-addon-license")
            status, _ = self.request("POST","/api/licenses/activate",{"code":code},token)
            self.assertEqual(status,200)
            status, product = self.request("POST","/api/products",{
                "id":"public-addon-product","name":"Latte","sellPrice":100,"costPrice":30
            },token)
            self.assertEqual(status,201)
            db=server.connect()
            try:
                tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='online-addon-owner'").fetchone()[0]
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,
                    quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("public-addon-stock",tenant_id,"public-addon-product",setup["user"]["id"],"purchase",4,30,"opening",int(time.time())))
                db.commit()
            finally:
                db.close()
            status, addon = self.request("POST","/api/product-addons",{
                "id":"public-addon-01","productId":"public-addon-product","name":"Oat milk","price":25
            },token)
            self.assertEqual(status,201)
            status, menu = self.request("GET","/api/public/menu")
            self.assertEqual(status,200)
            menu_product=next(x for x in menu["items"] if x["id"]=="public-addon-product")
            self.assertEqual(menu_product["addons"],[{"id":"public-addon-01","productId":"public-addon-product","name":"Oat milk","price":25}])
            payload={
                "clientOrderId":"public-addon-order-01","customerName":"Online Guest",
                "phone":"+49123456789",
                "items":[{"productId":"public-addon-product","quantity":2,"addons":["public-addon-01"]}]
            }
            status, order = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,201)
            self.assertEqual(order["total"],250)
            status, duplicate = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,200)
            self.assertTrue(duplicate["duplicate"])
            changed=dict(payload)
            changed["items"]=[{"productId":"public-addon-product","quantity":2}]
            status, conflict = self.request("POST","/api/public/orders",changed)
            self.assertEqual(status,409)
            db=server.connect()
            try:
                item=db.execute("SELECT unit_price,line_total,addons_json FROM order_items WHERE order_id='public-public-addon-order-01'").fetchone()
                self.assertEqual((item["unit_price"],item["line_total"]),(125,250))
                self.assertEqual(json.loads(item["addons_json"]),[{"id":"public-addon-01","name":"Oat milk","price":25}])
            finally:
                db.close()
            status, removed = self.request("DELETE","/api/product-addons/public-addon-01",token=token)
            self.assertEqual(status,200)
            status, duplicate = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,200)
            self.assertTrue(duplicate["duplicate"])
            unavailable=dict(payload)
            unavailable["clientOrderId"]="public-addon-order-02"
            status, result = self.request("POST","/api/public/orders",unavailable)
            self.assertEqual(status,409)
        finally:
            server.DB_PATH=original_db_path
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)

    def test_cafe_product_addons_are_priced_snapshotted_and_idempotent(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "product-addons-test.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Addon Cafe", "username": "addon-owner",
                "password": "addon-owner-password-2026"
            })
            self.assertEqual(status, 200)
            token = setup["token"]
            license_code = make_license(["cafe","inventory","accounting"], "addon-license")
            status, activated = self.request("POST", "/api/licenses/activate", {"code":license_code}, token)
            self.assertEqual(status, 200)
            status, product = self.request("POST", "/api/products", {
                "id":"addon-product","name":"Coffee","sellPrice":100,"costPrice":30
            }, token)
            self.assertEqual(status, 201)
            db = server.connect()
            try:
                tenant_id = db.execute("SELECT tenant_id FROM users WHERE username='addon-owner'").fetchone()[0]
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,
                    quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("addon-stock",tenant_id,"addon-product",setup["user"]["id"],"purchase",5,30,"opening stock",int(time.time())))
                db.commit()
            finally:
                db.close()
            status, too_large = self.request("POST", "/api/product-addons", {
                "id":"addon-too-large","productId":"addon-product","name":"Unsafe price","price":9007199254740992
            }, token)
            self.assertEqual(status,400)
            status, created = self.request("POST", "/api/product-addons", {
                "id":"addon-01","productId":"addon-product","name":"Extra shot","price":20
            }, token)
            self.assertEqual(status, 201)
            status, unsafe_update = self.request("POST", "/api/product-addons/addon-01", {
                "name":"Extra shot","price":9007199254740992
            }, token)
            self.assertEqual(status,400)
            status, updated = self.request("POST", "/api/product-addons/addon-01", {
                "name":"Extra shot","price":25
            }, token)
            self.assertEqual(status, 200)
            status, addons = self.request("GET", "/api/product-addons", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(addons["items"][0]["price"], 25)

            order_payload = {
                "id":"addon-order-01",
                "items":[{"productId":"addon-product","quantity":2,"addons":["addon-01"]}],
                "discount":0,"paymentMethod":"cash"
            }
            status, order = self.request("POST", "/api/orders", order_payload, token)
            self.assertEqual(status, 201)
            self.assertEqual(order["subtotal"], 250)
            self.assertEqual(order["total"], 250)
            status, details = self.request("GET", "/api/orders/addon-order-01", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(details["order"]["total"],250)
            self.assertEqual(details["items"][0]["addons"],[{"id":"addon-01","name":"Extra shot","price":25}])
            status, duplicate = self.request("POST", "/api/orders", order_payload, token)
            self.assertEqual(status, 200)
            self.assertTrue(duplicate["duplicate"])
            status, conflict = self.request("POST", "/api/orders", {
                **order_payload,
                "items":[{"productId":"addon-product","quantity":2}]
            }, token)
            self.assertEqual(status, 409)

            db = server.connect()
            try:
                item = db.execute("SELECT unit_price,line_total,addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual((item["unit_price"],item["line_total"]),(125,250))
                saved = json.loads(item["addons_json"])
                self.assertEqual(saved,[{"id":"addon-01","name":"Extra shot","price":25}])
                stock = db.execute("SELECT SUM(quantity_delta) FROM inventory_movements WHERE product_id='addon-product'").fetchone()[0]
                self.assertEqual(stock,3)
            finally:
                db.close()

            status, snapshot = self.request("GET", "/api/backup", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["tables"]["product_addons"][0]["id"],"addon-01")
            self.assertEqual(snapshot["tables"]["order_items"][0]["addons_json"],item["addons_json"])
            status, removed = self.request("DELETE", "/api/product-addons/addon-01", token=token)
            self.assertEqual(status, 200)
            status, unavailable = self.request("POST", "/api/orders", {
                "id":"addon-order-02",
                "items":[{"productId":"addon-product","quantity":1,"addons":["addon-01"]}],
                "discount":0,"paymentMethod":"cash"
            }, token)
            self.assertEqual(status, 409)
            db = server.connect()
            try:
                saved = db.execute("SELECT addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual(json.loads(saved["addons_json"])[0]["name"],"Extra shot")
            finally:
                db.close()
            status, restored = self.request("POST", "/api/backup/restore", snapshot, token)
            self.assertEqual(status, 200)
            status, restored_addons = self.request("GET", "/api/product-addons", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(restored_addons["items"][0]["id"],"addon-01")
            db = server.connect()
            try:
                restored_item = db.execute("SELECT addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual(json.loads(restored_item["addons_json"])[0]["price"],25)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path

    def test_backup_scheduler_retries_transient_failures_with_backoff(self):
        delays = []

        class StopAfterThreeWaits:
            def is_set(self):
                return False

            def wait(self, seconds):
                delays.append(seconds)
                return len(delays) >= 3

        original_interval = server.BACKUP_INTERVAL_SECONDS
        try:
            server.BACKUP_INTERVAL_SECONDS = 86400
            with mock.patch("server.create_snapshot", side_effect=[
                OSError("temporary disk issue"),
                OSError("temporary disk issue"),
                Path("/tmp/mg-test-snapshot.sqlite3"),
            ]) as create:
                server.backup_scheduler(StopAfterThreeWaits())
                self.assertEqual(create.call_count, 3)
            self.assertEqual(delays, [60, 120, 86400])
        finally:
            server.BACKUP_INTERVAL_SECONDS = original_interval

    def test_backup_export_rejects_oversized_workspace_before_fetching_rows(self):
        original_limit = server.BACKUP_MAX_ROWS
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "backup-export-limit.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Backup Limit Test", "username": "backup-limit-owner",
                "password": "backup-limit-password-2026"
            })
            self.assertEqual(status, 200)
            server.BACKUP_MAX_ROWS = 1
            status, result = self.request("GET", "/api/backup", token=setup["token"])
            self.assertEqual(status, 413)
            self.assertIn("پشتیبان", result["error"])
        finally:
            server.BACKUP_MAX_ROWS = original_limit
            server.DB_PATH = original_db_path

    def test_client_ip_trusts_real_ip_only_from_configured_proxy(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"X-Real-IP": "198.51.100.23"}
        old = os.environ.get("MG_TRUSTED_PROXIES")
        try:
            os.environ.pop("MG_TRUSTED_PROXIES", None)
            self.assertEqual(handler.client_ip(), "127.0.0.1")
            os.environ["MG_TRUSTED_PROXIES"] = "127.0.0.1/32"
            self.assertEqual(handler.client_ip(), "198.51.100.23")
            handler.headers = {"X-Real-IP": "not-an-ip"}
            self.assertEqual(handler.client_ip(), "127.0.0.1")
            os.environ["MG_TRUSTED_PROXIES"] = "192.0.2.0/24"
            self.assertEqual(handler.client_ip(), "127.0.0.1")
        finally:
            if old is None:
                os.environ.pop("MG_TRUSTED_PROXIES", None)
            else:
                os.environ["MG_TRUSTED_PROXIES"] = old

    def test_readiness_endpoint_checks_database_schema_and_integrity(self):
        with urllib.request.urlopen(self.base + "/api/ready", timeout=5) as response:
            self.assertEqual(response.status,200)
            payload=json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["service"],"mg-business")
        self.assertEqual(payload["checks"],{
            "databaseIntegrity":True,"foreignKeys":True,"requiredSchema":True,"migrations":True
        })
        self.assertEqual(payload["missingTables"],[])

    def test_production_responses_include_hsts_and_cross_origin_guards(self):
        old_env = {key: os.environ.get(key) for key in ("MG_ENV", "MG_PUBLIC_URL")}
        try:
            os.environ["MG_ENV"] = "production"
            os.environ["MG_PUBLIC_URL"] = "https://business.example.com"
            with urllib.request.urlopen(self.base + "/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Strict-Transport-Security"), "max-age=31536000")
                self.assertEqual(response.headers.get("Cross-Origin-Opener-Policy"), "same-origin")
                self.assertEqual(response.headers.get("Cross-Origin-Resource-Policy"), "same-origin")
                self.assertEqual(response.headers.get("X-Permitted-Cross-Domain-Policies"), "none")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_public_static_server_hides_source_and_development_files(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
        for path in ("/server.py", "/license_tool.py", "/backup_manager.py", "/README.md", "/tests/test_server.py"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(caught.exception.code, 404)

    def test_session_idle_expiry_and_absolute_lifetime(self):
        # Use an isolated database so this security test cannot contaminate
        # the shared integration fixtures or depend on test execution order.
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "session-test.sqlite3"
        try:
            status, result = self.request("POST", "/api/setup", {
                "businessName": "Session Test Shop", "username": "session-owner",
                "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            token = result["token"]
            token_hash = server.hashlib.sha256(token.encode()).hexdigest()
            db = server.connect()
            try:
                now = int(server.time.time())
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (now + 5, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 200)
            db = server.connect()
            try:
                refreshed = db.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
                self.assertGreaterEqual(refreshed["expires_at"], int(server.time.time()) + 29 * 60)
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (int(server.time.time()) - 1, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 401)

            status, result = self.request("POST", "/api/login", {
                "username": "session-owner", "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            token = result["token"]
            token_hash = server.hashlib.sha256(token.encode()).hexdigest()
            db = server.connect()
            try:
                db.execute("UPDATE sessions SET created_at=? WHERE token_hash=?",
                           (int(server.time.time()) - server.SESSION_SECONDS - 1, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 401)
        finally:
            server.DB_PATH = original_db_path

    def test_public_order_throttle_state_is_bounded_and_enforced(self):
        with server.LOGIN_LOCK:
            original = server.PUBLIC_ORDER_FAILURES.copy()
            server.PUBLIC_ORDER_FAILURES.clear()
            try:
                for index in range(2100):
                    ip = "203.0."+str(index // 256)+"."+str(index % 256)
                    self.assertTrue(server.remember_public_order_attempt(ip, 1000))
                self.assertLessEqual(len(server.PUBLIC_ORDER_FAILURES), 2048)
                for attempt in range(5):
                    self.assertTrue(server.remember_public_order_attempt("198.51.100.7", 2000 + attempt))
                self.assertFalse(server.remember_public_order_attempt("198.51.100.7", 2005))
                self.assertTrue(server.remember_public_order_attempt("198.51.100.7", 3000))
            finally:
                server.PUBLIC_ORDER_FAILURES.clear()
                server.PUBLIC_ORDER_FAILURES.update(original)

    def test_login_throttle_state_is_bounded(self):
        with server.LOGIN_LOCK:
            original_user = server.LOGIN_FAILURES.copy()
            original_ip = server.LOGIN_IP_FAILURES.copy()
            server.LOGIN_FAILURES.clear()
            server.LOGIN_IP_FAILURES.clear()
            try:
                for index in range(4200):
                    ip = "198.51."+str(index // 256)+"."+str(index % 256)
                    server.remember_login_failure((ip,"user-"+str(index)), ip, index)
                self.assertLessEqual(len(server.LOGIN_FAILURES), 4096)
                self.assertLessEqual(len(server.LOGIN_IP_FAILURES), 2048)
            finally:
                server.LOGIN_FAILURES.clear()
                server.LOGIN_FAILURES.update(original_user)
                server.LOGIN_IP_FAILURES.clear()
                server.LOGIN_IP_FAILURES.update(original_ip)

    def test_login_rate_limit_covers_username_rotation_per_ip(self):
        ip = "127.0.0.1"
        with server.LOGIN_LOCK:
            server.LOGIN_IP_FAILURES.pop(ip, None)
        try:
            for index in range(30):
                status, _ = self.request("POST", "/api/login", {
                    "username": "unknown-user-" + str(index),
                    "password": "incorrect-password"
                })
                self.assertEqual(status, 401)
            status, result = self.request("POST", "/api/login", {
                "username": "another-unknown-user", "password": "incorrect-password"
            })
            self.assertEqual(status, 429)
        finally:
            with server.LOGIN_LOCK:
                server.LOGIN_IP_FAILURES.pop(ip, None)
                for key in list(server.LOGIN_FAILURES):
                    if key[0] == ip:
                        server.LOGIN_FAILURES.pop(key, None)

    def test_manager_cannot_reset_owner_password(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "owner-password-guard.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Owner Guard", "username": "owner-guard",
                "password": "owner-secure-password-2026"
            })
            self.assertEqual(status, 200)
            owner_token = setup["token"]
            status, created = self.request("POST", "/api/users", {
                "username": "delegated-manager",
                "password": "manager-secure-password-2026",
                "role": "manager"
            }, owner_token)
            self.assertEqual(status, 201)
            status, manager_login = self.request("POST", "/api/login", {
                "username": "delegated-manager",
                "password": "manager-secure-password-2026"
            })
            self.assertEqual(status, 200)
            status, denied = self.request("POST", "/api/users/"+setup["user"]["id"]+"/password", {
                "password": "attacker-controlled-password"
            }, manager_login["token"])
            self.assertEqual(status, 403)
            status, owner_login = self.request("POST", "/api/login", {
                "username": "owner-guard",
                "password": "owner-secure-password-2026"
            })
            self.assertEqual(status, 200)
        finally:
            server.DB_PATH = original_db_path

    def test_user_can_change_own_password_and_revoke_old_sessions(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "password-change-test.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Password Test Shop", "username": "password-owner",
                "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            old_token = setup["token"]
            status, denied = self.request("POST", "/api/account/password", {
                "currentPassword": "wrong-current-password",
                "newPassword": "new-secure-password-2026"
            }, old_token)
            self.assertEqual(status, 403)
            status, changed = self.request("POST", "/api/account/password", {
                "currentPassword": "correct-horse-battery",
                "newPassword": "new-secure-password-2026"
            }, old_token)
            self.assertEqual(status, 200)
            self.assertTrue(changed.get("token"))
            status, _ = self.request("GET", "/api/me", token=old_token)
            self.assertEqual(status, 401)
            status, me = self.request("GET", "/api/me", token=changed["token"])
            self.assertEqual(status, 200)
            self.assertEqual(me["user"]["username"], "password-owner")
            status, login = self.request("POST", "/api/login", {
                "username": "password-owner", "password": "new-secure-password-2026"
            })
            self.assertEqual(status, 200)
        finally:
            server.DB_PATH = original_db_path

    def test_password_hash_is_salted_and_verified(self):
        encoded = server.password_hash("a-long-test-password")
        self.assertTrue(server.verify_password("a-long-test-password", encoded))
        self.assertFalse(server.verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, server.password_hash("a-long-test-password"))


if __name__ == "__main__":
    unittest.main()
