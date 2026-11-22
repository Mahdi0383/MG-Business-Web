import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import server


class JournalImmutabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name) / "data"
        server.DB_PATH = server.DATA_DIR / "journal-immutability.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.db = server.connect()
        self.tenant_id = uuid.uuid4().hex
        self.user_id = uuid.uuid4().hex
        self.account_id = uuid.uuid4().hex
        self.entry_id = uuid.uuid4().hex
        self.line_id = uuid.uuid4().hex
        now = int(time.time())
        self.db.execute(
            "INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
            (self.tenant_id, "Journal test", now),
        )
        self.db.execute(
            """INSERT INTO users
               (id,tenant_id,username,password_hash,role,active,created_at)
               VALUES(?,?,?,?, 'owner',1,?)""",
            (self.user_id, self.tenant_id, "owner-" + self.user_id, "test-hash", now),
        )
        self.db.execute(
            """INSERT INTO accounts
               (id,tenant_id,code,name,account_type,normal_balance)
               VALUES(?,?,?,'Test cash','asset','debit')""",
            (self.account_id, self.tenant_id, "T-" + self.account_id[:8]),
        )
        self.db.execute(
            """INSERT INTO journal_entries
               (id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self.entry_id, self.tenant_id, time.strftime("%Y-%m-%d"),
             "Immutable test entry", "test", self.entry_id, self.user_id, now),
        )
        self.db.execute(
            """INSERT INTO journal_lines
               (id,tenant_id,entry_id,account_id,debit,credit)
               VALUES(?,?,?,?,100,0)""",
            (self.line_id, self.tenant_id, self.entry_id, self.account_id),
        )

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def test_posted_entry_cannot_be_updated_or_deleted(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "journal entries are immutable"):
            self.db.execute(
                "UPDATE journal_entries SET memo='rewritten' WHERE id=?",
                (self.entry_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "journal entries are immutable"):
            self.db.execute("DELETE FROM journal_entries WHERE id=?", (self.entry_id,))

    def test_posted_lines_cannot_be_updated_or_deleted(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "journal lines are immutable"):
            self.db.execute(
                "UPDATE journal_lines SET debit=90,credit=0 WHERE id=?",
                (self.line_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "journal lines are immutable"):
            self.db.execute("DELETE FROM journal_lines WHERE id=?", (self.line_id,))

    def test_new_reversal_entry_and_lines_remain_insertable(self):
        reversal_id = uuid.uuid4().hex
        now = int(time.time())
        self.db.execute(
            """INSERT INTO journal_entries
               (id,tenant_id,entry_date,memo,source_type,source_id,reversal_of,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (reversal_id, self.tenant_id, time.strftime("%Y-%m-%d"),
             "Reversal", "test_reversal", reversal_id, self.entry_id, self.user_id, now),
        )
        self.db.execute(
            """INSERT INTO journal_lines
               (id,tenant_id,entry_id,account_id,debit,credit)
               VALUES(?,?,?,?,0,100)""",
            (uuid.uuid4().hex, self.tenant_id, reversal_id, self.account_id),
        )
        count = self.db.execute(
            "SELECT COUNT(*) FROM journal_lines WHERE entry_id=?",
            (reversal_id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
