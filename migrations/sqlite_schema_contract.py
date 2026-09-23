#!/usr/bin/env python3
"""SQLite schema introspection contract shared by analysis and migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ALLOWED_FK_ACTIONS = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
_NUMERIC_TYPE = re.compile(r"^(?:NUMERIC|DECIMAL)(?:\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?$")
_NUMERIC_DEFAULT = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_IDENTIFIER_CLEAN = re.compile(r"[^A-Za-z0-9_]+")


class SchemaContractError(ValueError):
    """Raised when SQLite schema semantics are not safely portable."""


@dataclass(frozen=True)
class ColumnContract:
    name: str
    declared_type: str
    pg_type: str
    nullable: bool
    pk_order: int
    default_kind: str | None = None
    default_value: str | None = None


@dataclass(frozen=True)
class IndexContract:
    source_name: str
    target_name: str
    columns: tuple[str, ...]
    unique: bool
    origin: str


@dataclass(frozen=True)
class ForeignKeyContract:
    target_name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_update: str
    on_delete: str


@dataclass(frozen=True)
class TableContract:
    name: str
    columns: tuple[ColumnContract, ...]
    indexes: tuple[IndexContract, ...]
    foreign_keys: tuple[ForeignKeyContract, ...]

    @property
    def primary_key(self) -> tuple[str, ...]:
        ordered = sorted(
            (column for column in self.columns if column.pk_order > 0),
            key=lambda column: column.pk_order,
        )
        return tuple(column.name for column in ordered)

    @property
    def identity_column(self) -> str | None:
        if len(self.primary_key) != 1:
            return None
        column = next(c for c in self.columns if c.name == self.primary_key[0])
        base = re.sub(r"\s*\(.*\)\s*$", "", column.declared_type.strip().upper())
        return column.name if base == "INTEGER" else None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["primary_key"] = list(self.primary_key)
        payload["identity_column"] = self.identity_column
        return payload


def quote_sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def stable_name(prefix: str, table: str, payload: str) -> str:
    digest = hashlib.sha256(f"{table}|{payload}".encode("utf-8")).hexdigest()[:10]
    stem = _IDENTIFIER_CLEAN.sub("_", table).strip("_")[:35] or "table"
    return f"{prefix}_{stem}_{digest}"[:63]


def map_type(declared_type: str) -> str:
    normalized = " ".join(declared_type.strip().upper().split())
    if not normalized:
        raise SchemaContractError("columns without an explicit SQLite type are not supported")

    numeric = _NUMERIC_TYPE.fullmatch(normalized)
    if numeric:
        precision, scale = numeric.groups()
        if precision and scale:
            return f"NUMERIC({precision},{scale})"
        if precision:
            return f"NUMERIC({precision})"
        return "NUMERIC"

    character = _CHAR_TYPE.fullmatch(normalized)
    if character:
        kind, length = character.groups()
        target = "VARCHAR" if kind in {"VARCHAR", "NVARCHAR"} else "CHAR"
        return f"{target}({length})" if length else target

    base = re.sub(r"\s*\(.*\)\s*$", "", normalized)
    if base == "DATE":
        return "DATE"
    if base in {"DATETIME", "TIMESTAMP"}:
        return "TIMESTAMPTZ"
    if base in {"JSON", "JSONB"}:
        return base
    if base == "INTEGER":
        return "INTEGER"
    if base == "BIGINT":
        return "BIGINT"
    if base == "SMALLINT":
        return "SMALLINT"
    if "BOOL" in base:
        return "BOOLEAN"
    if any(token in base for token in ("CLOB", "TEXT")):
        return "TEXT"
    if any(token in base for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in base:
        return "BYTEA"
    raise SchemaContractError(f"unsupported SQLite declared type: {declared_type}")

def normalize_default(default_value: Any) -> tuple[str | None, str | None]:
    if default_value is None:
        return None, None

    value = str(default_value).strip()
    while len(value) >= 2 and value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()

    upper = value.upper()
    if upper == "CURRENT_TIMESTAMP":
        return "current_timestamp", None
    if upper == "NULL":
        return "null", None
    if _NUMERIC_DEFAULT.fullmatch(value):
        return "numeric", value
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return "literal", value[1:-1].replace("''", "'")

    raise SchemaContractError(f"unsupported SQLite default expression: {default_value!r}")


def _introspect_indexes(connection: sqlite3.Connection, table: str) -> tuple[IndexContract, ...]:
    quoted = quote_sqlite_identifier(table)
    contracts: list[IndexContract] = []
    for row in connection.execute(f"PRAGMA index_list({quoted})").fetchall():
        if len(row) < 5:
            raise SchemaContractError(f"unexpected PRAGMA index_list shape for table: {table}")
        _seq, source_name, unique, origin, partial = row[:5]
        if origin == "pk":
            continue
        if partial:
            raise SchemaContractError(f"partial indexes are not supported: {table}.{source_name}")

        info = connection.execute(
            f"PRAGMA index_info({quote_sqlite_identifier(source_name)})"
        ).fetchall()
        columns = tuple(item[2] for item in info if len(item) >= 3)
        if not columns or any(column is None for column in columns):
            raise SchemaContractError(
                f"expression/unknown index columns are not supported: {table}.{source_name}"
            )

        if source_name.startswith("sqlite_autoindex_"):
            target_name = stable_name(
                "uq" if unique else "ix",
                table,
                f"{origin}|{','.join(columns)}",
            )
        else:
            target_name = source_name

        contracts.append(
            IndexContract(
                source_name=source_name,
                target_name=target_name,
                columns=columns,
                unique=bool(unique),
                origin=str(origin),
            )
        )
    return tuple(sorted(contracts, key=lambda item: (item.target_name, item.columns)))


def _introspect_foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[ForeignKeyContract, ...]:
    quoted = quote_sqlite_identifier(table)
    rows = connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(row)

    contracts: list[ForeignKeyContract] = []
    for fk_id, parts in sorted(grouped.items()):
        ordered = sorted(parts, key=lambda item: int(item[1]))
        referenced_table = str(ordered[0][2])
        columns = tuple(str(item[3]) for item in ordered)
        referenced_columns_raw = tuple(item[4] for item in ordered)
        if any(value is None for value in referenced_columns_raw):
            raise SchemaContractError(
                f"implicit referenced PK columns are not supported: {table} fk_id={fk_id}"
            )
        referenced_columns = tuple(str(value) for value in referenced_columns_raw)
        on_update = str(ordered[0][5]).upper()
        on_delete = str(ordered[0][6]).upper()
        if on_update not in _ALLOWED_FK_ACTIONS or on_delete not in _ALLOWED_FK_ACTIONS:
            raise SchemaContractError(
                f"unsupported FK action: {table} update={on_update} delete={on_delete}"
            )
        payload = (
            f"{','.join(columns)}->{referenced_table}({','.join(referenced_columns)})"
            f"|{on_update}|{on_delete}"
        )
        contracts.append(
            ForeignKeyContract(
                target_name=stable_name("fk", table, payload),
                columns=columns,
                referenced_table=referenced_table,
                referenced_columns=referenced_columns,
                on_update=on_update,
                on_delete=on_delete,
            )
        )
    return tuple(contracts)


def introspect_table(connection: sqlite3.Connection, table: str) -> TableContract:
    quoted = quote_sqlite_identifier(table)
    raw_columns = connection.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
    if not raw_columns:
        raise SchemaContractError(f"table has no visible columns: {table}")

    columns: list[ColumnContract] = []
    for _cid, name, declared_type, notnull, default_value, pk_order, hidden in raw_columns:
        if hidden:
            raise SchemaContractError(
                f"generated/hidden columns are not supported: {table}.{name}"
            )
        default_kind, normalized_default = normalize_default(default_value)
        columns.append(
            ColumnContract(
                name=str(name),
                declared_type=str(declared_type),
                pg_type=map_type(str(declared_type)),
                nullable=not bool(notnull) and not bool(pk_order),
                pk_order=int(pk_order),
                default_kind=default_kind,
                default_value=normalized_default,
            )
        )

    contract = TableContract(
        name=table,
        columns=tuple(columns),
        indexes=_introspect_indexes(connection, table),
        foreign_keys=_introspect_foreign_keys(connection, table),
    )
    if not contract.primary_key:
        raise SchemaContractError(
            f"table requires a primary key for idempotent migration: {table}"
        )
    return contract


def connect_readonly(source: str | Path) -> sqlite3.Connection:
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"source database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return connection


def table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    )


def strict_schema_contract(
    source: str | Path,
) -> tuple[sqlite3.Connection, tuple[TableContract, ...]]:
    connection = connect_readonly(source)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SchemaContractError(f"SQLite integrity check failed: {integrity}")
        names = table_names(connection)
        if not names:
            raise SchemaContractError("SQLite source contains no user tables")
        contracts = tuple(introspect_table(connection, table) for table in names)
        return connection, contracts
    except Exception:
        connection.close()
        raise


def analyze_sqlite_schema(source: str | Path) -> dict[str, Any]:
    path = Path(source).resolve()
    connection = connect_readonly(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        names = table_names(connection)
        tables: list[dict[str, Any]] = []
        unsupported: list[dict[str, str]] = []
        for table in names:
            try:
                tables.append(introspect_table(connection, table).to_dict())
            except SchemaContractError as exc:
                unsupported.append({"table": table, "reason": str(exc)})

        metrics = {
            "tables": len(tables),
            "columns": sum(len(item["columns"]) for item in tables),
            "foreign_keys": sum(len(item["foreign_keys"]) for item in tables),
            "indexes": sum(len(item["indexes"]) for item in tables),
            "unique_indexes": sum(
                1 for item in tables for index in item["indexes"] if index["unique"]
            ),
            "defaults": sum(
                1
                for item in tables
                for column in item["columns"]
                if column["default_kind"] is not None
            ),
            "date_columns": sum(
                1 for item in tables for column in item["columns"] if column["pg_type"] == "DATE"
            ),
            "datetime_columns": sum(
                1
                for item in tables
                for column in item["columns"]
                if column["pg_type"] == "TIMESTAMPTZ"
            ),
            "json_columns": sum(
                1 for item in tables for column in item["columns"] if column["pg_type"] in {"JSON", "JSONB"}
            ),
            "numeric_columns": sum(
                1
                for item in tables
                for column in item["columns"]
                if column["pg_type"].startswith("NUMERIC")
            ),
            "identity_columns": sum(
                1 for item in tables if item["identity_column"] is not None
            ),
        }
        contract_payload = {"tables": tables, "unsupported": unsupported}
        fingerprint = hashlib.sha256(
            json.dumps(contract_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "status": "supported" if integrity == "ok" and not unsupported else "unsupported",
            "source": str(path),
            "integrity": integrity,
            "schema_fingerprint": fingerprint,
            "metrics": metrics,
            "unsupported": unsupported,
            "tables": tables,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze SQLite -> PostgreSQL schema portability")
    parser.add_argument("--source", required=True)
    parser.add_argument("--json")
    parser.add_argument("--require-supported", action="store_true")
    args = parser.parse_args()

    report = analyze_sqlite_schema(args.source)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        Path(args.json).write_text(payload, encoding="utf-8")
    print(payload, end="")
    if args.require_supported and report["status"] != "supported":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
