from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from migrations.sqlite_to_postgres import (
    MigrationConflictError,
    PreflightError,
    migrate_sqlite_to_postgres,
    preflight_migration,
)


class SQLiteToPostgresE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("POSTGRES_DSN", "")
        if not cls.dsn:
            raise unittest.SkipTest("POSTGRES_DSN is required for PostgreSQL E2E")

    def setUp(self) -> None:
        self.schema = f"e2e_{uuid.uuid4().hex[:16]}"
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as pg:
            pg.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )
        self.tempdir.cleanup()

    def _create_source(self) -> Path:
        source = self.root / "source.sqlite"
        with sqlite3.connect(source) as db:
            db.execute(
                """
                CREATE TABLE demands (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    active BOOLEAN NOT NULL
                )
                """
            )
            db.executemany(
                "INSERT INTO demands(id, title, active) VALUES (?, ?, ?)",
                [
                    (1, "synthetic-a", 1),
                    (2, "synthetic-b", 0),
                ],
            )
            db.commit()
        return source

    def _target_rows(self) -> list[tuple[int, str, bool]]:
        with psycopg.connect(self.dsn) as pg:
            return pg.execute(
                sql.SQL("SELECT id, title, active FROM {}.{} ORDER BY id").format(
                    sql.Identifier(self.schema),
                    sql.Identifier("demands"),
                )
            ).fetchall()

    def _target_count(self) -> int:
        with psycopg.connect(self.dsn) as pg:
            return int(
                pg.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier("demands"),
                    )
                ).fetchone()[0]
            )

    def test_positive_migration_and_idempotent_replay(self) -> None:
        source = self._create_source()
        correlation_id = f"migration-{uuid.uuid4().hex}"

        first = migrate_sqlite_to_postgres(
            source=source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(first["tables"]["demands"]["source_count"], 2)
        self.assertEqual(first["tables"]["demands"]["target_count"], 2)
        self.assertEqual(first["replay_count"], 1)
        self.assertEqual(
            self._target_rows(),
            [(1, "synthetic-a", True), (2, "synthetic-b", False)],
        )

        second = migrate_sqlite_to_postgres(
            source=source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(second["replay_count"], 2)
        self.assertEqual(self._target_count(), 2)
        self.assertEqual(
            self._target_rows(),
            [(1, "synthetic-a", True), (2, "synthetic-b", False)],
        )

        with psycopg.connect(self.dsn) as pg:
            run_count = pg.execute(
                sql.SQL(
                    "SELECT run_count FROM {}.{} WHERE correlation_id = %s"
                ).format(
                    sql.Identifier(self.schema),
                    sql.Identifier("_migration_runs"),
                ),
                (correlation_id,),
            ).fetchone()[0]
        self.assertEqual(run_count, 2)

    def test_changed_source_reusing_correlation_id_is_rejected(self) -> None:
        source = self._create_source()
        correlation_id = f"migration-{uuid.uuid4().hex}"
        migrate_sqlite_to_postgres(
            source=source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha="test-head",
        )

        with sqlite3.connect(source) as db:
            db.execute(
                "INSERT INTO demands(id, title, active) VALUES (?, ?, ?)",
                (3, "synthetic-c", 1),
            )
            db.commit()

        with self.assertRaisesRegex(
            MigrationConflictError,
            "different source fingerprint",
        ):
            migrate_sqlite_to_postgres(
                source=source,
                postgres_dsn=self.dsn,
                schema=self.schema,
                correlation_id=correlation_id,
                environment="ci",
                automation_sha="test-head",
            )

        self.assertEqual(self._target_count(), 2)
        self.assertEqual(
            self._target_rows(),
            [(1, "synthetic-a", True), (2, "synthetic-b", False)],
        )

    def test_table_without_primary_key_fails_before_destination_mutation(self) -> None:
        source = self.root / "no-pk.sqlite"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE notes (body TEXT NOT NULL)")
            db.execute("INSERT INTO notes(body) VALUES ('synthetic')")
            db.commit()

        with self.assertRaisesRegex(PreflightError, "primary key"):
            preflight_migration(
                source=source,
                postgres_dsn=self.dsn,
                schema=self.schema,
                environment="ci",
            )

        with psycopg.connect(self.dsn) as pg:
            schema_exists = pg.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                (self.schema,),
            ).fetchone()[0]
        self.assertFalse(schema_exists)


if __name__ == "__main__":
    unittest.main()
