from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arss.checkpoints import JsonCheckpointBackend, default_checkpoint_path
from arss.monitor import (
    CheckpointEdge,
    CheckpointState,
    CheckpointStore,
    MonitorKind,
)


class JsonCheckpointBackendTest(unittest.TestCase):
    def test_state_round_trips_and_kinds_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoints.json"
            backend = JsonCheckpointBackend(path)
            rss = CheckpointState(("one", "two"), CheckpointEdge.START, False)
            backend.save(MonitorKind.RSS, "https://example.test/feed", rss)
            self.assertEqual(rss, backend.load(MonitorKind.RSS, "https://example.test/feed"))
            self.assertIsNone(backend.load(MonitorKind.PODCAST, "https://example.test/feed"))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o600, path.with_name("checkpoints.json.lock").stat().st_mode & 0o777)

    def test_store_uses_atomic_json_compare_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = JsonCheckpointBackend(
                Path(temporary) / "checkpoints.json"
            )
            first = CheckpointStore(backend).record_success(
                MonitorKind.RSS,
                "https://example.test/feed",
                ("old",),
            )
            second = CheckpointStore(backend).record_success(
                MonitorKind.RSS,
                "https://example.test/feed",
                ("new", "old"),
            )

            self.assertTrue(first.is_baseline)
            self.assertEqual(("new",), second.new_ids)
            state = backend.load(
                MonitorKind.RSS,
                "https://example.test/feed",
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(("new", "old"), state.ids[:2])

    def test_corrupt_runtime_state_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoints.json"
            path.write_text("not json", encoding="utf-8")
            backend = JsonCheckpointBackend(path)
            self.assertIsNone(backend.load(MonitorKind.RSS, "https://example.test/feed"))

    def test_xdg_state_home_must_be_absolute(self) -> None:
        home = Path("/tmp/example-home")
        self.assertEqual(
            home / ".local/state/arss/checkpoints.json",
            default_checkpoint_path({"XDG_STATE_HOME": "relative"}, home=home),
        )
        self.assertEqual(
            Path("/var/tmp/state/arss/checkpoints.json"),
            default_checkpoint_path({"XDG_STATE_HOME": "/var/tmp/state"}, home=home),
        )


if __name__ == "__main__":
    unittest.main()
