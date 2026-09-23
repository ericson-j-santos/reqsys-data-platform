from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.sqlite_schema_contract import (
    SchemaContractError,
    analyze_sqlite_schema,
    strict_schema_contract,
)


class SQLiteSchemaContractTest(unittest.TestCase):
    def test_reqsys_like_contract_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "reqsys-like.sqlite"
            with sqlite3.connect(source) as db:
                db.execute(
                    """
                    CREATE TABLE projects (
                        id INTEGER PRIMARY KEY,
                        code VARCHAR(40) NOT NULL UNIQUE,
                        reference_date DATE NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        provider VARCHAR(64) NOT NULL DEFAULT 'hash-local-256',
                        amount NUMERIC(12, 6),
                        payload JSON
                    )
                    """
                )
                db.execute("CREATE INDEX ix_projects_reference_date ON projects(reference_date)")
                db.execute(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    )
                    """
                )
                db.commit()

            report = analyze_sqlite_schema(source)
            self.assertEqual(report["status"], "supported")
            self.assertEqual(report["metrics"]["tables"], 2)
            self.assertEqual(report["metrics"]["foreign_keys"], 1)
            self.assertGreaterEqual(report["metrics"]["indexes"], 2)
            self.assertGreaterEqual(report["metrics"]["unique_indexes"], 1)
            self.assertEqual(report["metrics"]["json_columns"], 1)
            self.assertEqual(report["metrics"]["numeric_columns"], 1)
            self.assertEqual(report["metrics"]["date_columns"], 1)
            self.assertEqual(report["metrics"]["datetime_columns"], 1)
            self.assertEqual(report["metrics"]["identity_columns"], 2)

            connection, contracts = strict_schema_contract(source)
            try:
                projects = next(item for item in contracts if item.name == "projects")
                defaults = {
                    column.name: (column.default_kind, column.default_value)
                    for column in projects.columns
                }
                self.assertEqual(defaults["created_at"], ("current_timestamp", None))
                self.assertEqual(defaults["provider"], ("literal", "hash-local-256"))
            finally:
                connection.close()

    def test_partial_index_is_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "unsupported.sqlite"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
                db.execute(
                    "CREATE INDEX ix_items_open ON items(status) WHERE status = 'open'"
                )
                db.commit()

            report = analyze_sqlite_schema(source)
            self.assertEqual(report["status"], "unsupported")
            self.assertEqual(len(report["unsupported"]), 1)
            self.assertIn("partial indexes", report["unsupported"][0]["reason"])
            with self.assertRaises(SchemaContractError):
                strict_schema_contract(source)


if __name__ == "__main__":
    unittest.main()
