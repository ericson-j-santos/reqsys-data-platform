from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup.sqlite_backup import create_backup
from restore.sqlite_restore import restore_backup


class BackupRestoreE2ETest(unittest.TestCase):
    def _create_source(self, root: Path) -> Path:
        source = root / "source.sqlite"
        with sqlite3.connect(source) as db:
            db.execute(
                "CREATE TABLE demands (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
            )
            db.executemany(
                "INSERT INTO demands(id, title) VALUES (?, ?)",
                [(1, "synthetic-a"), (2, "synthetic-b")],
            )
            db.commit()
        return source

    def _count_demands(self, path: Path) -> int:
        with sqlite3.connect(path) as db:
            return int(db.execute("SELECT COUNT(*) FROM demands").fetchone()[0])

    def test_positive_restore_and_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._create_source(root)
            output = root / "backup"
            correlation_id = "e2e-backup-restore-positive"

            manifest = create_backup(
                source=source,
                output_dir=output,
                correlation_id=correlation_id,
                environment="ci",
                automation_sha="test-head",
            )
            artifact = output / manifest["artifact"]
            manifest_file = output / f"{correlation_id}.manifest.json"
            target = root / "restore" / "restored.sqlite"

            first = restore_backup(artifact, manifest_file, target)
            self.assertEqual(first["status"], "restored")
            self.assertEqual(self._count_demands(target), 2)
            self.assertEqual(self._count_demands(source), 2)

            second = restore_backup(artifact, manifest_file, target, replace=True)
            self.assertEqual(second["row_counts"], {"demands": 2})
            self.assertEqual(self._count_demands(target), 2)
            self.assertEqual(self._count_demands(source), 2)

    def test_corrupted_artifact_is_rejected_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._create_source(root)
            output = root / "backup"
            correlation_id = "e2e-backup-restore-negative"

            manifest = create_backup(
                source=source,
                output_dir=output,
                correlation_id=correlation_id,
                environment="ci",
                automation_sha="test-head",
            )
            artifact = output / manifest["artifact"]
            manifest_file = output / f"{correlation_id}.manifest.json"
            target = root / "restore" / "should-not-exist.sqlite"

            data = bytearray(artifact.read_bytes())
            data[-1] = (data[-1] + 1) % 256
            artifact.write_bytes(bytes(data))

            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                restore_backup(artifact, manifest_file, target)

            self.assertFalse(target.exists())
            self.assertEqual(self._count_demands(source), 2)

    def test_manifest_must_reference_exact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._create_source(root)
            output = root / "backup"
            correlation_id = "e2e-backup-restore-manifest"

            manifest = create_backup(
                source=source,
                output_dir=output,
                correlation_id=correlation_id,
                environment="ci",
                automation_sha="test-head",
            )
            artifact = output / manifest["artifact"]
            manifest_file = output / f"{correlation_id}.manifest.json"
            target = root / "restore" / "should-not-exist.sqlite"

            altered = json.loads(manifest_file.read_text(encoding="utf-8"))
            altered["artifact"] = "other.sqlite"
            manifest_file.write_text(json.dumps(altered), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "artifact name"):
                restore_backup(artifact, manifest_file, target)

            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
