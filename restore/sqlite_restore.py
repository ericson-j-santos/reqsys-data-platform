#!/usr/bin/env python3
"""Restore a SQLite backup only after manifest and integrity validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {
        name: int(connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()[0])
        for (name,) in rows
    }


def restore_backup(
    artifact: str | Path,
    manifest_file: str | Path,
    target: str | Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    artifact_path = Path(artifact).resolve()
    manifest_path = Path(manifest_file).resolve()
    target_path = Path(target).resolve()

    if not artifact_path.is_file():
        raise FileNotFoundError(f"backup artifact does not exist: {artifact_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest does not exist: {manifest_path}")
    if target_path.exists() and not replace:
        raise FileExistsError(f"target already exists: {target_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("engine") != "sqlite":
        raise ValueError("manifest engine must be sqlite")
    if manifest.get("artifact") != artifact_path.name:
        raise ValueError("manifest artifact name does not match backup artifact")

    observed_hash = _sha256(artifact_path)
    if observed_hash != manifest.get("sha256"):
        raise ValueError("backup sha256 mismatch")

    with sqlite3.connect(artifact_path) as backup_db:
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"backup integrity check failed: {integrity}")

        observed_counts = _row_counts(backup_db)
        if observed_counts != manifest.get("row_counts", {}):
            raise ValueError("backup row counts do not match manifest")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target_path.with_suffix(target_path.suffix + ".tmp")
        if temp_target.exists():
            temp_target.unlink()

        with sqlite3.connect(temp_target) as restored_db:
            backup_db.backup(restored_db)
        os.replace(temp_target, target_path)

    with sqlite3.connect(target_path) as restored_db:
        restored_integrity = restored_db.execute("PRAGMA integrity_check").fetchone()[0]
        if restored_integrity != "ok":
            raise ValueError(f"restored database integrity check failed: {restored_integrity}")
        restored_counts = _row_counts(restored_db)

    if restored_counts != manifest.get("row_counts", {}):
        raise ValueError("restored row counts do not match manifest")

    return {
        "status": "restored",
        "correlation_id": manifest.get("correlation_id"),
        "target": str(target_path),
        "sha256": observed_hash,
        "row_counts": restored_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a verified SQLite backup")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    result = restore_backup(
        artifact=args.artifact,
        manifest_file=args.manifest,
        target=args.target,
        replace=args.replace,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
