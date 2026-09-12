from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

from arss.contract import ContractError, DEFAULT_CONTRACT_DIRECTORY


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "arss_validate_vendored_contract",
    ROOT / "tools" / "validate_vendored_contract.py",
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ConsumerContractValidatorTest(unittest.TestCase):
    def test_public_snapshot_and_consumer_lock_validate_without_mutating_snapshot(self) -> None:
        VALIDATOR.validate_vendored_contract(DEFAULT_CONTRACT_DIRECTORY)

    def test_consumer_wrapper_rejects_unmanifested_files_other_than_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arss-consumer-contract-") as temporary:
            copied = Path(temporary) / "contract"
            shutil.copytree(DEFAULT_CONTRACT_DIRECTORY, copied)
            (copied / "unexpected.txt").write_text("not public", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unexpected.txt"):
                VALIDATOR.validate_vendored_contract(copied)

    def test_consumer_wrapper_never_executes_vendored_python(self) -> None:
        source = (ROOT / "tools" / "validate_vendored_contract.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("runpy", source)
        self.assertNotIn("exec(", source)

    def test_sync_workflow_isolates_candidate_data_from_write_credentials(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "sync-contract.yml"
        ).read_text(encoding="utf-8")
        validate, update = workflow.split("\n  update:\n", 1)
        self.assertIn("permissions:\n  contents: read", validate)
        self.assertNotIn("contents: write", validate)
        self.assertNotIn("pull-requests: write", validate)
        self.assertIn("contents: write", update)
        self.assertIn("pull-requests: write", update)
        self.assertGreaterEqual(workflow.count("persist-credentials: false"), 3)
        handoff = workflow.split("- name: Hand off validated contract data only", 1)[1]
        handoff = handoff.split("\n  update:\n", 1)[0]
        self.assertIn("include-hidden-files: true", handoff)
        before_push, push = update.split(
            "- name: Push the update branch and open or refresh a pull request",
            1,
        )
        self.assertNotIn("GH_TOKEN:", before_push)
        self.assertIn("GH_TOKEN: ${{ github.token }}", push)
        self.assertIn("git add -f -- arss/data/contract", push)
        self.assertNotIn("contract/tools/validate.py", workflow)


if __name__ == "__main__":
    unittest.main()
