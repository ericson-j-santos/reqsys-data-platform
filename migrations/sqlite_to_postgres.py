#!/usr/bin/env python3
"""Governed SQLite -> PostgreSQL migration for isolated local/DEV/CI schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrations.sqlite_schema_contract import (
    ColumnContract,
    ForeignKeyContract,
    IndexContract,
    SchemaContractError,
    TableContract,
    strict_schema_contract,
)

SAFE_ENVIRONMENTS = {"local", "dev", "ci", "test"}
SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH_SIZE_DEFAULT = 500


class PreflightError(ValueError):
    """Raised when migration preconditions are not safe or supported."""


class MigrationConflictError(RuntimeError):
    """Raised when an idempotency key is replayed with different source state."""


class VerificationError(RuntimeError):
    """Raised when post-migration evidence differs from the source."""


def _load_psycopg() -> tuple[Any, Any, Any, Any]:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Json, Jsonb
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for PostgreSQL migration; install requirements-ci.txt"
        ) from exc
    return psycopg, sql, Json, Jsonb


def _canonical_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value).strip()).isoformat()


def _canonical_json(value: Any) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_value(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "BOOLEAN":
        return bool(value)
    if pg_type in {"INTEGER", "BIGINT", "SMALLINT"}:
        return int(value)
    if pg_type == "DOUBLE PRECISION":
        return format(float(value), ".17g")
    if pg_type.startswith("NUMERIC"):
        decimal = Decimal(str(value))
        if decimal == 0:
            return "0"
        return format(decimal.normalize(), "f")
    if pg_type == "BYTEA":
        return bytes(value).hex()
    if pg_type == "DATE":
        return _canonical_date(value)
    if pg_type == "TIMESTAMPTZ":
        return _canonical_datetime(value)
    if pg_type in {"JSON", "JSONB"}:
        return _canonical_json(value)
    return str(value)

def _coerce_for_postgres(
    value: Any,
    pg_type: str,
    Json: Any,
    Jsonb: Any,
) -> Any:
    if value is None:
        return None
    if pg_type == "BOOLEAN":
        return bool(value)
    if pg_type in {"INTEGER", "BIGINT", "SMALLINT"}:
        return int(value)
    if pg_type == "DOUBLE PRECISION":
        return float(value)
    if pg_type.startswith("NUMERIC"):
        return Decimal(str(value))
    if pg_type == "BYTEA":
        return bytes(value)
    if pg_type == "DATE":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value).strip())
    if pg_type == "TIMESTAMPTZ":
        raw = value
        if not isinstance(raw, datetime):
            text_value = str(raw).strip()
            if text_value.endswith("Z"):
                text_value = text_value[:-1] + "+00:00"
            raw = datetime.fromisoformat(text_value)
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return raw
    if pg_type in {"JSON", "JSONB"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return Jsonb(parsed) if pg_type == "JSONB" else Json(parsed)
    return str(value)

def _sqlite_rows(
    connection: Any,
    contract: TableContract,
) -> Iterable[tuple[Any, ...]]:
    from migrations.sqlite_schema_contract import quote_sqlite_identifier

    columns = ", ".join(quote_sqlite_identifier(c.name) for c in contract.columns)
    order = ", ".join(
        quote_sqlite_identifier(name) for name in contract.primary_key
    )
    cursor = connection.execute(
        f"SELECT {columns} FROM {quote_sqlite_identifier(contract.name)} ORDER BY {order}"
    )
    yield from cursor


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


def _source_evidence(
    connection: Any,
    contracts: tuple[TableContract, ...],
) -> tuple[dict[str, dict[str, Any]], str]:
    tables: dict[str, dict[str, Any]] = {}
    overall = hashlib.sha256()
    for contract in contracts:
        count, row_hash = _hash_rows(_sqlite_rows(connection, contract), contract.columns)
        schema_payload = contract.to_dict()
        tables[contract.name] = {
            "source_count": count,
            "source_sha256": row_hash,
            "schema": schema_payload,
        }
        overall.update(
            json.dumps(schema_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        overall.update(row_hash.encode("ascii"))
    return tables, overall.hexdigest()


def _pg_type_details(pg_type: str) -> tuple[str, int | None, int | None, int | None]:
    upper = pg_type.upper()
    character = re.fullmatch(r"(VARCHAR|CHAR)(?:\((\d+)\))?", upper)
    if character:
        kind, length = character.groups()
        data_type = "character varying" if kind == "VARCHAR" else "character"
        return data_type, int(length) if length else None, None, None

    numeric = re.fullmatch(r"NUMERIC(?:\((\d+)(?:,(\d+))?\))?", upper)
    if numeric:
        precision, scale = numeric.groups()
        return (
            "numeric",
            None,
            int(precision) if precision else None,
            int(scale) if scale else None,
        )

    mapping = {
        "INTEGER": "integer",
        "BIGINT": "bigint",
        "SMALLINT": "smallint",
        "BOOLEAN": "boolean",
        "TEXT": "text",
        "DOUBLE PRECISION": "double precision",
        "BYTEA": "bytea",
        "DATE": "date",
        "TIMESTAMPTZ": "timestamp with time zone",
        "JSON": "json",
        "JSONB": "jsonb",
    }
    if upper not in mapping:
        raise PreflightError(f"unsupported PostgreSQL type mapping: {pg_type}")
    return mapping[upper], None, None, None

def _postgres_table_signature(
    pg: Any,
    schema: str,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    rows = pg.execute(
        """
        SELECT column_name, data_type, character_maximum_length,
               CASE WHEN data_type = 'numeric' THEN numeric_precision ELSE NULL END,
               CASE WHEN data_type = 'numeric' THEN numeric_scale ELSE NULL END,
               is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _expected_pg_signature(contract: TableContract) -> tuple[tuple[Any, ...], ...]:
    expected: list[tuple[Any, ...]] = []
    for column in contract.columns:
        data_type, length, precision, scale = _pg_type_details(column.pg_type)
        expected.append(
            (
                column.name,
                data_type,
                length,
                precision,
                scale,
                "YES" if column.nullable else "NO",
            )
        )
    return tuple(expected)


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


