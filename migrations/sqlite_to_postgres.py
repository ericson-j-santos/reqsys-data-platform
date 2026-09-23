#!/usr/bin/env python3
"""Fail-closed SQLite -> PostgreSQL migration for isolated local/DEV/CI schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

SAFE_ENVIRONMENTS = {"local", "dev", "ci", "test"}
SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH_SIZE_DEFAULT = 500


class PreflightError(ValueError):
    """Raised when migration preconditions are not safe or supported."""


class MigrationConflictError(RuntimeError):
    """Raised when an idempotency key is replayed with different source state."""


class VerificationError(RuntimeError):
    """Raised when post-migration evidence differs from the source."""


@dataclass(frozen=True)
class ColumnContract:
    name: str
    pg_type: str
    nullable: bool
    pk_order: int


@dataclass(frozen=True)
class TableContract:
    name: str
    columns: tuple[ColumnContract, ...]

    @property
    def primary_key(self) -> tuple[str, ...]:
        ordered = sorted(
            (column for column in self.columns if column.pk_order > 0),
            key=lambda column: column.pk_order,
        )
        return tuple(column.name for column in ordered)


def _load_psycopg() -> tuple[Any, Any]:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for PostgreSQL migration; install requirements-ci.txt"
        ) from exc
    return psycopg, sql


def _quote_sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_connect_readonly(source: Path) -> sqlite3.Connection:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return connection


def _map_type(declared_type: str) -> str:
    normalized = declared_type.strip().upper()
    if not normalized:
        raise PreflightError("columns without an explicit SQLite type are not supported")
    if "BOOL" in normalized:
        return "BOOLEAN"
    if "INT" in normalized:
        return "BIGINT"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT", "VARCHAR")):
        return "TEXT"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in normalized:
        return "BYTEA"
    if any(token in normalized for token in ("NUMERIC", "DECIMAL")):
        return "NUMERIC"
    raise PreflightError(f"unsupported SQLite declared type: {declared_type}")


def _table_contract(connection: sqlite3.Connection, table: str) -> TableContract:
    quoted = _quote_sqlite_identifier(table)
    raw_columns = connection.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
    if not raw_columns:
        raise PreflightError(f"table has no visible columns: {table}")

    columns: list[ColumnContract] = []
    for _cid, name, declared_type, notnull, default_value, pk_order, hidden in raw_columns:
        if hidden:
            raise PreflightError(f"generated/hidden columns are not supported: {table}.{name}")
        if default_value is not None:
            raise PreflightError(
                f"SQLite defaults require an explicit compatibility contract: {table}.{name}"
            )
        columns.append(
            ColumnContract(
                name=name,
                pg_type=_map_type(declared_type),
                nullable=not bool(notnull) and not bool(pk_order),
                pk_order=int(pk_order),
            )
        )

    contract = TableContract(name=table, columns=tuple(columns))
    if not contract.primary_key:
        raise PreflightError(f"table requires a primary key for idempotent migration: {table}")

    if connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
        raise PreflightError(
            f"foreign keys require an explicit ordering/constraint contract: {table}"
        )

    indexes = connection.execute(f"PRAGMA index_list({quoted})").fetchall()
    unsupported = [row for row in indexes if len(row) >= 4 and row[3] != "pk"]
    if unsupported:
        raise PreflightError(
            f"secondary indexes require an explicit compatibility contract: {table}"
        )
    return contract


def _source_contract(source: Path) -> tuple[sqlite3.Connection, tuple[TableContract, ...]]:
    connection = _sqlite_connect_readonly(source)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise PreflightError(f"SQLite integrity check failed: {integrity}")

        table_names = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        if not table_names:
            raise PreflightError("SQLite source contains no user tables")

        contracts = tuple(_table_contract(connection, table) for table in table_names)
        return connection, contracts
    except Exception:
        connection.close()
        raise


def _canonical_value(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "BOOLEAN":
        return bool(value)
    if pg_type == "BIGINT":
        return int(value)
    if pg_type == "DOUBLE PRECISION":
        return format(float(value), ".17g")
    if pg_type == "NUMERIC":
        decimal = Decimal(str(value))
        if decimal == 0:
            return "0"
        return format(decimal.normalize(), "f")
    if pg_type == "BYTEA":
        return bytes(value).hex()
    return str(value)


def _coerce_for_postgres(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "BOOLEAN":
        return bool(value)
    if pg_type == "BIGINT":
        return int(value)
    if pg_type == "DOUBLE PRECISION":
        return float(value)
    if pg_type == "NUMERIC":
        return Decimal(str(value))
    if pg_type == "BYTEA":
        return bytes(value)
    return str(value)


def _hash_rows(
    rows: Iterable[tuple[Any, ...]],
    columns: tuple[ColumnContract, ...],
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        canonical = [
            _canonical_value(value, column.pg_type)
            for value, column in zip(row, columns, strict=True)
        ]
        digest.update(
            json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _sqlite_rows(
    connection: sqlite3.Connection,
    contract: TableContract,
) -> Iterable[tuple[Any, ...]]:
    columns = ", ".join(_quote_sqlite_identifier(c.name) for c in contract.columns)
    order = ", ".join(_quote_sqlite_identifier(name) for name in contract.primary_key)
    cursor = connection.execute(
        f"SELECT {columns} FROM {_quote_sqlite_identifier(contract.name)} ORDER BY {order}"
    )
    yield from cursor


def _source_evidence(
    connection: sqlite3.Connection,
    contracts: tuple[TableContract, ...],
) -> tuple[dict[str, dict[str, Any]], str]:
    tables: dict[str, dict[str, Any]] = {}
    overall = hashlib.sha256()
    for contract in contracts:
        count, row_hash = _hash_rows(_sqlite_rows(connection, contract), contract.columns)
        schema_payload = {
            "name": contract.name,
            "columns": [
                {
                    "name": column.name,
                    "pg_type": column.pg_type,
                    "nullable": column.nullable,
                    "pk_order": column.pk_order,
                }
                for column in contract.columns
            ],
        }
        tables[contract.name] = {
            "source_count": count,
            "source_sha256": row_hash,
            "schema": schema_payload,
        }
        overall.update(json.dumps(schema_payload, sort_keys=True).encode("utf-8"))
        overall.update(row_hash.encode("ascii"))
    return tables, overall.hexdigest()


def _expected_pg_signature(contract: TableContract) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            column.name,
            column.pg_type.lower(),
            "YES" if column.nullable else "NO",
        )
        for column in contract.columns
    )


def _postgres_table_signature(
    pg: Any,
    schema: str,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    rows = pg.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _postgres_primary_key(pg: Any, schema: str, table: str) -> tuple[str, ...]:
    rows = pg.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = %s AND t.relname = %s AND i.indisprimary
        ORDER BY k.ord
        """,
        (schema, table),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _assert_existing_table_compatible(
    pg: Any,
    schema: str,
    contract: TableContract,
) -> None:
    signature = _postgres_table_signature(pg, schema, contract.name)
    if not signature:
        return
    expected = _expected_pg_signature(contract)
    if signature != expected:
        raise PreflightError(
            f"destination table schema mismatch for {schema}.{contract.name}: "
            f"expected={expected!r} observed={signature!r}"
        )
    observed_pk = _postgres_primary_key(pg, schema, contract.name)
    if observed_pk != contract.primary_key:
        raise PreflightError(
            f"destination primary key mismatch for {schema}.{contract.name}: "
            f"expected={contract.primary_key!r} observed={observed_pk!r}"
        )


