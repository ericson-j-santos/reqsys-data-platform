"""Simulated driver controls. These tests do NOT validate a real SQL engine."""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
import sys
import types
import unittest
from unittest.mock import patch

from scripts import sqlserver_hml_inventory as inventory
from scripts import sqlserver_enterprise_validation as suite

ENV = {
    "SQLSERVER_INVENTORY_ENV": "HML",
    "SQLSERVER_INVENTORY_APPROVED": "HML-READONLY",
    "SQLSERVER_EXPECTED_SERVER": "fixture-server",
    "SQLSERVER_EXPECTED_DATABASE": "fixture_hml",
    "SQLSERVER_INVENTORY_SOURCE_SHA": "a" * 40,
    "SQLSERVER_CORRELATION_ID": "sql-enterprise-test-20261019",
    "SQLSERVER_HML_DSN": (
        "Driver={ODBC Driver 18 for SQL Server};Server=fixture-server;"
        "Database=fixture_hml;Encrypt=Yes;TrustServerCertificate=no;"
        "ApplicationIntent=ReadOnly;Trusted_Connection=Yes"
    ),
}


class Connection:
    def __init__(self):
        self.rows = {
            inventory.VERSION_SQL: ("17.0.5015.4", "Developer Edition",
                                    "fixture-server", "fixture_hml", 0, 0, 0, 0),
            inventory.COMPAT_SQL: (170,),
            inventory.QS_SQL: ("READ_WRITE", "READ_WRITE", 0),
            inventory.TLS_SQL: ("TRUE",),
            suite.SETTINGS_SQL: (0, 1),
            **{sql: expected for _, sql, expected in suite.JSON_CHECKS + suite.NATIVE_JSON_CHECKS},
        }
        self.errors = {sql: RuntimeError(f"42000 ({code})")
                       for _, sql, code in suite.NEGATIVE_CHECKS + suite.NATIVE_JSON_NEGATIVE}
        self.closed = False
        self.executed = []
        self.extra_row = False
        self.timeout = None

    def cursor(self):
        owner = self

        class Cursor:
            def execute(self, sql):
                owner.executed.append(sql)
                self.sql = sql
                self.fetched = False
                if sql in owner.errors:
                    raise owner.errors[sql]

            def fetchone(self):
                if self.fetched:
                    return owner.rows.get(self.sql) if owner.extra_row else None
                self.fetched = True
                return owner.rows.get(self.sql)

            def close(self):
                pass

        return Cursor()

    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(ENV)
        self.connections = [Connection(), Connection()]
        self.calls = []

    def connect(self, dsn, **kwargs):
        self.calls.append(kwargs)
        return self.connections[len(self.calls) - 1]

    def test_positive_two_connections_and_no_hml_claim(self):
        result = suite.probe(self.env, self.connect)
        self.assertEqual(2, result["connections_verified"])
        self.assertEqual(11, len(result["checks"]))
        self.assertTrue(result["openjson_runtime_verified"])
        self.assertFalse(result["hml_verified"])
        self.assertFalse(result["mutations_executed"])
        self.assertTrue(all(c.closed and c.timeout == 15 for c in self.connections))
        self.assertEqual([{"timeout": 8, "autocommit": True}] * 2, self.calls)
        text = json.dumps(result)
        self.assertNotIn("fixture-server", text)
        self.assertNotIn("fixture_hml", text)

    def test_prod_blocked_before_connection(self):
        self.env["SQLSERVER_INVENTORY_ENV"] = "PROD"
        with self.assertRaisesRegex(inventory.InventoryBlocked, "environment_not_hml"):
            suite.probe(self.env, self.connect)
        self.assertEqual([], self.calls)

    def test_missing_dsn_before_connection(self):
        self.env.pop("SQLSERVER_HML_DSN")
        with self.assertRaisesRegex(inventory.InventoryBlocked, "dsn_unavailable"):
            suite.probe(self.env, self.connect)
        self.assertEqual([], self.calls)

    def test_untrusted_certificate_before_connection(self):
        self.env["SQLSERVER_HML_DSN"] = self.env["SQLSERVER_HML_DSN"].replace(
            "TrustServerCertificate=no", "TrustServerCertificate=yes")
        with self.assertRaises(inventory.InventoryBlocked):
            suite.probe(self.env, self.connect)
        self.assertEqual([], self.calls)

    def test_privileged_identity_closes_without_json(self):
        row = list(self.connections[0].rows[inventory.VERSION_SQL])
        row[4] = 1
        self.connections[0].rows[inventory.VERSION_SQL] = tuple(row)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "privileged_identity"):
            suite.probe(self.env, self.connect)
        self.assertEqual([inventory.VERSION_SQL], self.connections[0].executed)
        self.assertTrue(self.connections[0].closed)

    def test_target_mismatch_blocks_second_connection(self):
        row = list(self.connections[1].rows[inventory.VERSION_SQL])
        row[2] = "another-server"
        self.connections[1].rows[inventory.VERSION_SQL] = tuple(row)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "resolved_target_mismatch"):
            suite.probe(self.env, self.connect)
        self.assertTrue(all(c.closed for c in self.connections))

    def test_unencrypted_session_before_json(self):
        self.connections[0].rows[inventory.TLS_SQL] = ("FALSE",)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "tls_not_verified"):
            suite.probe(self.env, self.connect)
        self.assertNotIn(suite.SETTINGS_SQL, self.connections[0].executed)

    def test_json_wrong_value_fails_and_closes(self):
        self.connections[0].rows[suite.JSON_CHECKS[0][1]] = (999,)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "json_result_mismatch"):
            suite.probe(self.env, self.connect)
        self.assertTrue(self.connections[0].closed)

    def test_array_order_change_is_detected(self):
        self.connections[0].rows[suite.JSON_CHECKS[2][1]] = (3, 30, 20, 10)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "json_result_mismatch"):
            suite.probe(self.env, self.connect)

    def test_extra_result_row_rejected(self):
        self.connections[0].extra_row = True
        with self.assertRaisesRegex(inventory.InventoryBlocked, "unexpected_result_cardinality"):
            suite.probe(self.env, self.connect)

    def test_missing_result_row_rejected(self):
        self.connections[0].rows[suite.SETTINGS_SQL] = None
        with self.assertRaisesRegex(inventory.InventoryBlocked, "unexpected_result_cardinality"):
            suite.probe(self.env, self.connect)

    def test_negative_control_that_does_not_fail_is_rejected(self):
        self.connections[0].errors.pop(suite.NEGATIVE_CHECKS[0][1])
        with self.assertRaisesRegex(inventory.InventoryBlocked, "negative_control_not_rejected"):
            suite.probe(self.env, self.connect)

    def test_wrong_error_does_not_count_as_expected_rejection(self):
        self.connections[0].errors[suite.NEGATIVE_CHECKS[0][1]] = RuntimeError("timeout (258)")
        with self.assertRaisesRegex(inventory.InventoryBlocked, "negative_control_unclassified"):
            suite.probe(self.env, self.connect)

    def test_build_drift_rejected(self):
        row = list(self.connections[1].rows[inventory.VERSION_SQL])
        row[0] = "17.0.5005.3"
        self.connections[1].rows[inventory.VERSION_SQL] = tuple(row)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "independent_readback_mismatch"):
            suite.probe(self.env, self.connect)

    def test_settings_drift_rejected(self):
        self.connections[1].rows[suite.SETTINGS_SQL] = (0, 0)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "independent_readback_mismatch"):
            suite.probe(self.env, self.connect)

    def test_compatibility_exception_needs_runtime_result(self):
        for connection in self.connections:
            connection.rows[inventory.COMPAT_SQL] = (120,)
            connection.rows[suite.SETTINGS_SQL] = (1, None)
        result = suite.probe(self.env, self.connect)
        self.assertFalse(result["inventory"]["openjson_compatible"])
        self.assertTrue(result["openjson_runtime_verified"])

    def test_repeat_is_deterministic(self):
        first = suite.probe(self.env, self.connect)
        self.connections = [Connection(), Connection()]
        self.calls = []
        self.assertEqual(first, suite.probe(self.env, self.connect))

    def test_all_queries_are_fixed_readonly_statements(self):
        sqls = [suite.SETTINGS_SQL] + [sql for _, sql, _ in
                                     suite.JSON_CHECKS + suite.NEGATIVE_CHECKS +
                                     suite.NATIVE_JSON_CHECKS + suite.NATIVE_JSON_NEGATIVE]
        for sql in sqls:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
            self.assertIsNone(re.search(
                r"\b(ALTER|CREATE|DROP|INSERT|UPDATE|DELETE|MERGE|EXEC|BACKUP|RESTORE|GRANT)\b",
                sql, re.I))

    def test_native_json_wrong_value_blocks(self):
        self.connections[0].rows[suite.NATIVE_JSON_CHECKS[0][1]] = ('{"id":8}',)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "native_json_result_mismatch"):
            suite.probe(self.env, self.connect)

    def test_pre2025_does_not_run_native_type(self):
        for connection in self.connections:
            row = list(connection.rows[inventory.VERSION_SQL])
            row[0] = "16.0.4305.5"
            connection.rows[inventory.VERSION_SQL] = tuple(row)
        result = suite.probe(self.env, self.connect)
        self.assertEqual(8, len(result["checks"]))
        self.assertNotIn(suite.NATIVE_JSON_CHECKS[0][1], self.connections[0].executed)

    def test_malformed_settings_blocks(self):
        self.connections[0].rows[suite.SETTINGS_SQL] = ("yes", 1)
        with self.assertRaisesRegex(inventory.InventoryBlocked, "settings_invalid"):
            suite.probe(self.env, self.connect)

    def test_timeout_failure_closes_connection(self):
        class NoTimeout(Connection):
            def __setattr__(self, key, value):
                if key == "timeout" and value is not None:
                    raise RuntimeError("cannot set timeout")
                super().__setattr__(key, value)
        self.connections[0] = NoTimeout()
        with self.assertRaisesRegex(inventory.InventoryBlocked, "query_timeout_unavailable"):
            suite.probe(self.env, self.connect)
        self.assertTrue(self.connections[0].closed)
        self.assertEqual([], self.connections[0].executed)

    def test_main_sanitizes_driver_error(self):
        self.connections[0].errors[suite.SETTINGS_SQL] = RuntimeError("private-string-never-emit")
        driver = types.SimpleNamespace(connect=self.connect, pooling=True)
        with patch.dict(os.environ, self.env, clear=True), patch.dict(
            sys.modules, {"pyodbc": driver}), contextlib.redirect_stdout(io.StringIO()) as output:
            code = suite.main()
        self.assertEqual(2, code)
        self.assertNotIn("private-string", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["hml_verified"])
        self.assertFalse(driver.pooling)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.sha = ENV["SQLSERVER_INVENTORY_SOURCE_SHA"]
        self.correlation = ENV["SQLSERVER_CORRELATION_ID"]
        self.receipts = {gate: {
            "source_sha": self.sha, "correlation_id": self.correlation, "scope": "HML",
            "artifact_sha256": "b" * 64, "readback_sha256": "c" * 64, "result": "PASS",
        } for gate in suite.ENTERPRISE_GATES}

    def assess(self):
        return suite.coverage(self.receipts, self.sha, self.correlation)

    def test_all_receipts_still_need_independent_review(self):
        result = self.assess()
        self.assertEqual("REVIEW_REQUIRED", result["status"])
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["hml_verified"])
        self.assertFalse(result["evidence_authenticity_verified"])

    def test_missing_gate_blocks(self):
        self.receipts.pop(suite.ENTERPRISE_GATES[0])
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_old_sha_blocks(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["source_sha"] = "d" * 40
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_wrong_correlation_blocks(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["correlation_id"] = "another-run"
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_ci_cannot_pass_as_hml(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["scope"] = "CI"
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_skipped_is_not_success(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["result"] = "skipped"
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_missing_readback_blocks(self):
        self.receipts[suite.ENTERPRISE_GATES[0]].pop("readback_sha256")
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_fail_blocks(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["result"] = "FAIL"
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_not_applicable_requires_reason_and_review(self):
        item = self.receipts[suite.ENTERPRISE_GATES[0]]
        item["result"] = "NOT_APPLICABLE"
        self.assertEqual("BLOCKED", self.assess()["status"])
        item.update(applicability_reason="No Azure resources in approved inventory",
                    review_reference="fixture-review-only")
        self.assertEqual("REVIEW_REQUIRED", self.assess()["status"])

    def test_malformed_receipts_rejected(self):
        with self.assertRaisesRegex(inventory.InventoryBlocked, "receipts_invalid"):
            suite.coverage([], self.sha, self.correlation)

    def test_malformed_result_blocks(self):
        self.receipts[suite.ENTERPRISE_GATES[0]]["result"] = ["PASS"]
        self.assertEqual("BLOCKED", self.assess()["status"])

    def test_unknown_gate_rejected(self):
        self.receipts["typo"] = {}
        with self.assertRaisesRegex(inventory.InventoryBlocked, "unknown_gate"):
            self.assess()

    def test_malformed_sha_rejected(self):
        with self.assertRaisesRegex(inventory.InventoryBlocked, "source_sha_invalid"):
            suite.coverage({}, "main", self.correlation)

    def test_repeat_does_not_mutate_receipts(self):
        before = json.dumps(self.receipts, sort_keys=True)
        self.assertEqual(self.assess(), self.assess())
        self.assertEqual(before, json.dumps(self.receipts, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
