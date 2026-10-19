"""Read-only SQL Server HML checks. Never installs, patches, or promotes an instance."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from decimal import Decimal

from scripts.sqlserver_hml_inventory import InventoryBlocked, collect

QUERY_TIMEOUT_SECONDS = 15
SETTINGS_SQL = """SELECT
 MAX(CASE WHEN name = 'ALLOW_BUILTIN_TVF_IN_ALL_COMPAT_LEVELS'
     THEN CONVERT(int, value) END),
 MAX(CASE WHEN name = 'OPTIONAL_PARAMETER_OPTIMIZATION'
     THEN CONVERT(int, value) END)
 FROM sys.database_scoped_configurations"""
JSON_CHECKS = (
    ("explicit_schema_unicode", """SELECT id, label, amount, active, items
 FROM OPENJSON(N'[{"id":1,"label":"ação","amount":12.50,"active":true,"items":[1,2]}]')
 WITH (id int '$.id', label nvarchar(20) '$.label',
 amount decimal(10,2) '$.amount', active bit '$.active',
 items nvarchar(max) '$.items' AS JSON)""",
     (1, "ação", Decimal("12.50"), True, "[1,2]")),
    ("null_and_missing", """SELECT absent, nullable FROM OPENJSON(N'{"nullable":null}')
 WITH (absent int '$.absent', nullable int '$.nullable')""", (None, None)),
    ("array_order_and_values", """SELECT COUNT_BIG(*),
 MAX(CASE WHEN [key] = N'0' THEN CONVERT(int, value) END),
 MAX(CASE WHEN [key] = N'1' THEN CONVERT(int, value) END),
 MAX(CASE WHEN [key] = N'2' THEN CONVERT(int, value) END)
 FROM OPENJSON(N'[10,20,30]')""", (3, 10, 20, 30)),
    ("case_sensitive_path", """SELECT id FROM OPENJSON(N'{"Id":7}')
 WITH (id int '$.id')""", (None,)),
    ("invalid_document_detection", """SELECT ISJSON(N'{"id":')""", (0,)),
)
NEGATIVE_CHECKS = (
    ("malformed_json_rejected", """SELECT * FROM OPENJSON(N'{"id":')""", 13609),
    ("strict_missing_rejected", """SELECT id FROM OPENJSON(N'{}')
 WITH (id int 'strict $.id')""", 13608),
    ("invalid_integer_rejected", """SELECT id FROM OPENJSON(N'{"id":"not-an-integer"}')
 WITH (id int '$.id')""", 245),
)
NATIVE_JSON_CHECKS = (
    ("native_json_roundtrip", """SELECT
 CAST(CAST(N'{"id":7}' AS json) AS nvarchar(max))""", ('{"id":7}',)),
    ("native_json_value", """SELECT
 JSON_VALUE(CAST(N'{"id":7}' AS json), '$.id')""", ("7",)),
)
NATIVE_JSON_NEGATIVE = (
    ("native_json_scalar_rejected", """SELECT CAST(N'123' AS json)""", 13609),
)
# Missing or inapplicable receipts must never be converted to successful tests.
ENTERPRISE_GATES = (
    "build_servicing_and_support", "cu_known_issue_regressions",
    "json_contract_and_ingestion_replay", "native_json_and_index_benchmark",
    "compatibility_160_170_oppo", "concurrency_and_engine_performance",
    "tls_authentication_and_least_privilege", "drivers_and_integrations",
    "azure_sql_applicability_and_migration", "tooling_and_agent_permissions",
    "backup_restore_and_ha", "business_e2e_and_rollback",
)


def _one(connection: object, sql: str) -> tuple:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        if row is None or cursor.fetchone() is not None:
            raise InventoryBlocked("unexpected_result_cardinality")
        return tuple(row)
    finally:
        cursor.close()


def _negative(connection: object, sql: str, expected_code: int) -> None:
    cursor = connection.cursor()
    try:
        try:
            cursor.execute(sql)
            cursor.fetchone()
        except Exception as exc:
            # Inspect only to classify a known engine error. Never emit the text.
            text = " ".join(str(value) for value in getattr(exc, "args", ()))
            if not re.search(r"\(" + str(expected_code) + r"\)", text):
                raise InventoryBlocked("negative_control_unclassified") from None
        else:
            raise InventoryBlocked("negative_control_not_rejected")
    finally:
        cursor.close()


def probe(env: Mapping[str, str], connect: Callable[..., object]) -> dict:
    """Run fixed read-only queries on two independently opened connections.

    collect() validates HML, target, driver, TLS, and non-admin role membership.
    No arbitrary SQL, host arguments, DDL/DML, business data, or raw errors.
    """
    captured: list = []

    def bounded_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        try:
            connection.timeout = QUERY_TIMEOUT_SECONDS
        except Exception:
            connection.close()
            raise InventoryBlocked("query_timeout_unavailable") from None
        return connection

    # Inventory is independently repeated; the underlying collector closes both.
    # JSON checks run before close but only after collect has validated identity/TLS.
    def observed_connect(*args, **kwargs):
        connection = bounded_connect(*args, **kwargs)

        class HeldConnection:
            def cursor(self):
                return connection.cursor()

            def close(self):
                # Delay close until probe controls finish; outer finally owns it.
                return None

        captured.append(connection)
        return HeldConnection()

    observations = []
    settings = []
    checked_names = []
    try:
        for _ in range(2):
            observation = collect(env, observed_connect)
            connection = captured[-1]
            setting_row = _one(connection, SETTINGS_SQL)
            if len(setting_row) != 2 or any(
                value is not None and (type(value) is not int or value not in (0, 1))
                for value in setting_row
            ):
                raise InventoryBlocked("settings_invalid")
            settings.append(setting_row)
            for _, sql, expected in JSON_CHECKS:
                if _one(connection, sql) != expected:
                    raise InventoryBlocked("json_result_mismatch")
            for _, sql, code in NEGATIVE_CHECKS:
                _negative(connection, sql, code)
            names = [name for name, *_ in JSON_CHECKS + NEGATIVE_CHECKS]
            major = str(observation["product_version"]).split(".", 1)[0]
            if major == "17":
                for name, sql, expected in NATIVE_JSON_CHECKS:
                    if _one(connection, sql) != expected:
                        raise InventoryBlocked("native_json_result_mismatch")
                    names.append(name)
                for name, sql, code in NATIVE_JSON_NEGATIVE:
                    _negative(connection, sql, code)
                    names.append(name)
            checked_names.append(names)
            observations.append(observation)
            connection.close()
            captured.pop()
        if observations[0] != observations[1] or settings[0] != settings[1]:
            raise InventoryBlocked("independent_readback_mismatch")
        return {
            "schema_version": "1.0",
            "status": "observed",
            "scope": "HML",
            "source_sha": observations[0]["source_sha"],
            "correlation_id": observations[0]["correlation_id"],
            "inventory": observations[0],
            "query_timeout_seconds": QUERY_TIMEOUT_SECONDS,
            "connections_verified": 2,
            "openjson_runtime_verified": True,
            "allow_builtin_tvf_all_compat_levels": settings[0][0],
            "optional_parameter_optimization": settings[0][1],
            "checks": checked_names[0],
            "independent_readback_verified": True,
            "hml_verified": False,
            "enterprise_status": "PARTIAL",
            "pending_enterprise_gates": list(ENTERPRISE_GATES),
            "mutations_executed": False,
        }
    finally:
        for connection in captured:
            connection.close()


def coverage(receipts: Mapping[str, object], source_sha: str,
             correlation_id: str) -> dict:
    """Structural coverage only, not authentication or verification of evidence.

    A complete receipt set produces REVIEW_REQUIRED, never permission to deploy.
    Actual artifacts, signatures, results, and version must be independently read.
    """
    if not isinstance(receipts, Mapping):
        raise InventoryBlocked("receipts_invalid")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise InventoryBlocked("source_sha_invalid")
    if not isinstance(correlation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", correlation_id):
        raise InventoryBlocked("correlation_id_invalid")
    if set(receipts) - set(ENTERPRISE_GATES):
        raise InventoryBlocked("unknown_gate")
    states = {}
    for gate in ENTERPRISE_GATES:
        receipt = receipts.get(gate)
        if not isinstance(receipt, Mapping):
            states[gate] = "BLOCKED"
            continue
        linked = (
            receipt.get("source_sha") == source_sha
            and receipt.get("correlation_id") == correlation_id
            and receipt.get("scope") == "HML"
            and bool(re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("artifact_sha256", ""))))
            and bool(re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("readback_sha256", ""))))
        )
        result = receipt.get("result")
        if not linked or not isinstance(result, str) or result not in {"PASS", "FAIL", "NOT_APPLICABLE"}:
            states[gate] = "BLOCKED"
        elif result == "NOT_APPLICABLE" and not (
            receipt.get("applicability_reason") and receipt.get("review_reference")
        ):
            states[gate] = "BLOCKED"
        else:
            states[gate] = result
    ready = all(state in {"PASS", "NOT_APPLICABLE"} for state in states.values())
    return {
        "status": "REVIEW_REQUIRED" if ready else "BLOCKED",
        "states": states,
        "source_sha": source_sha,
        "correlation_id": correlation_id,
        "evidence_authenticity_verified": False,
        "promotion_allowed": False,
        "hml_verified": False,
    }


def main() -> int:
    try:
        # Delay driver import until collect's complete static preflight succeeds.
        from scripts.sqlserver_hml_inventory import _validate_scope
        _validate_scope(os.environ)
        import pyodbc
        pyodbc.pooling = False  # Two separate connections, not pooled sessions.
        result = probe(os.environ, pyodbc.connect)
    except InventoryBlocked as exc:
        result = {"status": "blocked", "code": exc.code, "hml_verified": False}
    except Exception:
        result = {"status": "blocked", "code": "connection_or_probe_error",
                  "hml_verified": False}
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0 if result["status"] == "observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
