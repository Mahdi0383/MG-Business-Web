import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import server


class SharedTenantIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name) / "data"
        server.DB_PATH = server.DATA_DIR / "shared-tenant-integrity.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def new_id():
        return uuid.uuid4().hex

    def setUp(self):
        self.db = server.connect()
        now = int(time.time())
        self.tenant_a, self.tenant_b = self.new_id(), self.new_id()
        self.user_a, self.user_b = self.new_id(), self.new_id()
        self.customer_a, self.customer_b = self.new_id(), self.new_id()
        self.product_a, self.product_b = self.new_id(), self.new_id()
        self.device_a, self.device_b = self.new_id(), self.new_id()

        for tenant_id, name in ((self.tenant_a, "A"), (self.tenant_b, "B")):
            self.db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                            (tenant_id, "Tenant " + name, now))
        for tenant_id, user_id in ((self.tenant_a, self.user_a), (self.tenant_b, self.user_b)):
            self.db.execute(
                """INSERT INTO users(id,tenant_id,username,password_hash,role,active,created_at)
                   VALUES(?,?,?,'test-hash','owner',1,?)""",
                (user_id, tenant_id, "user-" + user_id, now),
            )
        for tenant_id, customer_id, name in (
            (self.tenant_a, self.customer_a, "Customer A"),
            (self.tenant_b, self.customer_b, "Customer B"),
        ):
            self.db.execute(
                """INSERT INTO customers(id,tenant_id,full_name,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (customer_id, tenant_id, name, now, now),
            )
        for tenant_id, product_id, name in (
            (self.tenant_a, self.product_a, "Product A"),
            (self.tenant_b, self.product_b, "Product B"),
        ):
            self.db.execute(
                """INSERT INTO products(id,tenant_id,name,sell_price,cost_price,created_at,updated_at)
                   VALUES(?,?,?,100,50,?,?)""",
                (product_id, tenant_id, name, now, now),
            )
        for tenant_id, device_id, name in (
            (self.tenant_a, self.device_a, "Device A"),
            (self.tenant_b, self.device_b, "Device B"),
        ):
            self.db.execute(
                """INSERT INTO game_devices
                   (id,tenant_id,name,device_type,hourly_rate,is_free,active,created_at)
                   VALUES(?,?,?,'pc',1000,0,1,?)""",
                (device_id, tenant_id, name, now),
            )

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def create_order(self):
        order_id = self.new_id()
        self.db.execute(
            """INSERT INTO orders
               (id,tenant_id,order_number,customer_id,created_by,status,subtotal,discount,
                total,payment_method,created_at)
               VALUES(?,?,?,?,?,'paid',100,0,100,'cash',?)""",
            (order_id, self.tenant_a, "ORD-" + order_id, self.customer_a,
             self.user_a, int(time.time())),
        )
        return order_id

    def create_session(self):
        session_id = self.new_id()
        self.db.execute(
            """INSERT INTO game_sessions
               (id,tenant_id,device_id,customer_id,started_by,started_at,status,
                players_count,rate_per_hour,is_free,amount_due,created_at)
               VALUES(?,?,?,?,?,?,'running',1,1000,0,0,?)""",
            (session_id, self.tenant_a, self.device_a, self.customer_a,
             self.user_a, int(time.time()), int(time.time())),
        )
        return session_id

    def test_valid_inventory_order_game_and_customer_ledger_records(self):
        order_id = self.create_order()
        session_id = self.create_session()
        now = int(time.time())
        self.db.execute(
            """INSERT INTO inventory_movements
               (id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,created_at)
               VALUES(?,?,?,?,'purchase',2,50,?)""",
            (self.new_id(), self.tenant_a, self.product_a, self.user_a, now),
        )
        self.db.execute(
            """INSERT INTO order_items
               (id,tenant_id,order_id,product_id,product_name,quantity,unit_price,unit_cost,line_total)
               VALUES(?,?,?,?,?,1,100,50,100)""",
            (self.new_id(), self.tenant_a, order_id, self.product_a, "Product A"),
        )
        self.db.execute(
            """INSERT INTO game_session_segments
               (id,tenant_id,session_id,device_id,started_at,players_count,rate_per_hour,is_free)
               VALUES(?,?,?,?,?,1,1000,0)""",
            (self.new_id(), self.tenant_a, session_id, self.device_a, now),
        )


    def test_inventory_movement_rejects_foreign_product_and_actor(self):
        now = int(time.time())
        for product_id, actor_id in ((self.product_b, self.user_a),
                                     (self.product_a, self.user_b)):
            with self.subTest(product_id=product_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO inventory_movements
                           (id,tenant_id,product_id,created_by,movement_type,quantity_delta,created_at)
                           VALUES(?,?,?,?,'adjustment',1,?)""",
                        (self.new_id(), self.tenant_a, product_id, actor_id, now),
                    )

    def test_order_rejects_foreign_customer_or_creator(self):
        for customer_id, actor_id in ((self.customer_b, self.user_a),
                                      (self.customer_a, self.user_b)):
            with self.subTest(customer_id=customer_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO orders
                           (id,tenant_id,order_number,customer_id,created_by,status,
                            subtotal,discount,total,payment_method,created_at)
                           VALUES(?,?,?,?,?,'paid',100,0,100,'cash',?)""",
                        (self.new_id(), self.tenant_a, "ORD-" + self.new_id(),
                         customer_id, actor_id, int(time.time())),
                    )

    def test_order_item_rejects_foreign_order_or_product(self):
        order_id = self.create_order()
        attempts = ((self.tenant_a, order_id, self.product_b),
                    (self.tenant_b, order_id, self.product_b))
        for tenant_id, linked_order, product_id in attempts:
            with self.subTest(tenant_id=tenant_id, product_id=product_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO order_items
                           (id,tenant_id,order_id,product_id,product_name,quantity,
                            unit_price,unit_cost,line_total)
                           VALUES(?,?,?,?,?,1,100,50,100)""",
                        (self.new_id(), tenant_id, linked_order, product_id, "Foreign"),
                    )

    def test_game_session_rejects_foreign_device_customer_or_actor(self):
        now = int(time.time())
        attempts = ((self.device_b, self.customer_a, self.user_a),
                    (self.device_a, self.customer_b, self.user_a),
                    (self.device_a, self.customer_a, self.user_b))
        for device_id, customer_id, actor_id in attempts:
            with self.subTest(device_id=device_id, customer_id=customer_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO game_sessions
                           (id,tenant_id,device_id,customer_id,started_by,started_at,status,
                            players_count,rate_per_hour,is_free,created_at)
                           VALUES(?,?,?,?,?,?,'running',1,1000,0,?)""",
                        (self.new_id(), self.tenant_a, device_id, customer_id,
                         actor_id, now, now),
                    )

    def test_game_segment_rejects_foreign_session_or_device(self):
        session_id = self.create_session()
        now = int(time.time())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO game_session_segments
                   (id,tenant_id,session_id,device_id,started_at,players_count,rate_per_hour,is_free)
                   VALUES(?,?,?,?,?,1,1000,0)""",
                (self.new_id(), self.tenant_a, session_id, self.device_b, now),
            )




if __name__ == "__main__":
    unittest.main()
