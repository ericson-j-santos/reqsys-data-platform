#!/usr/bin/env python3
"""Fail closed when forbidden data/security artifacts are tracked."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".dump", ".bak", ".backup", ".bkp",
    ".parquet", ".feather", ".avro", ".orc", ".p12", ".pfx", ".pem", ".key",
}

FORBIDDEN_NAMES = {
    ".env",
}

SENSITIVE_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]

TEXT_LIMIT_BYTES = 1_000_000


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(p.decode("utf-8")) for p in result.stdout.split(b"\0") if p]


def is_forbidden_path(path: Path) -> bool:
    lower = path.name.lower()
    if lower in FORBIDDEN_NAMES:
        return True
    return path.suffix.lower() in FORBIDDEN_SUFFIXES


def scan_text(path: Path) -> list[str]:
    try:
        if path.stat().st_size > TEXT_LIMIT_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings = []
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            findings.append(pattern.pattern)
    return findings


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        if is_forbidden_path(path):
            violations.append(f"forbidden tracked artifact: {path}")
            continue
        for _pattern in scan_text(path):
            violations.append(f"possible secret material: {path}")

    if violations:
        print("REPOSITORY_HYGIENE=failed")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1

    print("REPOSITORY_HYGIENE=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
