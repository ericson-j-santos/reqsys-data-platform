from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_quality.validator import ContractError, validate_sqlite


class DataQualityE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _database(self, *, invalid: bool = False) -> Path:
        path = self.root / "quality.sqlite"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            db.execute(
                """
                CREATE TABLE demands (
                    id INTEGER PRIMARY KEY,
                    external_key TEXT,
                    title TEXT,
                    status TEXT,
                    owner_id INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            db.execute(
                "INSERT INTO owners(id, name) VALUES (?, ?)",
                (1, "synthetic-owner"),
            )
            if invalid:
                rows = [
                    (
                        1,
                        "ext-1",
                        "",
                        "PRIVATE_SENTINEL",
                        99,
                        "2026-09-24T10:00:00Z",
                        "2026-09-23T10:00:00Z",
                    ),
                    (
                        2,
                        "ext-1",
                        "synthetic-valid",
                        "OPEN",
                        1,
                        "2026-09-23T10:00:00Z",
                        "2026-09-24T10:00:00Z",
                    ),
                ]
            else:
                rows = [
                    (
                        1,
                        "ext-1",
                        "synthetic-a",
                        "OPEN",
                        1,
                        "2026-09-23T10:00:00Z",
                        "2026-09-23T11:00:00Z",
                    ),
                    (
                        2,
                        "ext-2",
                        "synthetic-b",
                        "DONE",
                        1,
                        "2026-09-23T12:00:00Z",
                        "2026-09-23T13:00:00Z",
                    ),
                ]
            db.executemany(
                """
                INSERT INTO demands(
                    id, external_key, title, status, owner_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            db.commit()
        return path

    def _contract(self) -> dict:
        return {
            "schema_version": 1,
            "tables": {
                "demands": {
                    "required": ["external_key", "title", "status"],
                    "unique": [["external_key"]],
                    "domains": {"status": ["OPEN", "DONE"]},
                    "temporal": [
                        {"start": "created_at", "end": "updated_at"}
                    ],
                    "foreign_keys": [
                        {
                            "columns": ["owner_id"],
                            "ref_table": "owners",
                            "ref_columns": ["id"],
                        }
                    ],
                    "idempotency_keys": [["external_key"]],
                }
            },
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_valid_data_passes_and_replay_is_deterministic(self) -> None:
        source = self._database()
        before = self._sha256(source)

        first = validate_sqlite(
            source,
            self._contract(),
            "quality-positive",
            environment="ci",
            automation_sha="test-head",
        )
        second = validate_sqlite(
            source,
            self._contract(),
            "quality-positive",
            environment="ci",
            automation_sha="test-head",
        )

        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["violation_total"], 0)
        self.assertEqual(first["table_row_counts"], {"demands": 2})
        self.assertEqual(first, second)
        self.assertEqual(before, self._sha256(source))

    def test_invalid_data_fails_all_configured_dimensions_without_values(self) -> None:
        source = self._database(invalid=True)

        evidence = validate_sqlite(
            source,
            self._contract(),
            "quality-negative",
            environment="ci",
            automation_sha="test-head",
        )

        self.assertEqual(evidence["status"], "failed")
        by_dimension = {}
        for item in evidence["rules"]:
            by_dimension[item["dimension"]] = (
                by_dimension.get(item["dimension"], 0)
                + item["violation_count"]
            )
        self.assertEqual(
            by_dimension,
            {
                "completeness": 1,
                "uniqueness": 1,
                "domain_validity": 1,
                "temporal_consistency": 1,
                "referential_integrity": 1,
                "idempotency": 1,
            },
        )
        self.assertEqual(evidence["violation_total"], 6)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(evidence, sort_keys=True))

    def test_unknown_column_is_rejected_fail_closed(self) -> None:
        source = self._database()
        contract = self._contract()
        contract["tables"]["demands"]["required"].append("does_not_exist")

        with self.assertRaisesRegex(ContractError, "unknown column"):
            validate_sqlite(
                source,
                contract,
                "quality-contract-negative",
                environment="ci",
                automation_sha="test-head",
            )


if __name__ == "__main__":
    unittest.main()