def preflight_migration(
    source: str | Path,
    postgres_dsn: str,
    schema: str,
    *,
    environment: str,
) -> dict[str, Any]:
    if environment not in SAFE_ENVIRONMENTS:
        raise PreflightError(
            f"environment must be one of {sorted(SAFE_ENVIRONMENTS)}; "
            "HML/STG/PROD require a separately governed cutover"
        )
    if not SAFE_SCHEMA.fullmatch(schema):
        raise PreflightError(f"invalid PostgreSQL schema name: {schema!r}")
    if not postgres_dsn.strip():
        raise PreflightError("PostgreSQL DSN is required")

    source_path = Path(source).resolve()
    sqlite_db, contracts = _source_contract(source_path)
    try:
        source_tables, source_fingerprint = _source_evidence(sqlite_db, contracts)
    finally:
        sqlite_db.close()

    psycopg, _sql = _load_psycopg()
    with psycopg.connect(postgres_dsn) as pg:
        database, server_version, can_create = pg.execute(
            """
            SELECT current_database(),
                   current_setting('server_version'),
                   has_database_privilege(current_user, current_database(), 'CREATE')
            """
        ).fetchone()
        schema_exists = bool(
            pg.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                (schema,),
            ).fetchone()[0]
        )
        if schema_exists:
            can_create_in_schema = bool(
                pg.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'CREATE')",
                    (schema,),
                ).fetchone()[0]
            )
            if not can_create_in_schema:
                raise PreflightError(
                    f"PostgreSQL user lacks CREATE privilege on existing schema: {schema}"
                )
        elif not can_create:
            raise PreflightError(
                "PostgreSQL user lacks CREATE privilege on target database"
            )

        for contract in contracts:
            _assert_existing_table_compatible(pg, schema, contract)

    return {
        "status": "preflight_passed",
        "environment": environment,
        "source": str(source_path),
        "schema": schema,
        "destination_database": database,
        "destination_server_version": server_version,
        "source_fingerprint": source_fingerprint,
        "tables": source_tables,
    }


