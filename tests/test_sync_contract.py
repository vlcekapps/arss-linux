from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_contract import ContractFixture


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "arss_sync_contract",
    ROOT / "tools" / "sync-contract.py",
)
assert SPEC is not None and SPEC.loader is not None
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class SyncContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="arss-sync-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        ContractFixture(self.source).write()
        self.git("init")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=ARSS test",
            "-c",
            "user.email=arss@example.test",
            "commit",
            "-m",
            "Synthetic contract",
        )
        self.git("remote", "add", "origin", SYNC.DEFAULT_REPOSITORY)
        self.git("tag", "v1.0.0")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.destination = self.root / "destination"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.source,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        return result.stdout

    def arguments(self, *extra: str) -> list[str]:
        return [
            "--source",
            str(self.source),
            "--destination",
            str(self.destination),
            "--source-tag",
            "v1.0.0",
            "--source-commit",
            self.commit,
            *extra,
        ]

    def test_valid_release_is_complete_and_check_mode_detects_no_drift(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        self.assertTrue((self.destination / "contract.lock.json").is_file())
        self.assertTrue(
            (self.destination / "golden/guide/stations.normalized.json").is_file()
        )
        self.assertEqual(0, SYNC.main(self.arguments("--check")))

    def test_invalid_source_does_not_modify_existing_destination(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        before = SYNC.tree_files(self.destination)
        catalog = self.source / "catalogs/guide_stations.json"
        catalog.write_bytes(catalog.read_bytes() + b"tampered")
        self.assertEqual(2, SYNC.main(self.arguments()))
        self.assertEqual(before, SYNC.tree_files(self.destination))

    def test_check_mode_reports_drift_without_modifying_destination(self) -> None:
        self.destination.mkdir()
        marker = self.destination / "old.txt"
        marker.write_text("keep", encoding="utf-8")
        self.assertEqual(1, SYNC.main(self.arguments("--check")))
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_rejects_unversioned_or_mismatched_tag(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("v1.0.0")] = "main"
        self.assertEqual(2, SYNC.main(arguments))
        self.assertFalse(self.destination.exists())

    def test_rejects_forged_commit_and_dirty_checkout(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index(self.commit)] = "0" * 40
        self.assertEqual(2, SYNC.main(arguments))
        self.assertFalse(self.destination.exists())

        (self.source / "untracked.txt").write_text("dirty", encoding="utf-8")
        self.assertEqual(2, SYNC.main(self.arguments()))
        self.assertFalse(self.destination.exists())

    def test_failed_swap_restores_the_previous_known_good_tree(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        before = SYNC.tree_files(self.destination)
        staged = self.root / "staged"
        shutil.copytree(self.destination, staged)
        lock = staged / "contract.lock.json"
        lock.write_text(
            lock.read_text(encoding="utf-8").replace(
                SYNC.DEFAULT_REPOSITORY,
                "https://example.test/arss-contract",
            ),
            encoding="utf-8",
        )
        real_replace = os.replace
        calls = 0

        def fail_second_replace(source: object, destination: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted swap")
            real_replace(source, destination)

        with patch.object(SYNC.os, "replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "interrupted swap"):
                SYNC.install_staged(staged, self.destination)
        self.assertEqual(before, SYNC.tree_files(self.destination))
        self.assertFalse((self.root / ".destination.previous").exists())
        self.assertFalse((self.root / ".destination.previous.marker").exists())

    def test_next_install_recovers_a_crash_between_directory_renames(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        staged = self.root / "staged"
        shutil.copytree(self.destination, staged)
        backup = self.root / ".destination.previous"
        marker = self.root / ".destination.previous.marker"
        marker.write_bytes(SYNC._BACKUP_MARKER)
        os.replace(self.destination, backup)

        self.assertFalse(SYNC.install_staged(staged, self.destination))
        self.assertTrue((self.destination / "contract.lock.json").is_file())
        self.assertFalse(backup.exists())
        self.assertFalse(marker.exists())

    def test_unmarked_backup_collision_is_never_deleted(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        staged = self.root / "staged"
        backup = self.root / ".destination.previous"
        shutil.copytree(self.destination, staged)
        shutil.copytree(self.destination, backup)

        with self.assertRaisesRegex(SYNC.ContractError, "trusted marker"):
            SYNC.install_staged(staged, self.destination)
        self.assertTrue((self.destination / "contract.lock.json").is_file())
        self.assertTrue((backup / "contract.lock.json").is_file())

    def test_nondirectory_destination_is_rejected_before_any_swap(self) -> None:
        self.destination.write_text("keep this file", encoding="utf-8")

        self.assertEqual(2, SYNC.main(self.arguments()))

        self.assertEqual("keep this file", self.destination.read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".destination.previous").exists())
        self.assertFalse((self.root / ".destination.previous.marker").exists())

    def test_nondirectory_stale_backup_is_never_removed(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        staged = self.root / "staged"
        shutil.copytree(self.destination, staged)
        backup = self.root / ".destination.previous"
        marker = self.root / ".destination.previous.marker"
        backup.write_text("untrusted collision", encoding="utf-8")
        marker.write_bytes(SYNC._BACKUP_MARKER)

        with self.assertRaisesRegex(SYNC.ContractError, "not a directory"):
            SYNC.install_staged(staged, self.destination)

        self.assertTrue((self.destination / "contract.lock.json").is_file())
        self.assertEqual("untrusted collision", backup.read_text(encoding="utf-8"))
        self.assertEqual(SYNC._BACKUP_MARKER, marker.read_bytes())

    def test_invalid_marker_without_backup_is_never_removed(self) -> None:
        self.assertEqual(0, SYNC.main(self.arguments()))
        before = SYNC.tree_files(self.destination)
        staged = self.root / "staged"
        shutil.copytree(self.destination, staged)
        marker = self.root / ".destination.previous.marker"
        marker.write_bytes(b"foreign marker\n")

        with self.assertRaisesRegex(SYNC.ContractError, "marker is invalid"):
            SYNC.install_staged(staged, self.destination)

        self.assertEqual(before, SYNC.tree_files(self.destination))
        self.assertEqual(b"foreign marker\n", marker.read_bytes())

    def test_rejects_a_symlinked_destination_component(self) -> None:
        target = self.root / "outside"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exception:
            self.skipTest(f"Directory symlinks are unavailable: {exception}")
        arguments = self.arguments(
            "--destination",
            str(link / "contract"),
        )
        self.assertEqual(2, SYNC.main(arguments))
        self.assertEqual([], list(target.iterdir()))


if __name__ == "__main__":
    unittest.main()
