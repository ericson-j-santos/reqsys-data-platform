#!/usr/bin/env python3
"""Validate repository governance controls without external dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED_FILES = (
    "AGENTS.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    "docs/gold-standard.md",
)

REQUIRED_JOBS = (
    "repository-hygiene:",
    "backup-restore-e2e:",
    "sqlite-postgres-e2e:",
    "data-quality-e2e:",
)

ACTION_REF_RE = re.compile(r"^\s*-\s+uses:\s+[^@\s]+@([^\s#]+)", re.MULTILINE)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_repository(root: Path) -> list[str]:
    violations: list[str] = []

    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            violations.append(f"missing required governance file: {relative}")

    workflows_dir = root / ".github" / "workflows"
    workflows = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    if not workflows:
        violations.append("no GitHub Actions workflow found")
        return violations

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        if "pull_request_target:" in text:
            violations.append(f"unsafe trigger pull_request_target: {workflow}")
        for ref in ACTION_REF_RE.findall(text):
            if not FULL_SHA_RE.fullmatch(ref):
                violations.append(f"mutable GitHub Action reference @{ref}: {workflow}")

    data_safety = root / ".github" / "workflows" / "data-safety.yml"
    if not data_safety.is_file():
        violations.append("missing Data Safety Gate workflow")
        return violations

    text = data_safety.read_text(encoding="utf-8")
    for job in REQUIRED_JOBS:
        if job not in text:
            violations.append(f"missing required Data Safety Gate job: {job[:-1]}")

    if "persist-credentials: false" not in text:
        violations.append("checkout must disable persisted credentials")
    if "permissions:\n  contents: read" not in text:
        violations.append("workflow must declare least-privilege contents: read")
    if "timeout-minutes:" not in text:
        violations.append("workflow jobs must declare timeouts")

    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = validate_repository(root)
    if violations:
        print("GOVERNANCE_CONTRACT=failed")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1

    print("GOVERNANCE_CONTRACT=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