def _validate_reference_scope(contracts: tuple[TableContract, ...]) -> None:
    names = {contract.name for contract in contracts}
    for contract in contracts:
        for foreign_key in contract.foreign_keys:
            if foreign_key.referenced_table not in names:
                raise PreflightError(
                    f"foreign key references a table outside the migration scope: "
                    f"{contract.name}->{foreign_key.referenced_table}"
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
    try:
        sqlite_db, contracts = strict_schema_contract(source_path)
    except SchemaContractError as exc:
        raise PreflightError(str(exc)) from exc
    try:
        _validate_reference_scope(contracts)
        source_tables, source_fingerprint = _source_evidence(sqlite_db, contracts)
    finally:
        sqlite_db.close()

    psycopg, _sql, _Json, _Jsonb = _load_psycopg()
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
            raise PreflightError("PostgreSQL user lacks CREATE privilege on target database")

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


def _default_fragment(sql: Any, column: ColumnContract) -> Any | None:
    kind = column.default_kind
    if kind is None:
        return None
    if kind == "current_timestamp":
        return sql.SQL("DEFAULT CURRENT_TIMESTAMP")
    if kind == "null":
        return sql.SQL("DEFAULT NULL")
    if kind == "literal":
        return sql.SQL("DEFAULT {}").format(sql.Literal(column.default_value))
    if kind == "numeric":
        return sql.SQL("DEFAULT {}").format(sql.Literal(Decimal(column.default_value or "0")))
    raise PreflightError(f"unsupported normalized default kind: {kind}")


def _create_target_table(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> None:
    definitions: list[Any] = []
    for column in contract.columns:
        if contract.identity_column == column.name:
            if column.pg_type != "INTEGER":
                raise PreflightError(
                    f"serial identity requires INTEGER: {contract.name}.{column.name}"
                )
            parts = [sql.Identifier(column.name), sql.SQL("SERIAL")]
        else:
            parts = [sql.Identifier(column.name), sql.SQL(column.pg_type)]
        default = _default_fragment(sql, column)
        if default is not None:
            parts.append(default)
        if not column.nullable:
            parts.append(sql.SQL("NOT NULL"))
        definitions.append(sql.SQL(" ").join(parts))

    definitions.append(
        sql.SQL("PRIMARY KEY ({})").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in contract.primary_key)
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
    sqlite_db: Any,
    pg: Any,
    sql: Any,
    Json: Any,
    Jsonb: Any,
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
        sql.SQL(", ").join(sql.Identifier(name) for name in contract.primary_key)
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

    from migrations.sqlite_schema_contract import quote_sqlite_identifier

    columns = ", ".join(quote_sqlite_identifier(c.name) for c in contract.columns)
    order = ", ".join(
        quote_sqlite_identifier(name) for name in contract.primary_key
    )
    cursor = sqlite_db.execute(
        f"SELECT {columns} FROM {quote_sqlite_identifier(contract.name)} ORDER BY {order}"
    )

    migrated = 0
    with pg.cursor() as target_cursor:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            payload = [
                tuple(
                    _coerce_for_postgres(value, column.pg_type, Json, Jsonb)
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


def _create_indexes(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> None:
    for index in contract.indexes:
        unique = sql.SQL("UNIQUE ") if index.unique else sql.SQL("")
        pg.execute(
            sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                unique,
                sql.Identifier(index.target_name),
                sql.Identifier(schema),
                sql.Identifier(contract.name),
                sql.SQL(", ").join(sql.Identifier(column) for column in index.columns),
            )
        )


def _constraint_exists(pg: Any, schema: str, name: str) -> bool:
    return bool(
        pg.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE n.nspname = %s AND c.conname = %s
            )
            """,
            (schema, name),
        ).fetchone()[0]
    )


def _create_foreign_keys(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> None:
    for foreign_key in contract.foreign_keys:
        if _constraint_exists(pg, schema, foreign_key.target_name):
            continue
        pg.execute(
            sql.SQL(
                "ALTER TABLE {}.{} ADD CONSTRAINT {} FOREIGN KEY ({}) "
                "REFERENCES {}.{} ({}) ON UPDATE {} ON DELETE {}"
            ).format(
                sql.Identifier(schema),
                sql.Identifier(contract.name),
                sql.Identifier(foreign_key.target_name),
                sql.SQL(", ").join(
                    sql.Identifier(column) for column in foreign_key.columns
                ),
                sql.Identifier(schema),
                sql.Identifier(foreign_key.referenced_table),
                sql.SQL(", ").join(
                    sql.Identifier(column)
                    for column in foreign_key.referenced_columns
                ),
                sql.SQL(foreign_key.on_update),
                sql.SQL(foreign_key.on_delete),
            )
        )


def _sync_identity_sequence(
    pg: Any,
    sql: Any,
    schema: str,
    contract: TableContract,
) -> None:
    column = contract.identity_column
    if not column:
        return
    relation = sql.Identifier(schema, contract.name).as_string(pg)
    sequence = pg.execute(
        "SELECT pg_get_serial_sequence(%s, %s)",
        (relation, column),
    ).fetchone()[0]
    if not sequence:
        return
    maximum = pg.execute(
        sql.SQL("SELECT MAX({}) FROM {}.{}").format(
            sql.Identifier(column),
            sql.Identifier(schema),
            sql.Identifier(contract.name),
        )
    ).fetchone()[0]
    if maximum is not None:
        pg.execute("SELECT setval(%s::regclass, %s, true)", (sequence, maximum))


def _verify_indexes(
    pg: Any,
    schema: str,
    contract: TableContract,
) -> None:
    observed = {
        name
        for (name,) in pg.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (schema, contract.name),
        ).fetchall()
    }
    missing = [
        index.target_name for index in contract.indexes if index.target_name not in observed
    ]
    if missing:
        raise VerificationError(
            f"destination indexes missing for {contract.name}: {missing}"
        )


def _verify_foreign_keys(
    pg: Any,
    schema: str,
    contract: TableContract,
) -> None:
    missing = [
        foreign_key.target_name
        for foreign_key in contract.foreign_keys
        if not _constraint_exists(pg, schema, foreign_key.target_name)
    ]
    if missing:
        raise VerificationError(
            f"destination foreign keys missing for {contract.name}: {missing}"
        )


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
    try:
        sqlite_db, contracts = strict_schema_contract(source_path)
    except SchemaContractError as exc:
        raise PreflightError(str(exc)) from exc
    psycopg, sql, Json, Jsonb = _load_psycopg()

    try:
        current_tables, current_fingerprint = _source_evidence(sqlite_db, contracts)
        if current_fingerprint != plan["source_fingerprint"]:
            raise MigrationConflictError(
                "SQLite source changed after preflight; rerun preflight"
            )
        if set(current_tables) != set(plan["tables"]):
            raise MigrationConflictError("SQLite source contract changed after preflight")

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

            for contract in contracts:
                _create_target_table(pg, sql, schema, contract)

            rows_processed: dict[str, int] = {}
            for contract in contracts:
                rows_processed[contract.name] = _upsert_source_rows(
                    sqlite_db,
                    pg,
                    sql,
                    Json,
                    Jsonb,
                    schema,
                    contract,
                    batch_size=batch_size,
                )
                _sync_identity_sequence(pg, sql, schema, contract)

            table_evidence: dict[str, dict[str, Any]] = {}
            for contract in contracts:
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
                    "rows_processed": rows_processed[contract.name],
                    "indexes": len(contract.indexes),
                    "foreign_keys": len(contract.foreign_keys),
                }

            for contract in contracts:
                _create_indexes(pg, sql, schema, contract)
            for contract in contracts:
                _create_foreign_keys(pg, sql, schema, contract)
            for contract in contracts:
                _verify_indexes(pg, schema, contract)
                _verify_foreign_keys(pg, schema, contract)

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
        raise PreflightError(f"required environment variable is not set: {variable}")
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
