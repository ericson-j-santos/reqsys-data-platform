from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from migrations.sqlite_schema_contract import analyze_sqlite_schema
from migrations.sqlite_to_postgres import migrate_sqlite_to_postgres


class ReqSysProductSchemaContractE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("POSTGRES_DSN", "")
        cls.source = os.environ.get("REQSYS_PRODUCT_SCHEMA_DB", "")
        if not cls.dsn or not cls.source:
            raise unittest.SkipTest(
                "POSTGRES_DSN and REQSYS_PRODUCT_SCHEMA_DB are required"
            )

    def setUp(self) -> None:
        self.schema = f"reqsys_contract_{uuid.uuid4().hex[:12]}"

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as pg:
            pg.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )

    def test_current_reqsys_generated_sqlite_schema_is_supported(self) -> None:
        report = analyze_sqlite_schema(Path(self.source))
        self.assertEqual(report["status"], "supported", report["unsupported"])
        correlation_id = f"product-schema-{uuid.uuid4().hex}"
        first = migrate_sqlite_to_postgres(
            source=self.source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(first["replay_count"], 1)

        expected_tables = {item["name"] for item in report["tables"]}
        expected_indexes = {
            index["target_name"]
            for table in report["tables"]
            for index in table["indexes"]
        }
        expected_fks = {
            fk["target_name"]
            for table in report["tables"]
            for fk in table["foreign_keys"]
        }

        with psycopg.connect(self.dsn) as pg:
            observed_tables = {
                row[0]
                for row in pg.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    """,
                    (self.schema,),
                ).fetchall()
                if row[0] != "_migration_runs"
            }
            self.assertEqual(observed_tables, expected_tables)

            observed_indexes = {
                row[0]
                for row in pg.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                    (self.schema,),
                ).fetchall()
            }
            self.assertTrue(expected_indexes.issubset(observed_indexes))

            observed_fks = {
                row[0]
                for row in pg.execute(
                    """
                    SELECT c.conname
                    FROM pg_constraint c
                    JOIN pg_namespace n ON n.oid = c.connamespace
                    WHERE n.nspname = %s AND c.contype = 'f'
                    """,
                    (self.schema,),
                ).fetchall()
            }
            self.assertEqual(observed_fks, expected_fks)

        second = migrate_sqlite_to_postgres(
            source=self.source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(second["replay_count"], 2)


if __name__ == "__main__":
    unittest.main()