def _create_schema_and_audit(pg: Any, sql: Any, schema: str) -> None:
    pg.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    pg.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.{} (
                correlation_id TEXT PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                automation_sha TEXT NOT NULL,
                environment TEXT NOT NULL,
                run_count BIGINT NOT NULL,
                status TEXT NOT NULL,
                evidence_json JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        ).format(sql.Identifier(schema), sql.Identifier("_migration_runs"))
    )


def _create_target_table(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> None:
    definitions: list[Any] = []
    for column in contract.columns:
        parts = [sql.Identifier(column.name), sql.SQL(column.pg_type)]
        if not column.nullable:
            parts.append(sql.SQL("NOT NULL"))
        definitions.append(sql.SQL(" ").join(parts))

    definitions.append(
        sql.SQL("PRIMARY KEY ({})").format(
            sql.SQL(", ").join(
                sql.Identifier(name) for name in contract.primary_key
            )
        )
    )
    pg.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
            sql.Identifier(schema),
            sql.Identifier(contract.name),
            sql.SQL(", ").join(definitions),
        )
    )
    _assert_existing_table_compatible(pg, schema, contract)


def _upsert_source_rows(
    sqlite_db: sqlite3.Connection,
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
    *,
    batch_size: int,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    column_names = [column.name for column in contract.columns]
    non_pk = [name for name in column_names if name not in contract.primary_key]
    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(contract.name),
        sql.SQL(", ").join(sql.Identifier(name) for name in column_names),
        sql.SQL(", ").join(sql.Placeholder() for _ in column_names),
    )
    statement += sql.SQL(" ON CONFLICT ({}) ").format(
        sql.SQL(", ").join(
            sql.Identifier(name) for name in contract.primary_key
        )
    )
    if non_pk:
        statement += sql.SQL("DO UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(name),
                    sql.Identifier(name),
                )
                for name in non_pk
            )
        )
    else:
        statement += sql.SQL("DO NOTHING")

    columns = ", ".join(_quote_sqlite_identifier(c.name) for c in contract.columns)
    order = ", ".join(_quote_sqlite_identifier(name) for name in contract.primary_key)
    cursor = sqlite_db.execute(
        f"SELECT {columns} FROM {_quote_sqlite_identifier(contract.name)} ORDER BY {order}"
    )

    migrated = 0
    with pg.cursor() as target_cursor:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            payload = [
                tuple(
                    _coerce_for_postgres(value, column.pg_type)
                    for value, column in zip(row, contract.columns, strict=True)
                )
                for row in rows
            ]
            target_cursor.executemany(statement, payload)
            migrated += len(payload)
    return migrated


def _postgres_rows(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> Iterable[tuple[Any, ...]]:
    cursor = pg.execute(
        sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            sql.SQL(", ").join(
                sql.Identifier(column.name) for column in contract.columns
            ),
            sql.Identifier(schema),
            sql.Identifier(contract.name),
            sql.SQL(", ").join(
                sql.Identifier(name) for name in contract.primary_key
            ),
        )
    )
    yield from cursor


