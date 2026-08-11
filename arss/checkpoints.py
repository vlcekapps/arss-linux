"""Durable XDG checkpoint backend for in-process feed monitoring."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Final

from .monitor import (
    CheckpointEdge,
    CheckpointResult,
    CheckpointState,
    LegacyAliases,
    MonitorKind,
    evaluate_checkpoint,
)


MAXIMUM_CHECKPOINT_FILE_BYTES: Final = 2 * 1024 * 1024
_LOCK = threading.RLock()


def default_checkpoint_path(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    resolved_home = Path.home() if home is None else Path(home)
    configured = values.get("XDG_STATE_HOME")
    state_home = Path(configured).expanduser() if configured else resolved_home / ".local" / "state"
    if not state_home.is_absolute():
        state_home = resolved_home / ".local" / "state"
    return state_home / "arss" / "checkpoints.json"


class JsonCheckpointBackend:
    """Store bounded transient baselines without including them in OPML data."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_checkpoint_path()

    def load(self, kind: MonitorKind, feed_url: str) -> CheckpointState | None:
        key = _key(kind, feed_url)
        with _LOCK:
            root = self._read()
            return _checkpoint_state(root.get(key))

    def save(self, kind: MonitorKind, feed_url: str, state: CheckpointState) -> None:
        key = _key(kind, feed_url)
        with _LOCK:
            # RSS and podcast systemd services are independent and may finish
            # together.  Serializing each read-modify-write across processes
            # prevents one kind from erasing the other's freshly saved state.
            with _exclusive_process_lock(self.path):
                root = self._read()
                root[key] = _serialized_state(state)
                self._write_root(root)

    def record_success_atomic(
        self,
        kind: MonitorKind,
        feed_url: str,
        article_ids: Iterable[str],
        legacy_aliases_by_id: LegacyAliases | None = None,
    ) -> CheckpointResult:
        """Compare and commit one feed while holding the process lock.

        The GUI and a systemd user service can share this file. Locking only
        the final JSON write would let both processes evaluate the same stale
        baseline and announce an item which the user had just opened.
        """

        normalized_kind = MonitorKind(kind)
        normalized_url = feed_url.strip()
        key = _key(normalized_kind, normalized_url)
        current_ids = tuple(article_ids)
        with _LOCK:
            with _exclusive_process_lock(self.path):
                root = self._read()
                previous = _checkpoint_state(root.get(key))
                decision = evaluate_checkpoint(
                    has_baseline=previous is not None,
                    previous_ids=previous.ids if previous is not None else (),
                    current_ids=current_ids,
                    legacy_aliases_by_id=legacy_aliases_by_id,
                    previous_edge=previous.edge
                    if previous is not None
                    else CheckpointEdge.UNKNOWN,
                    previous_complete=previous.complete
                    if previous is not None
                    else True,
                )
                root[key] = _serialized_state(decision.state)
                self._write_root(root)
        return CheckpointResult(
            is_baseline=previous is None,
            new_ids=decision.new_ids,
        )

    def _write_root(self, root: Mapping[str, Any]) -> None:
        payload = json.dumps(
            root,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAXIMUM_CHECKPOINT_FILE_BYTES:
            raise OSError("Checkpoint file exceeds its byte limit")
        _atomic_write(self.path, payload)

    def _read(self) -> dict[str, Any]:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        if len(payload) > MAXIMUM_CHECKPOINT_FILE_BYTES:
            return {}
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def _checkpoint_state(raw: object) -> CheckpointState | None:
    if not isinstance(raw, dict):
        return None
    ids = raw.get("ids")
    if not isinstance(ids, list) or not all(
        isinstance(value, str) for value in ids
    ):
        return None
    try:
        edge = CheckpointEdge(
            str(raw.get("edge", CheckpointEdge.UNKNOWN.value))
        )
        complete = raw.get("complete", True)
        if type(complete) is not bool:
            return None
        return CheckpointState(tuple(ids), edge, complete)
    except (ValueError, TypeError):
        return None


def _serialized_state(state: CheckpointState) -> dict[str, object]:
    return {
        "ids": list(state.ids),
        "edge": state.edge.value,
        "complete": state.complete,
    }


def _key(kind: MonitorKind, feed_url: str) -> str:
    digest = hashlib.sha256(feed_url.strip().encode("utf-8")).hexdigest()
    return f"{MonitorKind(kind).value}:{digest}"


@contextmanager
def _exclusive_process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f"{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
