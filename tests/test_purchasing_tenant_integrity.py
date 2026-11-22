import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import server


class PurchasingTenantIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name) / "data"
        server.DB_PATH = server.DATA_DIR / "tenant-integrity.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.db = server.connect()
        self.a = self._id()
        self.b = self._id()
        self.user_a = self._id()
        self.user_b = self._id()
        self.supplier_a = self._id()
        self.supplier_b = self._id()
        self.supplier_a_other = self._id()
        self.product_a = self._id()
        self.product_b = self._id()
        now = int(time.time())

        self.db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                        (self.a, "Tenant A", now))
        self.db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                        (self.b, "Tenant B", now))
        for tenant_id, user_id, username in (
            (self.a, self.user_a, "owner-" + self.a),
            (self.b, self.user_b, "owner-" + self.b),
        ):
            self.db.execute(
                """INSERT INTO users(id,tenant_id,username,password_hash,role,active,created_at)
                   VALUES(?,?,?,?, 'owner',1,?)""",
                (user_id, tenant_id, username, "test-hash", now),
            )
        for tenant_id, supplier_id, name in (
            (self.a, self.supplier_a, "Supplier A"),
            (self.b, self.supplier_b, "Supplier B"),
            (self.a, self.supplier_a_other, "Supplier A Other"),
        ):
            self.db.execute(
                """INSERT INTO suppliers(id,tenant_id,name,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (supplier_id, tenant_id, name, now, now),
            )
        for tenant_id, product_id, name in (
            (self.a, self.product_a, "Product A"),
            (self.b, self.product_b, "Product B"),
        ):
            self.db.execute(
                """INSERT INTO products(id,tenant_id,name,sell_price,cost_price,created_at,updated_at)
                   VALUES(?,?,?,100,50,?,?)""",
                (product_id, tenant_id, name, now, now),
            )

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    @staticmethod
    def _id():
        return uuid.uuid4().hex

    def _invoice(self, tenant_id=None, supplier_id=None, created_by=None):
        invoice_id = self._id()
        tenant_id = tenant_id or self.a
        supplier_id = supplier_id or self.supplier_a
        created_by = created_by or self.user_a
        now = int(time.time())
        self.db.execute(
            """INSERT INTO purchase_invoices
               (id,tenant_id,supplier_id,created_by,invoice_number,invoice_date,
                subtotal,discount,total,paid_amount,status,created_at)
               VALUES(?,?,?,?,?,?,100,0,100,0,'unpaid',?)""",
            (invoice_id, tenant_id, supplier_id, created_by,
             "INV-" + invoice_id, time.strftime("%Y-%m-%d"), now),
        )
        return invoice_id

    def test_valid_purchase_invoice_item_and_payment_remain_supported(self):
        invoice_id = self._invoice()
        self.db.execute(
            """INSERT INTO purchase_invoice_items
               (id,tenant_id,invoice_id,product_id,product_name,quantity,unit_cost,line_total)
               VALUES(?,?,?,?,?,2,50,100)""",
            (self._id(), self.a, invoice_id, self.product_a, "Product A"),
        )
        self.db.execute(
            """INSERT INTO supplier_payments
               (id,tenant_id,invoice_id,supplier_id,created_by,payment_method,amount,created_at)
               VALUES(?,?,?,?,?,'cash',25,?)""",
            (self._id(), self.a, invoice_id, self.supplier_a, self.user_a, int(time.time())),
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM purchase_invoice_items WHERE invoice_id=?",
                            (invoice_id,)).fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM supplier_payments WHERE invoice_id=?",
                            (invoice_id,)).fetchone()[0], 1
        )

    def test_purchase_invoice_rejects_foreign_supplier_or_actor(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._invoice(supplier_id=self.supplier_b)

        with self.assertRaises(sqlite3.IntegrityError):
            self._invoice(created_by=self.user_b)

    def test_purchase_items_reject_foreign_invoice_or_product(self):
        invoice_id = self._invoice()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO purchase_invoice_items
                   (id,tenant_id,invoice_id,product_id,product_name,quantity,unit_cost,line_total)
                   VALUES(?,?,?,?,?,1,50,50)""",
                (self._id(), self.a, invoice_id, self.product_b, "Foreign product"),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO purchase_invoice_items
                   (id,tenant_id,invoice_id,product_id,product_name,quantity,unit_cost,line_total)
                   VALUES(?,?,?,?,?,1,50,50)""",
                (self._id(), self.b, invoice_id, self.product_b, "Foreign invoice"),
            )

    def test_supplier_payment_rejects_foreign_invoice_supplier_or_actor(self):
        invoice_id = self._invoice()
        attempts = (
            (self.b, invoice_id, self.supplier_b, self.user_b),
            (self.a, invoice_id, self.supplier_a_other, self.user_a),
            (self.a, invoice_id, self.supplier_a, self.user_b),
        )
        for tenant_id, linked_invoice, supplier_id, actor_id in attempts:
            with self.subTest(tenant_id=tenant_id, supplier_id=supplier_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO supplier_payments
                           (id,tenant_id,invoice_id,supplier_id,created_by,payment_method,amount,created_at)
                           VALUES(?,?,?,?,?,'cash',10,?)""",
                        (self._id(), tenant_id, linked_invoice, supplier_id, actor_id, int(time.time())),
                    )

    def test_tenant_guards_also_apply_to_relationship_updates(self):
        invoice_id = self._invoice()
        item_id = self._id()
        self.db.execute(
            """INSERT INTO purchase_invoice_items
               (id,tenant_id,invoice_id,product_id,product_name,quantity,unit_cost,line_total)
               VALUES(?,?,?,?,?,1,50,50)""",
            (item_id, self.a, invoice_id, self.product_a, "Product A"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE purchase_invoice_items SET product_id=? WHERE id=?",
                            (self.product_b, item_id))


if __name__ == "__main__":
    unittest.main()
