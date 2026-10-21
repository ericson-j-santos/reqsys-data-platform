"""Fail-closed unit controls for HML inventory (fake connection, NOT SQL E2E)."""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from scripts import sqlserver_hml_inventory as probe

VALID_ENV = {
    "SQLSERVER_INVENTORY_ENV": "HML",
    "SQLSERVER_INVENTORY_APPROVED": "HML-READONLY",
    "SQLSERVER_EXPECTED_SERVER": "hml-server",
    "SQLSERVER_EXPECTED_DATABASE": "analytics_hml",
    "SQLSERVER_INVENTORY_SOURCE_SHA": "a" * 40,
    "SQLSERVER_CORRELATION_ID": "test-hml-20261012",
    "SQLSERVER_HML_DSN": (
        "Driver={ODBC Driver 18 for SQL Server};Server=hml-server;"
        "Database=analytics_hml;Encrypt=Yes;TrustServerCertificate=no;"
        "ApplicationIntent=ReadOnly;Trusted_Connection=Yes"
    ),
}
SUCCESS = {
    probe.VERSION_SQL: ("17.0.5005.3", "Developer Edition", "hml-server",
                        "analytics_hml", 0, 0, 0, 0),
    probe.COMPAT_SQL: (170,),
    probe.QS_SQL: ("READ_WRITE", "READ_WRITE", 0),
    probe.TLS_SQL: ("TRUE",),
}

class FakeCursor:
    def __init__(self, owner):
        self.owner = owner
        self.query = None
    def execute(self, query):
        self.query = query
        self.owner.executed.append(query)
        if self.owner.fail_on == query:
            raise RuntimeError("private-string-must-never-leak")
    def fetchone(self):
        return self.owner.rows[self.query]
    def close(self):
        self.owner.cursor_closed += 1

