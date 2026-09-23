#!/usr/bin/env python3
"""Create a verifiable SQLite backup and manifest using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
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


def create_backup(
    source: str | Path,
    output_dir: str | Path,
    correlation_id: str,
    environment: str = "local",
    automation_sha: str | None = None,
) -> dict[str, Any]:
    source_path = Path(source).resolve()
    output_path = Path(output_dir).resolve()

    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if not correlation_id.strip():
        raise ValueError("correlation_id is required")

    output_path.mkdir(parents=True, exist_ok=True)
    artifact_path = output_path / f"{correlation_id}.sqlite"
    manifest_path = output_path / f"{correlation_id}.manifest.json"

    if source_path == artifact_path:
        raise ValueError("source and backup artifact must be different files")

    with sqlite3.connect(source_path) as source_db:
        integrity = source_db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"source integrity check failed: {integrity}")

        row_counts = _row_counts(source_db)
        temp_artifact = artifact_path.with_suffix(".sqlite.tmp")
        if temp_artifact.exists():
            temp_artifact.unlink()

        with sqlite3.connect(temp_artifact) as backup_db:
            source_db.backup(backup_db)
        os.replace(temp_artifact, artifact_path)

    manifest = {
        "schema_version": 1,
        "engine": "sqlite",
        "environment": environment,
        "correlation_id": correlation_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "automation_sha": automation_sha or os.environ.get("GITHUB_SHA") or "local",
        "artifact": artifact_path.name,
        "size_bytes": artifact_path.stat().st_size,
        "sha256": _sha256(artifact_path),
        "row_counts": row_counts,
    }

    temp_manifest = manifest_path.with_suffix(".json.tmp")
    temp_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_manifest, manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verifiable SQLite backup")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--environment", default="local")
    args = parser.parse_args()

    manifest = create_backup(
        source=args.source,
        output_dir=args.output_dir,
        correlation_id=args.correlation_id,
        environment=args.environment,
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
