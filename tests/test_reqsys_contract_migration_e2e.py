from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import errors, sql

from migrations.sqlite_to_postgres import migrate_sqlite_to_postgres


class ReqSysLikeSQLiteToPostgresE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("POSTGRES_DSN", "")
        if not cls.dsn:
            raise unittest.SkipTest("POSTGRES_DSN is required for PostgreSQL E2E")

    def setUp(self) -> None:
        self.schema = f"e2e_reqsys_{uuid.uuid4().hex[:12]}"
        self.tempdir = tempfile.TemporaryDirectory()
        self.source = Path(self.tempdir.name) / "reqsys-like.sqlite"
        with sqlite3.connect(self.source) as db:
            db.execute("PRAGMA foreign_keys = ON")
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
            db.execute(
                "CREATE INDEX ix_projects_reference_date ON projects(reference_date)"
            )
            db.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    active BOOLEAN NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            db.execute("CREATE INDEX ix_tasks_project_id ON tasks(project_id)")
            db.execute(
                """
                INSERT INTO projects(
                    id, code, reference_date, created_at, provider, amount, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    "REQSYS-SYNTHETIC",
                    "2026-09-23",
                    "2026-09-23 12:34:56",
                    "hash-local-256",
                    "12.340000",
                    json.dumps({"kind": "synthetic", "values": [1, 2]}),
                ),
            )
            db.execute(
                "INSERT INTO tasks(id, project_id, title, active) VALUES (?, ?, ?, ?)",
                (11, 7, "synthetic-task", 1),
            )
            db.commit()

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as pg:
            pg.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )
        self.tempdir.cleanup()

    def test_reqsys_contracts_and_replay(self) -> None:
        correlation_id = f"reqsys-contract-{uuid.uuid4().hex}"
        first = migrate_sqlite_to_postgres(
            source=self.source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(first["replay_count"], 1)
        self.assertEqual(first["tables"]["projects"]["target_count"], 1)
        self.assertEqual(first["tables"]["tasks"]["target_count"], 1)

        with psycopg.connect(self.dsn) as pg:
            project = pg.execute(
                sql.SQL(
                    "SELECT id, code, reference_date, provider, amount, payload "
                    "FROM {}.{} WHERE id = 7"
                ).format(sql.Identifier(self.schema), sql.Identifier("projects"))
            ).fetchone()
            self.assertEqual(project[0], 7)
            self.assertEqual(project[1], "REQSYS-SYNTHETIC")
            self.assertEqual(project[2].isoformat(), "2026-09-23")
            self.assertEqual(project[3], "hash-local-256")
            self.assertEqual(str(project[4]), "12.340000")
            self.assertEqual(project[5], {"kind": "synthetic", "values": [1, 2]})

            next_id = pg.execute(
                sql.SQL(
                    "INSERT INTO {}.{}(code, reference_date, amount, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb) RETURNING id, provider, created_at"
                ).format(sql.Identifier(self.schema), sql.Identifier("projects")),
                ("REQSYS-NEXT", "2026-09-24", "1.000000", '{"ok":true}'),
            ).fetchone()
            self.assertGreater(next_id[0], 7)
            self.assertEqual(next_id[1], "hash-local-256")
            self.assertIsNotNone(next_id[2])

            with self.assertRaises(errors.ForeignKeyViolation):
                with pg.transaction():
                    pg.execute(
                        sql.SQL(
                            "INSERT INTO {}.{}(project_id, title, active) "
                            "VALUES (999999, 'blocked', true)"
                        ).format(sql.Identifier(self.schema), sql.Identifier("tasks"))
                    )

            with self.assertRaises(errors.UniqueViolation):
                with pg.transaction():
                    pg.execute(
                        sql.SQL(
                            "INSERT INTO {}.{}(code, reference_date) "
                            "VALUES ('REQSYS-SYNTHETIC', '2026-09-25')"
                        ).format(sql.Identifier(self.schema), sql.Identifier("projects"))
                    )

            index_names = {
                row[0]
                for row in pg.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = %s AND tablename IN ('projects', 'tasks')",
                    (self.schema,),
                ).fetchall()
            }
            self.assertIn("ix_projects_reference_date", index_names)
            self.assertIn("ix_tasks_project_id", index_names)

        second = migrate_sqlite_to_postgres(
            source=self.source,
            postgres_dsn=self.dsn,
            schema=self.schema,
            correlation_id=correlation_id,
            environment="ci",
            automation_sha=os.environ.get("GITHUB_SHA", "test-head"),
        )
        self.assertEqual(second["replay_count"], 2)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(
                pg.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE id IN (7, 11)").format(
                        sql.Identifier(self.schema), sql.Identifier("projects")
                    )
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