class FakeConnection:
    def __init__(self, rows=None, fail_on=None):
        self.rows = dict(SUCCESS if rows is None else rows)
        self.fail_on = fail_on
        self.executed = []
        self.closed = False
        self.cursor_closed = 0
    def cursor(self):
        return FakeCursor(self)
    def close(self):
        self.closed = True

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(VALID_ENV)
        self.conn = FakeConnection()
        self.calls = []
    def connect(self, dsn, **kwargs):
        self.calls.append((dsn, kwargs))
        return self.conn
    def test_positive_scope_and_metadata(self):
        result = probe.collect(self.env, self.connect)
        self.assertEqual("observed", result["status"])
        self.assertEqual(170, result["compatibility_level"])
        self.assertTrue(result["openjson_compatible"])
        self.assertEqual("READ_WRITE", result["query_store_actual"])
        self.assertTrue(result["tls_encrypted"])
        self.assertEqual(4, len(self.conn.executed))
        self.assertEqual(4, self.conn.cursor_closed)
        self.assertTrue(self.conn.closed)
        self.assertEqual({"timeout": 8, "autocommit": True}, self.calls[0][1])
        self.assertNotIn("hml-server", str(result))
        self.assertNotIn("analytics_hml", str(result))
        self.assertNotIn(self.env["SQLSERVER_HML_DSN"], str(result))
    def test_same_input_idempotent(self):
        left = probe.collect(self.env, self.connect)
        self.conn = FakeConnection()
        right = probe.collect(self.env, self.connect)
        self.assertEqual(left, right)
    def test_missing_authorization_fails_before_connection(self):
        self.env.pop("SQLSERVER_INVENTORY_APPROVED")
        with self.assertRaisesRegex(probe.InventoryBlocked, "authorization_marker_missing"):
            probe.collect(self.env, self.connect)
        self.assertEqual([], self.calls)
    def test_prod_fails_closed(self):
        self.env["SQLSERVER_INVENTORY_ENV"] = "PROD"
        with self.assertRaisesRegex(probe.InventoryBlocked, "environment_not_hml"):
            probe.collect(self.env, self.connect)
        self.assertEqual([], self.calls)
    def test_certificate_or_encryption_disabled_fails_before_connect(self):
        for original, substitute in [("Encrypt=Yes","Encrypt=No"),
                                     ("TrustServerCertificate=no","TrustServerCertificate=yes"),
                                     ("ReadOnly","ReadWrite")]:
            with self.subTest(substitute=substitute):
                bad = dict(self.env)
                bad["SQLSERVER_HML_DSN"] = bad["SQLSERVER_HML_DSN"].replace(
                    original, substitute)
                with self.assertRaises(probe.InventoryBlocked):
                    probe.collect(bad, self.connect)
        self.assertEqual([], self.calls)
    def test_duplicate_dsn_fields_rejected(self):
        self.env["SQLSERVER_HML_DSN"] += ";Encrypt=no"
        with self.assertRaises(probe.InventoryBlocked):
            probe.collect(self.env, self.connect)
        self.assertEqual([], self.calls)
    def test_dsn_server_mismatch_rejected_before_connection(self):
        self.env["SQLSERVER_HML_DSN"] = self.env["SQLSERVER_HML_DSN"].replace(
            "Server=hml-server;", "Server=prod-server;")
        with self.assertRaisesRegex(probe.InventoryBlocked, "dsn_server_mismatch"):
            probe.collect(self.env, self.connect)
        self.assertEqual([], self.calls)

    def test_dsn_server_casefold_matches_approved_server(self):
        self.env["SQLSERVER_HML_DSN"] = self.env["SQLSERVER_HML_DSN"].replace(
            "Server=hml-server;", "Server=HML-SERVER;")
        result = probe.collect(self.env, self.connect)
        self.assertEqual("observed", result["status"])
        self.assertEqual(1, len(self.calls))

    def test_wrong_server_rejects_without_querying_other_metadata(self):
        self.conn.rows[probe.VERSION_SQL] = (
            "17.0.5005.3", "Developer Edition", "prod-server",
            "analytics_hml", 0,0,0,0)
        with self.assertRaisesRegex(probe.InventoryBlocked, "resolved_target_mismatch"):
            probe.collect(self.env, self.connect)
        self.assertEqual([probe.VERSION_SQL], self.conn.executed)
        self.assertTrue(self.conn.closed)
    def test_admin_role_rejected_before_further_queries(self):
        self.conn.rows[probe.VERSION_SQL] = (
            "17.0.5005.3", "Developer Edition", "hml-server",
            "analytics_hml", 1,0,0,0)
        with self.assertRaisesRegex(probe.InventoryBlocked, "privileged_identity_not_allowed"):
            probe.collect(self.env, self.connect)
        self.assertEqual([probe.VERSION_SQL], self.conn.executed)
    def test_old_compatibility_reports_openjson_incompatibility(self):
        self.conn.rows[probe.COMPAT_SQL] = (120,)
        result = probe.collect(self.env, self.connect)
        self.assertEqual(120, result["compatibility_level"])
        self.assertFalse(result["openjson_compatible"])
    def test_malformed_compatibility_blocks(self):
        self.conn.rows[probe.COMPAT_SQL] = ("?",)
        with self.assertRaisesRegex(probe.InventoryBlocked, "compatibility_level_invalid"):
            probe.collect(self.env, self.connect)
        self.assertEqual([probe.VERSION_SQL, probe.COMPAT_SQL], self.conn.executed)
    def test_tls_unencrypted_blocks(self):
        self.conn.rows[probe.TLS_SQL] = ("FALSE",)
        with self.assertRaisesRegex(probe.InventoryBlocked, "tls_not_verified"):
            probe.collect(self.env, self.connect)
        self.assertTrue(self.conn.closed)
    def test_exception_sanitized(self):
        fake = types.SimpleNamespace(connect=self.connect)
        self.conn.fail_on = probe.QS_SQL
        with patch.dict(os.environ, self.env, clear=True), patch.dict(sys.modules, {"pyodbc": fake}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = probe.main()
        data = json.loads(output.getvalue())
        self.assertEqual(2, status)
        self.assertEqual({"status": "blocked", "code": "connection_or_metadata_error"}, data)
        self.assertNotIn("private-string-must-never-leak", output.getvalue())
        self.assertTrue(self.conn.closed)

if __name__ == "__main__":
    unittest.main()