def migrate_sqlite_to_postgres(
    source: str | Path,
    postgres_dsn: str,
    schema: str,
    correlation_id: str,
    *,
    environment: str,
    automation_sha: str | None = None,
    batch_size: int = BATCH_SIZE_DEFAULT,
) -> dict[str, Any]:
    if not correlation_id.strip():
        raise PreflightError("correlation_id is required")

    plan = preflight_migration(
        source=source,
        postgres_dsn=postgres_dsn,
        schema=schema,
        environment=environment,
    )
    source_path = Path(source).resolve()
    sqlite_db, contracts = _source_contract(source_path)
    psycopg, sql = _load_psycopg()

    try:
        current_tables, current_fingerprint = _source_evidence(sqlite_db, contracts)
        if current_fingerprint != plan["source_fingerprint"]:
            raise MigrationConflictError(
                "SQLite source changed after preflight; rerun preflight"
            )
        if set(current_tables) != set(plan["tables"]):
            raise MigrationConflictError(
                "SQLite source contract changed after preflight"
            )

        with psycopg.connect(postgres_dsn) as pg:
            _create_schema_and_audit(pg, sql, schema)

            existing = pg.execute(
                sql.SQL(
                    "SELECT source_fingerprint, run_count "
                    "FROM {}.{} WHERE correlation_id = %s"
                ).format(
                    sql.Identifier(schema),
                    sql.Identifier("_migration_runs"),
                ),
                (correlation_id,),
            ).fetchone()
            if existing and existing[0] != plan["source_fingerprint"]:
                raise MigrationConflictError(
                    "correlation_id was already used with a different source fingerprint"
                )

            table_evidence: dict[str, dict[str, Any]] = {}
            for contract in contracts:
                _create_target_table(pg, sql, schema, contract)
                migrated = _upsert_source_rows(
                    sqlite_db,
                    pg,
                    sql,
                    schema,
                    contract,
                    batch_size=batch_size,
                )
                target_count, target_hash = _hash_rows(
                    _postgres_rows(pg, sql, schema, contract),
                    contract.columns,
                )
                source_evidence = plan["tables"][contract.name]
                if target_count != source_evidence["source_count"]:
                    raise VerificationError(
                        f"row count mismatch for {contract.name}: "
                        f"source={source_evidence['source_count']} target={target_count}"
                    )
                if target_hash != source_evidence["source_sha256"]:
                    raise VerificationError(
                        f"row fingerprint mismatch for {contract.name}"
                    )
                table_evidence[contract.name] = {
                    "source_count": source_evidence["source_count"],
                    "target_count": target_count,
                    "source_sha256": source_evidence["source_sha256"],
                    "target_sha256": target_hash,
                    "rows_processed": migrated,
                }

            evidence = {
                "status": "completed",
                "environment": environment,
                "schema": schema,
                "correlation_id": correlation_id,
                "source_fingerprint": plan["source_fingerprint"],
                "tables": table_evidence,
            }
            audit_cursor = pg.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.{} AS mr
                        (correlation_id, source_fingerprint, automation_sha,
                         environment, run_count, status, evidence_json)
                    VALUES (%s, %s, %s, %s, 1, 'completed', %s::jsonb)
                    ON CONFLICT (correlation_id) DO UPDATE SET
                        source_fingerprint = EXCLUDED.source_fingerprint,
                        automation_sha = EXCLUDED.automation_sha,
                        environment = EXCLUDED.environment,
                        run_count = mr.run_count + 1,
                        status = EXCLUDED.status,
                        evidence_json = EXCLUDED.evidence_json,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING run_count
                    """
                ).format(
                    sql.Identifier(schema),
                    sql.Identifier("_migration_runs"),
                ),
                (
                    correlation_id,
                    plan["source_fingerprint"],
                    automation_sha or os.environ.get("GITHUB_SHA") or "local",
                    environment,
                    json.dumps(evidence, sort_keys=True),
                ),
            )
            evidence["replay_count"] = int(audit_cursor.fetchone()[0])
            return evidence
    finally:
        sqlite_db.close()


def _dsn_from_environment(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value:
        raise PreflightError(
            f"required environment variable is not set: {variable}"
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a supported SQLite database into an isolated PostgreSQL schema"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--environment",
        required=True,
        choices=sorted(SAFE_ENVIRONMENTS),
    )
    parser.add_argument("--postgres-dsn-env", default="POSTGRES_DSN")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT)
    args = parser.parse_args()

    dsn = _dsn_from_environment(args.postgres_dsn_env)
    if args.preflight_only:
        result = preflight_migration(
            source=args.source,
            postgres_dsn=dsn,
            schema=args.schema,
            environment=args.environment,
        )
    else:
        result = migrate_sqlite_to_postgres(
            source=args.source,
            postgres_dsn=dsn,
            schema=args.schema,
            correlation_id=args.correlation_id,
            environment=args.environment,
            batch_size=args.batch_size,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
