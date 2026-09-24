from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_governance_contract import REQUIRED_FILES, validate_repository

GOOD_WORKFLOW = """name: Data Safety Gate

on:
  pull_request:

permissions:
  contents: read

jobs:
  repository-hygiene:
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          persist-credentials: false
  backup-restore-e2e:
    timeout-minutes: 5
  sqlite-postgres-e2e:
    timeout-minutes: 10
  data-quality-e2e:
    timeout-minutes: 5
"""


class GovernanceContractTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        for relative in REQUIRED_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ok\n", encoding="utf-8")

        workflow = root / ".github" / "workflows" / "data-safety.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(GOOD_WORKFLOW, encoding="utf-8")
        return root

    def test_valid_repository_passes(self) -> None:
        root = self.make_repo()
        self.assertEqual([], validate_repository(root))

    def test_mutable_action_reference_fails(self) -> None:
        root = self.make_repo()
        workflow = root / ".github" / "workflows" / "data-safety.yml"
        workflow.write_text(GOOD_WORKFLOW.replace(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "actions/checkout@v4",
        ), encoding="utf-8")

        violations = validate_repository(root)
        self.assertTrue(any("mutable GitHub Action reference" in item for item in violations))

    def test_missing_governance_file_fails(self) -> None:
        root = self.make_repo()
        (root / "CONTRIBUTING.md").unlink()

        violations = validate_repository(root)
        self.assertIn(
            "missing required governance file: CONTRIBUTING.md",
            violations,
        )


if __name__ == "__main__":
    unittest.main()
