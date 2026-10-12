"""Read-only SQL Server HML metadata probe; never print a DSN or host/database names."""
from __future__ import annotations
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping

VERSION_SQL = """SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)),
 CAST(SERVERPROPERTY('Edition') AS nvarchar(128)),
 CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)),
 DB_NAME(), IS_SRVROLEMEMBER(N'sysadmin'), IS_MEMBER(N'db_owner'),
 IS_MEMBER(N'db_datawriter'), IS_MEMBER(N'db_ddladmin')"""
COMPAT_SQL = "SELECT compatibility_level FROM sys.databases WHERE database_id = DB_ID()"
QS_SQL = ("SELECT actual_state_desc, desired_state_desc, readonly_reason "
          "FROM sys.database_query_store_options")
TLS_SQL = "SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = @@SPID"
_DRIVERS = {"ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"}

class InventoryBlocked(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:16]

def _dsn_fields(dsn: str) -> dict[str, str]:
    rest = dsn.replace("{ODBC Driver 17 for SQL Server}", "").replace(
        "{ODBC Driver 18 for SQL Server}", "")
    if "{" in rest or "}" in rest:
        raise InventoryBlocked("dsn_requires_simple_fields")
    fields: dict[str, str] = {}
    for part in dsn.strip().strip(";").split(";"):
        if "=" not in part:
            raise InventoryBlocked("dsn_format_invalid")
        key, value = (p.strip() for p in part.split("=", 1))
        key = key.casefold()
        if not key or not value or key in fields:
            raise InventoryBlocked("dsn_fields_invalid")
        fields[key] = value
    return fields

def _validate_scope(env: Mapping[str, str]) -> str:
    if env.get("SQLSERVER_INVENTORY_ENV") != "HML":
        raise InventoryBlocked("environment_not_hml")
    if env.get("SQLSERVER_INVENTORY_APPROVED") != "HML-READONLY":
        raise InventoryBlocked("authorization_marker_missing")
    if not env.get("SQLSERVER_EXPECTED_SERVER", "").strip() or not env.get(
        "SQLSERVER_EXPECTED_DATABASE", "").strip():
        raise InventoryBlocked("expected_target_missing")
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("SQLSERVER_INVENTORY_SOURCE_SHA", "")):
        raise InventoryBlocked("source_sha_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", env.get("SQLSERVER_CORRELATION_ID", "")):
        raise InventoryBlocked("correlation_id_invalid")
    dsn = env.get("SQLSERVER_HML_DSN", "").strip()
    if not dsn:
        raise InventoryBlocked("dsn_unavailable")
    f = _dsn_fields(dsn)
    if f.get("driver", "").strip("{}") not in _DRIVERS:
        raise InventoryBlocked("odbc_driver_not_allowed")
    if f.get("encrypt", "").casefold() not in {"yes", "true", "mandatory", "strict"}:
        raise InventoryBlocked("tls_encryption_not_required")
    if f.get("trustservercertificate", "").casefold() not in {"no", "false"}:
        raise InventoryBlocked("certificate_validation_not_required")
    if f.get("applicationintent", "").casefold() != "readonly":
        raise InventoryBlocked("readonly_intent_missing")
    if not f.get("server") or not f.get("database"):
        raise InventoryBlocked("dsn_target_missing")
    if f["database"].casefold() != env["SQLSERVER_EXPECTED_DATABASE"].casefold():
        raise InventoryBlocked("dsn_database_mismatch")
    if f["database"].casefold() in {"master", "model", "msdb", "tempdb"}:
        raise InventoryBlocked("system_database_not_allowed")
    return dsn

def _one(conn: object, statement: str) -> tuple:
    cur = conn.cursor()
    try:
        cur.execute(statement)
        row = cur.fetchone()
        if row is None:
            raise InventoryBlocked("metadata_missing")
        return tuple(row)
    finally:
        cur.close()


def collect(env: Mapping[str, str], connect: Callable[..., object]) -> dict:
    """Collect only metadata, after explicit HML configuration and target checks."""
    dsn = _validate_scope(env)  # No connection before static scope validation.
    conn = connect(dsn, timeout=8, autocommit=True)
    try:
        product, edition, server, db, admin, owner, writer, ddladmin = _one(
            conn, VERSION_SQL)
        if (str(server or "").casefold() != env["SQLSERVER_EXPECTED_SERVER"].casefold()
                or str(db or "").casefold()
                != env["SQLSERVER_EXPECTED_DATABASE"].casefold()):
            raise InventoryBlocked("resolved_target_mismatch")
        if any(x != 0 for x in (admin, owner, writer, ddladmin)):
            raise InventoryBlocked("privileged_identity_not_allowed")
        (compat_level,) = _one(conn, COMPAT_SQL)
        if not isinstance(compat_level, int) or compat_level < 100:
            raise InventoryBlocked("compatibility_level_invalid")
        actual, desired, reason = _one(conn, QS_SQL)
        (encrypted,) = _one(conn, TLS_SQL)
        if str(encrypted).upper() not in {"TRUE", "YES", "1"}:
            raise InventoryBlocked("tls_not_verified")
        return {
            "schema_version": "1.0",
            "status": "observed",
            "scope": "HML",
            "server_fingerprint": _fingerprint(str(server)),
            "database_fingerprint": _fingerprint(str(db)),
            "product_version": str(product),
            "edition": str(edition),
            "compatibility_level": compat_level,
            "openjson_compatible": compat_level >= 130,
            "query_store_actual": str(actual),
            "query_store_desired": str(desired),
            "query_store_readonly_reason": int(reason),
            "tls_encrypted": True,
            "identity_not_admin": True,
            "application_intent": "ReadOnly",
            "source_sha": env["SQLSERVER_INVENTORY_SOURCE_SHA"],
            "correlation_id": env["SQLSERVER_CORRELATION_ID"],
        }
    finally:
        conn.close()

def main() -> int:
    env = os.environ
    try:
        _validate_scope(env)
        import pyodbc
        result = collect(env, pyodbc.connect)
    except InventoryBlocked as exc:
        result = {"status": "blocked", "code": exc.code}
    except Exception:
        # ODBC errors can embed credentials, names and connection strings.
        result = {"status": "blocked", "code": "connection_or_metadata_error"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "observed" else 2

if __name__ == "__main__":
    raise SystemExit(main())
