#!/usr/bin/env python3
"""Read-only, contract-driven data quality validation for SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SAFE_ENVIRONMENTS = {"local", "dev", "ci", "test"}
SCHEMA_VERSION = 1


class DataQualityError(ValueError):
    """Base class for fail-closed data quality validation errors."""


class ContractError(DataQualityError):
    """Raised when the quality contract is invalid or incompatible."""


class PreflightError(DataQualityError):
    """Raised when the source database is unsafe or unsupported."""


def _quote_identifier(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ContractError("identifiers must be non-empty strings")
    return '"' + name.replace('"', '""') + '"'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contract_sha256(contract: dict[str, Any]) -> str:
    payload = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _readonly_connection(source: Path) -> sqlite3.Connection:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    wal = Path(str(source) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise PreflightError(
            "SQLite source has a non-empty WAL file; checkpoint before validation"
        )
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        connection.close()
        raise PreflightError(f"SQLite integrity check failed: {integrity}")
    return connection


def _schema(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_xinfo({_quote_identifier(table)})"
            ).fetchall()
            if not row[6]
        }
        for table in tables
    }


def _require_columns(
    schema: dict[str, set[str]],
    table: str,
    columns: Iterable[str],
) -> list[str]:
    if table not in schema:
        raise ContractError(f"contract references unknown table: {table}")
    normalized = []
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise ContractError(f"{table}: column names must be non-empty strings")
        if column not in schema[table]:
            raise ContractError(f"contract references unknown column: {table}.{column}")
        normalized.append(column)
    if not normalized:
        raise ContractError(f"{table}: rule column list cannot be empty")
    return normalized


def _normalized_contract(
    contract: dict[str, Any],
    schema: dict[str, set[str]],
) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise ContractError("contract must be a JSON object")
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"schema_version must be {SCHEMA_VERSION}")

    raw_tables = contract.get("tables")
    if not isinstance(raw_tables, dict) or not raw_tables:
        raise ContractError("contract.tables must be a non-empty object")

    normalized: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "tables": {}}
    for table, raw_rules in raw_tables.items():
        if not isinstance(table, str) or not table.strip():
            raise ContractError("table names must be non-empty strings")
        if table not in schema:
            raise ContractError(f"contract references unknown table: {table}")
        if not isinstance(raw_rules, dict):
            raise ContractError(f"{table}: rules must be an object")

        supported = {
            "required",
            "unique",
            "domains",
            "temporal",
            "foreign_keys",
            "idempotency_keys",
        }
        unknown = sorted(set(raw_rules) - supported)
        if unknown:
            raise ContractError(f"{table}: unsupported rules: {', '.join(unknown)}")

        table_rules: dict[str, Any] = {}

        required = raw_rules.get("required", [])
        if not isinstance(required, list):
            raise ContractError(f"{table}.required must be a list")
        table_rules["required"] = _require_columns(schema, table, required) if required else []

        for key in ("unique", "idempotency_keys"):
            raw_keys = raw_rules.get(key, [])
            if not isinstance(raw_keys, list):
                raise ContractError(f"{table}.{key} must be a list")
            normalized_keys = []
            for columns in raw_keys:
                if not isinstance(columns, list):
                    raise ContractError(f"{table}.{key} entries must be lists")
                normalized_keys.append(_require_columns(schema, table, columns))
            table_rules[key] = normalized_keys

        domains = raw_rules.get("domains", {})
        if not isinstance(domains, dict):
            raise ContractError(f"{table}.domains must be an object")
        normalized_domains: dict[str, list[Any]] = {}
        for column, allowed in domains.items():
            _require_columns(schema, table, [column])
            if not isinstance(allowed, list) or not allowed:
                raise ContractError(f"{table}.domains.{column} must be a non-empty list")
            if any(
                value is None or isinstance(value, (dict, list))
                for value in allowed
            ):
                raise ContractError(
                    f"{table}.domains.{column} accepts only non-null JSON scalars"
                )
            normalized_domains[column] = allowed
        table_rules["domains"] = normalized_domains

        temporal = raw_rules.get("temporal", [])
        if not isinstance(temporal, list):
            raise ContractError(f"{table}.temporal must be a list")
        normalized_temporal = []
        for rule in temporal:
            if not isinstance(rule, dict):
                raise ContractError(f"{table}.temporal entries must be objects")
            if set(rule) != {"start", "end"}:
                raise ContractError(
                    f"{table}.temporal entries require exactly start and end"
                )
            _require_columns(schema, table, [rule["start"], rule["end"]])
            normalized_temporal.append(
                {"start": rule["start"], "end": rule["end"]}
            )
        table_rules["temporal"] = normalized_temporal

        foreign_keys = raw_rules.get("foreign_keys", [])
        if not isinstance(foreign_keys, list):
            raise ContractError(f"{table}.foreign_keys must be a list")
        normalized_fks = []
        for rule in foreign_keys:
            if not isinstance(rule, dict):
                raise ContractError(f"{table}.foreign_keys entries must be objects")
            if set(rule) != {"columns", "ref_table", "ref_columns"}:
                raise ContractError(
                    f"{table}.foreign_keys entries require columns, ref_table, ref_columns"
                )
            columns = _require_columns(schema, table, rule["columns"])
            ref_table = rule["ref_table"]
            if not isinstance(ref_table, str) or ref_table not in schema:
                raise ContractError(
                    f"{table}: foreign key references unknown table: {ref_table}"
                )
            ref_columns = _require_columns(schema, ref_table, rule["ref_columns"])
            if len(columns) != len(ref_columns):
                raise ContractError(
                    f"{table}: foreign key column counts must match reference columns"
                )
            normalized_fks.append(
                {
                    "columns": columns,
                    "ref_table": ref_table,
                    "ref_columns": ref_columns,
                }
            )
        table_rules["foreign_keys"] = normalized_fks
        normalized["tables"][table] = table_rules

    return normalized


def _count_required(connection: sqlite3.Connection, table: str, column: str) -> int:
    q_table = _quote_identifier(table)
    q_column = _quote_identifier(column)
    sql = (
        f"SELECT COUNT(*) FROM {q_table} "
        f"WHERE {q_column} IS NULL "
        f"OR (typeof({q_column}) = 'text' AND trim({q_column}) = '')"
    )
    return int(connection.execute(sql).fetchone()[0])


def _count_duplicate_groups(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> int:
    q_table = _quote_identifier(table)
    q_columns = [_quote_identifier(column) for column in columns]
    complete = " AND ".join(f"{column} IS NOT NULL" for column in q_columns)
    grouped = ", ".join(q_columns)
    sql = (
        "SELECT COUNT(*) FROM ("
        f"SELECT 1 FROM {q_table} WHERE {complete} "
        f"GROUP BY {grouped} HAVING COUNT(*) > 1"
        ") AS duplicate_groups"
    )
    return int(connection.execute(sql).fetchone()[0])


def _count_domain(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    allowed: list[Any],
) -> int:
    q_table = _quote_identifier(table)
    q_column = _quote_identifier(column)
    placeholders = ", ".join("?" for _ in allowed)
    sql = (
        f"SELECT COUNT(*) FROM {q_table} "
        f"WHERE {q_column} IS NOT NULL AND {q_column} NOT IN ({placeholders})"
    )
    return int(connection.execute(sql, allowed).fetchone()[0])


def _count_temporal(
    connection: sqlite3.Connection,
    table: str,
    start: str,
    end: str,
) -> int:
    q_table = _quote_identifier(table)
    q_start = _quote_identifier(start)
    q_end = _quote_identifier(end)
    sql = (
        f"SELECT COUNT(*) FROM {q_table} "
        f"WHERE {q_start} IS NOT NULL AND {q_end} IS NOT NULL "
        f"AND {q_start} > {q_end}"
    )
    return int(connection.execute(sql).fetchone()[0])


def _count_foreign_key(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    ref_table: str,
    ref_columns: list[str],
) -> int:
    q_table = _quote_identifier(table)
    q_ref_table = _quote_identifier(ref_table)
    source_cols = [_quote_identifier(column) for column in columns]
    ref_cols = [_quote_identifier(column) for column in ref_columns]
    any_present = " OR ".join(f"s.{column} IS NOT NULL" for column in source_cols)
    matches = " AND ".join(
        f"r.{ref_column} = s.{source_column}"
        for source_column, ref_column in zip(source_cols, ref_cols)
    )
    sql = (
        f"SELECT COUNT(*) FROM {q_table} AS s "
        f"WHERE ({any_present}) "
        f"AND NOT EXISTS (SELECT 1 FROM {q_ref_table} AS r WHERE {matches})"
    )
    return int(connection.execute(sql).fetchone()[0])


def validate_sqlite(
    source: str | Path,
    contract: dict[str, Any],
    correlation_id: str,
    *,
    environment: str = "local",
    automation_sha: str | None = None,
) -> dict[str, Any]:
    if environment not in SAFE_ENVIRONMENTS:
        raise PreflightError(
            f"environment must be one of: {', '.join(sorted(SAFE_ENVIRONMENTS))}"
        )
    if not correlation_id or not correlation_id.strip():
        raise PreflightError("correlation_id is required")

    source_path = Path(source).resolve()
    source_hash_before = _sha256(source_path) if source_path.is_file() else ""
    connection = _readonly_connection(source_path)
    try:
        schema = _schema(connection)
        normalized = _normalized_contract(contract, schema)
        table_counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            for table in sorted(normalized["tables"])
        }

        results: list[dict[str, Any]] = []
        for table in sorted(normalized["tables"]):
            rules = normalized["tables"][table]
            for column in rules["required"]:
                results.append(
                    {
                        "dimension": "completeness",
                        "table": table,
                        "rule": f"required:{column}",
                        "violation_count": _count_required(
                            connection, table, column
                        ),
                    }
                )

            for columns in rules["unique"]:
                results.append(
                    {
                        "dimension": "uniqueness",
                        "table": table,
                        "rule": "unique:" + ",".join(columns),
                        "violation_count": _count_duplicate_groups(
                            connection, table, columns
                        ),
                    }
                )

            for column, allowed in sorted(rules["domains"].items()):
                results.append(
                    {
                        "dimension": "domain_validity",
                        "table": table,
                        "rule": f"domain:{column}",
                        "violation_count": _count_domain(
                            connection, table, column, allowed
                        ),
                    }
                )

            for rule in rules["temporal"]:
                results.append(
                    {
                        "dimension": "temporal_consistency",
                        "table": table,
                        "rule": f"temporal:{rule['start']}<={rule['end']}",
                        "violation_count": _count_temporal(
                            connection,
                            table,
                            rule["start"],
                            rule["end"],
                        ),
                    }
                )

            for rule in rules["foreign_keys"]:
                results.append(
                    {
                        "dimension": "referential_integrity",
                        "table": table,
                        "rule": (
                            "foreign_key:"
                            + ",".join(rule["columns"])
                            + "->"
                            + rule["ref_table"]
                            + "("
                            + ",".join(rule["ref_columns"])
                            + ")"
                        ),
                        "violation_count": _count_foreign_key(
                            connection,
                            table,
                            rule["columns"],
                            rule["ref_table"],
                            rule["ref_columns"],
                        ),
                    }
                )

            for columns in rules["idempotency_keys"]:
                results.append(
                    {
                        "dimension": "idempotency",
                        "table": table,
                        "rule": "idempotency_key:" + ",".join(columns),
                        "violation_count": _count_duplicate_groups(
                            connection, table, columns
                        ),
                    }
                )
    finally:
        connection.close()

    source_hash_after = _sha256(source_path)
    if source_hash_before != source_hash_after:
        raise PreflightError("SQLite source changed during validation")

    violation_total = sum(item["violation_count"] for item in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if violation_total == 0 else "failed",
        "environment": environment,
        "correlation_id": correlation_id,
        "automation_sha": automation_sha or os.environ.get("GITHUB_SHA") or "local",
        "contract_sha256": _contract_sha256(normalized),
        "source_sha256": source_hash_after,
        "table_row_counts": table_counts,
        "violation_total": violation_total,
        "rules": results,
    }


def load_contract(path: str | Path) -> dict[str, Any]:
    contract_path = Path(path)
    if not contract_path.is_file():
        raise FileNotFoundError(f"contract does not exist: {contract_path}")
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContractError("contract must be a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate SQLite data quality against a versioned contract"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--environment",
        required=True,
        choices=sorted(SAFE_ENVIRONMENTS),
    )
    args = parser.parse_args()

    try:
        evidence = validate_sqlite(
            source=args.source,
            contract=load_contract(args.contract),
            correlation_id=args.correlation_id,
            environment=args.environment,
        )
    except (DataQualityError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "correlation_id": args.correlation_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
