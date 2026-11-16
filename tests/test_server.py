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

        status, result = self.request("POST", "/api/transactions", {
            "id": "tx-bad", "title": "Invalid amount", "amount": True,
            "kind": "income", "createdAt": "2026-11-16T12:00:00Z"
        }, token)
        self.assertEqual(status, 400)

        status, result = self.request("DELETE", "/api/transactions/tx-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(result["items"], [])

        db = server.connect()
        try:
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 3)
        finally:
            db.close()

        status, result = self.request("GET", "/api/me")
        self.assertEqual(status, 401)

    def test_password_hash_is_salted_and_verified(self):
        encoded = server.password_hash("a-long-test-password")
        self.assertTrue(server.verify_password("a-long-test-password", encoded))
        self.assertFalse(server.verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, server.password_hash("a-long-test-password"))


if __name__ == "__main__":
    unittest.main()
