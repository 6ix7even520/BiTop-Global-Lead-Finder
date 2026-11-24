import unittest
import sqlite3
import tempfile
from pathlib import Path

import app
from app import LOCAL_HOST, LOCAL_ORIGIN, validate_local_host, validate_local_request


class LocalRequestSecurityTests(unittest.TestCase):
    def test_same_origin_post_is_allowed(self):
        validate_local_request({"Host": LOCAL_HOST, "Origin": LOCAL_ORIGIN}, require_origin=True)

    def test_wrong_host_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1:8765"):
            validate_local_request({"Host": "evil.example", "Origin": LOCAL_ORIGIN})

    def test_cross_site_get_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "跨站"):
            validate_local_request({"Host": LOCAL_HOST, "Origin": "https://evil.example"})

    def test_missing_origin_post_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "请求来源"):
            validate_local_request({"Host": LOCAL_HOST}, require_origin=True)


    def test_v11_customer_database_records_are_preserved(self):
        old_db = app.DB
        with tempfile.TemporaryDirectory() as td:
            try:
                app.DB = Path(td) / "leads.sqlite3"
                conn = sqlite3.connect(app.DB)
                try:
                    cols = ["id INTEGER PRIMARY KEY", "created TEXT", "updated TEXT"]
                    cols.extend(name + (" INTEGER DEFAULT 0" if name in app.FLAGS else " TEXT DEFAULT ''") for name in app.FIELDS)
                    conn.execute("CREATE TABLE leads(" + ",".join(cols) + ")")
                    conn.execute("INSERT INTO leads(created, updated, name, company, country, product, notes) VALUES(?,?,?,?,?,?,?)",
                                 ("2026-11-20", "2026-11-20", "Existing supplier", "Existing Auto Parts Ltd", "PH", "Brake Cleaner", "Keep this customer"))
                    conn.commit()
                finally:
                    conn.close()
                app.init()
                records = app.rows()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["company"], "Existing Auto Parts Ltd")
                self.assertEqual(records[0]["notes"], "Keep this customer")
            finally:
                app.DB = old_db

    def test_oauth_top_level_callback_only_needs_loopback_host(self):
        # OAuth callback is a top-level navigation from OpenAI; state + PKCE protect it.
        validate_local_host({"Host": LOCAL_HOST, "Origin": "https://auth.openai.com"})


if __name__ == "__main__":
    unittest.main()
