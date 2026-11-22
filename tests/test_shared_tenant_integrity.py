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

    def test_valid_inventory_order_and_game_records(self):
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
            """INSERT INTO customer_ledger
               (id,tenant_id,customer_id,created_by,source_type,source_id,debit,credit,created_at)
               VALUES(?,?,?,?, 'manual', ?,100,0,?)""",
            (self.new_id(), self.tenant_a, self.customer_a, self.user_a,
             self.new_id(), now),
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



    def test_product_addon_rejects_foreign_product(self):
        now = int(time.time())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO product_addons
                   (id,tenant_id,product_id,name,price,active,created_at,updated_at)
                   VALUES(?,?,?,'Extra',10,1,?,?)""",
                (self.new_id(), self.tenant_a, self.product_b, now, now),
            )

    def test_reservation_rejects_foreign_device_customer_or_creator(self):
        now = int(time.time())
        attempts = ((self.device_b, self.customer_a, self.user_a),
                    (self.device_a, self.customer_b, self.user_a),
                    (self.device_a, self.customer_a, self.user_b))
        for device_id, customer_id, actor_id in attempts:
            with self.subTest(device_id=device_id, customer_id=customer_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO gamenet_reservations
                           (id,tenant_id,device_id,customer_id,created_by,starts_at,ends_at,
                            status,players_count,created_at)
                           VALUES(?,?,?,?,?,?,?,'booked',1,?)""",
                        (self.new_id(), self.tenant_a, device_id, customer_id, actor_id,
                         now + 3600, now + 7200, now),
                    )

    def test_loyalty_ledger_rejects_foreign_customer_or_creator(self):
        now = int(time.time())
        for customer_id, actor_id in ((self.customer_b, self.user_a),
                                      (self.customer_a, self.user_b)):
            with self.subTest(customer_id=customer_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO customer_loyalty_ledger
                           (id,tenant_id,customer_id,created_by,source_type,source_id,
                            points_delta,created_at)
                           VALUES(?,?,?,?, 'order_earn', ?,10,?)""",
                        (self.new_id(), self.tenant_a, customer_id, actor_id,
                         self.new_id(), now),
                    )


    def test_customer_ledger_rejects_foreign_customer_or_creator(self):
        now = int(time.time())
        for customer_id, actor_id in ((self.customer_b, self.user_a),
                                      (self.customer_a, self.user_b)):
            with self.subTest(customer_id=customer_id, actor_id=actor_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        """INSERT INTO customer_ledger
                           (id,tenant_id,customer_id,created_by,source_type,source_id,
                            debit,credit,created_at)
                           VALUES(?,?,?,?, 'manual', ?,100,0,?)""",
                        (self.new_id(), self.tenant_a, customer_id, actor_id,
                         self.new_id(), now),
                    )


    def test_financial_and_audit_records_reject_foreign_actors(self):
        now = int(time.time())
        invalid_writes = (
            ("transaction", """INSERT INTO transactions
                (id,tenant_id,created_by,title,amount,kind,occurred_at,created_at)
                VALUES(?,?,?,'Cross tenant',100,'income','2026-11-22',?)""",
             (self.new_id(), self.tenant_a, self.user_b, now)),
            ("journal entry", """INSERT INTO journal_entries
                (id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
                VALUES(?,?,'2026-11-22','Cross tenant','manual',?,?,?)""",
             (self.new_id(), self.tenant_a, self.new_id(), self.user_b, now)),
            ("accounting period", """INSERT INTO accounting_periods
                (id,tenant_id,start_date,end_date,closed_by,closed_at)
                VALUES(?,?,'2026-01-01','2026-12-31',?,?)""",
             (self.new_id(), self.tenant_a, self.user_b, now)),
            ("cash shift", """INSERT INTO cash_register_shifts
                (id,tenant_id,opened_by,opened_at,opening_cash,status)
                VALUES(?,?,?,? ,0,'open')""",
             (self.new_id(), self.tenant_a, self.user_b, now)),
            ("audit log", """INSERT INTO audit_log
                (tenant_id,actor_id,action,entity_type,created_at)
                VALUES(?,?,'test','tenant',?)""",
             (self.tenant_a, self.user_b, now)),
        )
        for label, statement, values in invalid_writes:
            with self.subTest(record=label):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(statement, values)

    def test_financial_and_audit_records_accept_same_tenant_actors(self):
        now = int(time.time())
        self.db.execute(
            """INSERT INTO transactions
               (id,tenant_id,created_by,title,amount,kind,occurred_at,created_at)
               VALUES(?,?,?,'Valid transaction',100,'income','2026-11-22',?)""",
            (self.new_id(), self.tenant_a, self.user_a, now),
        )
        self.db.execute(
            """INSERT INTO journal_entries
               (id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
               VALUES(?,?,'2026-11-22','Valid journal','manual',?,?,?)""",
            (self.new_id(), self.tenant_a, self.new_id(), self.user_a, now),
        )
        self.db.execute(
            """INSERT INTO accounting_periods
               (id,tenant_id,start_date,end_date,closed_by,closed_at)
               VALUES(?,?,'2026-01-01','2026-12-31',?,?)""",
            (self.new_id(), self.tenant_a, self.user_a, now),
        )
        self.db.execute(
            """INSERT INTO cash_register_shifts
               (id,tenant_id,opened_by,opened_at,opening_cash,status)
               VALUES(?,?,?, ?,0,'open')""",
            (self.new_id(), self.tenant_a, self.user_a, now),
        )
        self.db.execute(
            """INSERT INTO audit_log
               (tenant_id,actor_id,action,entity_type,created_at)
               VALUES(?,?,'test','tenant',?)""",
            (self.tenant_a, self.user_a, now),
        )


    def test_tenant_modules_cannot_reference_another_tenants_license(self):
        now = int(time.time())
        license_a, license_b = self.new_id(), self.new_id()
        for tenant_id, license_id in ((self.tenant_a, license_a),
                                      (self.tenant_b, license_b)):
            self.db.execute(
                """INSERT INTO licenses
                   (id,tenant_id,code_hash,plan,expires_at,activated_at,status)
                   VALUES(?,?,?,'pro',? ,?,'active')""",
                (license_id, tenant_id, "hash-" + license_id, now + 86400, now),
            )
        self.db.execute(
            """INSERT INTO tenant_modules
               (tenant_id,module_key,enabled,expires_at,license_id,updated_at)
               VALUES(?,'accounting',1,?,?,?)""",
            (self.tenant_a, now + 86400, license_a, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO tenant_modules
                   (tenant_id,module_key,enabled,expires_at,license_id,updated_at)
                   VALUES(?,'gamenet',1,?,?,?)""",
                (self.tenant_a, now + 86400, license_b, now),
            )
        module = self.db.execute(
            "SELECT module_key FROM tenant_modules WHERE tenant_id=?",
            (self.tenant_a,),
        ).fetchone()
        self.assertEqual(module[0], "accounting")

    def test_user_and_license_cannot_be_moved_between_tenants(self):
        now = int(time.time())
        license_id = self.new_id()
        self.db.execute(
            """INSERT INTO licenses
               (id,tenant_id,code_hash,plan,expires_at,activated_at,status)
               VALUES(?,?,?,'pro',?,?, 'active')""",
            (license_id, self.tenant_a, "hash-" + license_id, now + 86400, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE licenses SET tenant_id=? WHERE id=?",
                            (self.tenant_b, license_id))



    def test_tenant_module_updates_allow_trial_and_reject_foreign_license(self):
        now = int(time.time())
        license_a, license_b = self.new_id(), self.new_id()
        for tenant_id, license_id in ((self.tenant_a, license_a),
                                      (self.tenant_b, license_b)):
            self.db.execute(
                """INSERT INTO licenses
                   (id,tenant_id,code_hash,plan,expires_at,activated_at,status)
                   VALUES(?,?,?,'pro',?,?, 'active')""",
                (license_id, tenant_id, "hash-" + license_id, now + 86400, now),
            )
        self.db.execute(
            """INSERT INTO tenant_modules
               (tenant_id,module_key,enabled,expires_at,license_id,updated_at)
               VALUES(?,'accounting',1,?,NULL,?)""",
            (self.tenant_a, now + 86400, now),
        )
        self.db.execute(
            "UPDATE tenant_modules SET license_id=? WHERE tenant_id=? AND module_key='accounting'",
            (license_a, self.tenant_a),
        )
        self.db.execute(
            "UPDATE tenant_modules SET license_id='trial' WHERE tenant_id=? AND module_key='accounting'",
            (self.tenant_a,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE tenant_modules SET license_id=? WHERE tenant_id=? AND module_key='accounting'",
                (license_b, self.tenant_a),
            )


if __name__ == "__main__":
    unittest.main()
