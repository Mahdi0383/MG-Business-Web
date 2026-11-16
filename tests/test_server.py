import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["amount"], 150000)

        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["revenue"], 150000)
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

        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-in-1", "productId": "product-1", "movementType": "purchase",
            "quantityDelta": 20, "unitCost": 30000
        }, token)
        self.assertEqual(status, 201)
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

        status, result = self.request("DELETE", "/api/products/product-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/products", token=token)
        self.assertEqual(result["items"][0]["active"], 0)

        status, result = self.request("DELETE", "/api/orders/order-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/orders", token=token)
        self.assertEqual(result["items"][0]["status"], "cancelled")
        db = server.connect()
        try:
            restored = db.execute("SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE product_id='product-1'").fetchone()[0]
            self.assertEqual(restored, 15)
        finally:
            db.close()

        status, result = self.request("DELETE", "/api/transactions/tx-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "order-order-test-1")
        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(result["netProfit"], -150000)
        status, result = self.request("GET", "/api/accounting/journal", token=token)
        self.assertEqual(len(result["items"]), 8)
        self.assertTrue(all(item["debit"] == item["credit"] for item in result["items"]))

        db = server.connect()
        try:
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 3)
        finally:
            db.close()

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-01", "name": "PC-01", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": False
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-01", "deviceId": "pc-01", "playersCount": 2
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-02", "deviceId": "pc-01", "playersCount": 1
        }, token)
        self.assertEqual(status, 409)
        db = server.connect()
        try:
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-01'", (int(__import__('time').time())-3600,))
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["amountDue"], 120000)
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["status"], "ended")

        status, result = self.request("GET", "/api/me")
        self.assertEqual(status, 401)

    def test_password_hash_is_salted_and_verified(self):
        encoded = server.password_hash("a-long-test-password")
        self.assertTrue(server.verify_password("a-long-test-password", encoded))
        self.assertFalse(server.verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, server.password_hash("a-long-test-password"))


if __name__ == "__main__":
    unittest.main()
