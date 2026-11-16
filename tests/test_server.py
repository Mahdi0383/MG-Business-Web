import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
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
        db = server.connect()
        try:
            restored = db.execute("SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE product_id='product-1'").fetchone()[0]
            self.assertEqual(restored, 13)
        finally:
            db.close()

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
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-02", "deviceId": "pc-01", "playersCount": 1
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("POST", "/api/gamenet/devices/pc-01", {
            "name": "PC-01 Updated", "deviceType": "pc", "hourlyRate": 130000, "isFree": False
        }, token)
        self.assertEqual(status, 200)
        db = server.connect()
        try:
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-01'", (int(__import__('time').time())-3600,))
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["amountDue"], 120000)
        self.assertLess(result["amountDue"], 130000)
        game_charge = result["amountDue"]
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(result["items"][0]["name"], "PC-01 Updated")
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "customer-1")["balance"], 35000 + game_charge)
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["status"], "ended")

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
            "invoiceDate":"2026-11-16","discount":30000,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["total"],420000)
        status, retry_invoice = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30000,
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
        self.assertEqual(result["remainingBalance"],320000)
        status, duplicate_supplier_payment = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_supplier_payment["duplicate"])
        self.assertEqual(duplicate_supplier_payment["remainingBalance"],320000)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":320000,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["status"],"paid")
        self.assertEqual(result["remainingBalance"],0)
        status, statement = self.request("GET", "/api/suppliers/supplier-1/ledger", token=token)
        self.assertEqual(status,200)
        self.assertEqual(statement["totalPurchases"],420000)
        self.assertEqual(statement["totalPayments"],420000)
        self.assertEqual(statement["balance"],0)
        self.assertEqual(len(statement["items"]),3)
        self.assertEqual(statement["items"][0]["sourceType"],"payment")
        self.assertEqual(statement["items"][0]["balanceAfter"],0)
        self.assertEqual(statement["items"][-1]["sourceType"],"invoice")
        self.assertEqual(statement["items"][-1]["balanceAfter"],420000)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":1,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/purchase-invoices", token=token)
        self.assertEqual(result["items"][0]["status"],"paid")
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial["totalDebit"],trial["totalCredit"])

        # GameNet reservations prevent overlapping bookings and bind check-in to the reserved customer.
        start_at=__import__("datetime").datetime.fromtimestamp(time.time()-10).strftime("%Y-%m-%dT%H:%M:%S")
        end_at=__import__("datetime").datetime.fromtimestamp(time.time()+3600).strftime("%Y-%m-%dT%H:%M:%S")
        status, reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-01","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":2,"notes":"Birthday booking"
        }, token)
        self.assertEqual(status,201)
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
        future_start=__import__("datetime").datetime.fromtimestamp(time.time()+7200).strftime("%Y-%m-%dT%H:%M")
        future_end=__import__("datetime").datetime.fromtimestamp(time.time()+10800).strftime("%Y-%m-%dT%H:%M")
        status, result = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-cancel","deviceId":"pc-01","startsAt":future_start,"endsAt":future_end
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/reservations/reservation-cancel/cancel", {}, token)
        self.assertEqual(status,200)
        status, snapshot = self.request("GET", "/api/backup", token=token)
        self.assertEqual(status, 200)
        altered = json.loads(json.dumps(snapshot))
        altered["tenant"]["id"] = "different-workspace"
        status, result = self.request("POST", "/api/backup/restore", altered, token)
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

    def test_public_static_server_hides_source_and_development_files(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
        for path in ("/server.py", "/license_tool.py", "/README.md", "/tests/test_server.py"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(caught.exception.code, 404)

    def test_password_hash_is_salted_and_verified(self):
        encoded = server.password_hash("a-long-test-password")
        self.assertTrue(server.verify_password("a-long-test-password", encoded))
        self.assertFalse(server.verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, server.password_hash("a-long-test-password"))


if __name__ == "__main__":
    unittest.main()
