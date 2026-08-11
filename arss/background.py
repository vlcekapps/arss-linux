"""Opt-in systemd user monitoring without a graphical GTK dependency.

The installed timers are never enabled by package installation.  The settings
UI calls :class:`BackgroundMonitorManager` only after an explicit user choice;
the one-shot runner then checks that durable preference again before touching
the network.  RSS and podcast jobs use separate schedules and process locks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import FrameType
from typing import Any, Final, Protocol

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from .checkpoints import JsonCheckpointBackend, default_checkpoint_path
from .feed import FeedClient
from .i18n import Translator
from .monitor import CheckpointStore, FeedMonitor, MonitorKind, NewFeedItem
from .notifications import GNotificationPublisher
from .storage import FeedStore, JsonPreferences, PodcastStore, XdgPaths


APPLICATION_ID: Final = "cz.pvlcek.arss"
BACKGROUND_CHECKS_ENABLED: Final = "background_checks_enabled"
TIMER_UNIT_TEMPLATE: Final = "arss-monitor@{kind}.timer"
SERVICE_UNIT_TEMPLATE: Final = "arss-monitor@{kind}.service"
MAXIMUM_CADENCE_STATE_BYTES: Final = 4096
SUPPORTED_BACKGROUND_INTERVALS: Final = frozenset(
    {1, 5, 10, 15, 30, 45, 60, 180, 360, 720}
)


class PreferencesProtocol(Protocol):
    def load(self) -> dict[str, object]: ...

    def set(self, key: str, value: object) -> None: ...


class MonitorProtocol(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def run_once(self, kind: MonitorKind) -> tuple[NewFeedItem, ...]: ...

    def cancel(self) -> None: ...

    def resume(self) -> None: ...


class RunLockProtocol(Protocol):
    def try_acquire(self, kind: MonitorKind) -> bool: ...

    def release(self, kind: MonitorKind) -> None: ...


class CadenceProtocol(Protocol):
    def is_due(self, kind: MonitorKind, interval_minutes: int, now: int) -> bool: ...

    def mark_completed(self, kind: MonitorKind, completed_at: int) -> None: ...


class BackgroundMonitorError(RuntimeError):
    """The requested systemd user timer state could not be applied."""


class BackgroundRunResult(StrEnum):
    DISABLED = "disabled"
    MANUAL = "manual"
    NOT_DUE = "not-due"
    BUSY = "busy"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def default_background_state_directory(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    return default_checkpoint_path(environment, home=home).parent


def default_user_unit_config_directory(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    resolved_home = Path.home() if home is None else Path(home)
    configured = values.get("XDG_CONFIG_HOME")
    config_home = Path(configured).expanduser() if configured else resolved_home / ".config"
    if not config_home.is_absolute():
        config_home = resolved_home / ".config"
    return config_home / "systemd" / "user"


def calendar_expressions(interval_minutes: int) -> tuple[str, ...]:
    """Return exact local-wall-clock systemd calendar expressions."""

    if type(interval_minutes) is not int or interval_minutes not in SUPPORTED_BACKGROUND_INTERVALS:
        raise ValueError("Unsupported background check interval")
    if interval_minutes in {1, 5, 10, 15, 30}:
        return (f"*-*-* *:0/{interval_minutes}:00",)
    if interval_minutes == 60:
        return ("*-*-* *:00:00",)
    if interval_minutes in {180, 360, 720}:
        hours = interval_minutes // 60
        return (f"*-*-* 0/{hours}:00:00",)
    # Forty-five minutes divides a 24-hour day but not an hour.  Enumerating
    # the daily wall-clock occurrences avoids the 45/15-minute pattern that a
    # minute-field "0/45" expression would produce.
    return tuple(
        f"*-*-* {minute_of_day // 60:02d}:{minute_of_day % 60:02d}:00"
        for minute_of_day in range(0, 24 * 60, 45)
    )


def render_timer_drop_in(interval_minutes: int) -> bytes:
    lines = [
        "# Managed by ARSS. Manual edits will be replaced.",
        "[Timer]",
        "OnCalendar=",
    ]
    lines.extend(
        f"OnCalendar={expression}" for expression in calendar_expressions(interval_minutes)
    )
    lines.extend(("Persistent=true", ""))
    return "\n".join(lines).encode("utf-8")


class FileMonitorRunLock:
    """Non-blocking advisory locks shared by GUI and systemd monitor processes."""

    def __init__(self, directory: Path | None = None) -> None:
        root = directory or default_background_state_directory() / "locks"
        self.directory = Path(root)
        self._descriptors: dict[MonitorKind, int] = {}
        self._lock = threading.RLock()

    def try_acquire(self, kind: MonitorKind) -> bool:
        normalized_kind = MonitorKind(kind)
        with self._lock:
            if normalized_kind in self._descriptors:
                return False
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                pass
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                self.directory / f"{normalized_kind.value}.lock",
                flags,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return False
            except BaseException:
                os.close(descriptor)
                raise
            self._descriptors[normalized_kind] = descriptor
            return True

    def release(self, kind: MonitorKind) -> None:
        normalized_kind = MonitorKind(kind)
        with self._lock:
            descriptor = self._descriptors.pop(normalized_kind, None)
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def close(self) -> None:
        with self._lock:
            kinds = tuple(self._descriptors)
        for kind in kinds:
            self.release(kind)


class BackgroundCadenceStore:
    """Durable last-completion times used as a duplicate and clock guard."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or default_background_state_directory())

    def is_due(self, kind: MonitorKind, interval_minutes: int, now: int) -> bool:
        if type(interval_minutes) is not int or interval_minutes <= 0:
            return False
        previous = self._load(MonitorKind(kind))
        if previous is None or now < previous:
            return True
        return now - previous >= interval_minutes * 60

    def mark_completed(self, kind: MonitorKind, completed_at: int) -> None:
        if type(completed_at) is not int or completed_at < 0:
            raise ValueError("completed_at must be a non-negative integer")
        payload = (
            json.dumps(
                {"last_completed_epoch": completed_at, "version": 1},
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        _atomic_write(self._path(MonitorKind(kind)), payload)

    def _load(self, kind: MonitorKind) -> int | None:
        try:
            payload = self._path(kind).read_bytes()
        except OSError:
            return None
        if len(payload) > MAXIMUM_CADENCE_STATE_BYTES:
            return None
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(decoded, dict) or decoded.get("version") != 1:
            return None
        value = decoded.get("last_completed_epoch")
        return value if type(value) is int and value >= 0 else None

    def _path(self, kind: MonitorKind) -> Path:
        return self.directory / f"background-{kind.value}.json"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stderr: str = ""


SystemctlRunner = Callable[[Sequence[str]], Any]


class BackgroundMonitorManager:
    """Atomically coordinate the explicit preference and systemd user timers."""

    def __init__(
        self,
        preferences: PreferencesProtocol,
        *,
        unit_config_directory: Path | None = None,
        command_runner: SystemctlRunner | None = None,
    ) -> None:
        self.preferences = preferences
        self.unit_config_directory = Path(
            unit_config_directory or default_user_unit_config_directory()
        )
        self._command_runner = command_runner or self._run_systemctl
        # Settings changes and the explicit switch are dispatched through the
        # application's worker pool. Serialize their multi-command systemd
        # transactions so an interval refresh cannot race an enable/disable
        # rollback and leave the durable preference out of sync with timers.
        self._operation_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        with self._operation_lock:
            return self.preferences.load().get(BACKGROUND_CHECKS_ENABLED) is True

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        with self._operation_lock:
            self._set_enabled_locked(enabled)

    def _set_enabled_locked(self, enabled: bool) -> None:
        if not enabled:
            preference_error: BaseException | None = None
            try:
                # Persist the network gate before asking systemd to stop.  Even
                # if stopping takes time, the runner and notifier fail closed.
                self.preferences.set(BACKGROUND_CHECKS_ENABLED, False)
            except BaseException as error:
                preference_error = error
            self._apply_disabled(raise_errors=preference_error is None)
            if preference_error is not None:
                raise preference_error
            return
        # Timers are configured while the durable network gate is false.  A
        # Persistent= catch-up that fires during setup therefore exits safely.
        self.preferences.set(BACKGROUND_CHECKS_ENABLED, False)
        try:
            self._apply_enabled()
        except BaseException:
            self._apply_disabled(raise_errors=False)
            raise
        try:
            self.preferences.set(BACKGROUND_CHECKS_ENABLED, True)
        except BaseException:
            self._apply_disabled(raise_errors=False)
            raise

    def sync(self) -> None:
        """Apply changed intervals, falling back to foreground on failure."""

        with self._operation_lock:
            if not self.enabled:
                self._apply_disabled(raise_errors=True)
                return
            try:
                self._apply_enabled()
            except BaseException:
                self._apply_disabled(raise_errors=False)
                self.preferences.set(BACKGROUND_CHECKS_ENABLED, False)
                raise

    def _apply_enabled(self) -> None:
        preferences = self.preferences.load()
        desired: list[MonitorKind] = []
        undesired: list[MonitorKind] = []
        for kind in MonitorKind:
            interval = _interval_from_preferences(preferences, kind)
            if interval > 0:
                path = self._drop_in_path(kind)
                try:
                    _atomic_write(path, render_timer_drop_in(interval))
                except OSError as error:
                    raise BackgroundMonitorError(
                        f"Could not write the {kind.value} timer schedule"
                    ) from error
                desired.append(kind)
            else:
                undesired.append(kind)

        self._systemctl("daemon-reload")
        self._stop_kinds(undesired, raise_errors=True)
        if desired:
            timers = [_timer_unit(kind) for kind in desired]
            self._systemctl("enable", *timers)
            self._systemctl("restart", *timers)

    def _apply_disabled(self, *, raise_errors: bool) -> None:
        first_error: BackgroundMonitorError | None = None
        try:
            self._systemctl("daemon-reload")
        except BackgroundMonitorError as error:
            first_error = error
        try:
            self._stop_kinds(tuple(MonitorKind), raise_errors=True)
        except BackgroundMonitorError as error:
            if first_error is None:
                first_error = error
        if first_error is not None and raise_errors:
            raise first_error

    def _stop_kinds(
        self,
        kinds: Sequence[MonitorKind],
        *,
        raise_errors: bool,
    ) -> None:
        if not kinds:
            return
        timers = [_timer_unit(kind) for kind in kinds]
        services = [_service_unit(kind) for kind in kinds]
        first_error: BackgroundMonitorError | None = None
        try:
            self._systemctl("disable", "--now", *timers)
        except BackgroundMonitorError as error:
            first_error = error
        try:
            self._systemctl("stop", "--no-block", *services)
        except BackgroundMonitorError as error:
            if first_error is None:
                first_error = error
        if first_error is not None and raise_errors:
            raise first_error

    def _drop_in_path(self, kind: MonitorKind) -> Path:
        return (
            self.unit_config_directory
            / f"{_timer_unit(kind)}.d"
            / "schedule.conf"
        )

    def _systemctl(self, *arguments: str) -> None:
        command = ("systemctl", "--user", *arguments)
        try:
            result = self._command_runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            raise BackgroundMonitorError(f"Could not run {' '.join(command)}") from error
        returncode = getattr(result, "returncode", None)
        if returncode != 0:
            detail = str(getattr(result, "stderr", "")).strip()
            message = f"{' '.join(command)} failed"
            if detail:
                message += f": {detail}"
            raise BackgroundMonitorError(message)

    @staticmethod
    def _run_systemctl(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


class BackgroundNotificationSink:
    """Send a GNOME notification whose actions activate the regular app."""

    def __init__(
        self,
        preferences: PreferencesProtocol,
        *,
        application_factory: Callable[[], Any] | None = None,
        publisher_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.preferences = preferences
        self._application_factory = application_factory or (
            lambda: Gio.Application(
                application_id=APPLICATION_ID,
                # Never transiently own the desktop application's well-known
                # bus name. Notification actions will D-Bus-activate the
                # regular application even if clicked as this process exits.
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
        )
        self._publisher_factory = publisher_factory
        self._application: Any | None = None

    def __call__(
        self,
        kind: MonitorKind,
        items: tuple[NewFeedItem, ...],
    ) -> None:
        preferences = self.preferences.load()
        normalized_kind = MonitorKind(kind)
        if (
            preferences.get(BACKGROUND_CHECKS_ENABLED) is not True
            or _interval_from_preferences(preferences, normalized_kind) <= 0
            or not items
        ):
            return
        translator = Translator(str(preferences.get("language", "system")))
        application = self._registered_application()
        publisher = (
            self._publisher_factory(application)
            if self._publisher_factory is not None
            else GNotificationPublisher(application)
        )
        publisher.publish_feed_updates(
            normalized_kind,
            items,
            translator,
        )

    def _registered_application(self) -> Any:
        if self._application is None:
            application = self._application_factory()
            if not application.register(None):
                raise BackgroundMonitorError("Could not register the notification app")
            self._application = application
        return self._application


class BackgroundCheckRunner:
    """Run one due subscription family under an inter-process lease."""

    def __init__(
        self,
        preferences: PreferencesProtocol,
        monitor: MonitorProtocol,
        cadence: CadenceProtocol,
        run_lock: RunLockProtocol,
        *,
        clock: Callable[[], float] = time.time,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self.preferences = preferences
        self.monitor = monitor
        self.cadence = cadence
        self.run_lock = run_lock
        self.clock = clock
        self._close_callback = close_callback
        self._closed = False

    def run(self, kind: MonitorKind) -> BackgroundRunResult:
        normalized_kind = MonitorKind(kind)
        eligibility = self._eligibility(normalized_kind)
        if eligibility is not None:
            return eligibility
        if not self.run_lock.try_acquire(normalized_kind):
            return BackgroundRunResult.BUSY
        try:
            # Re-read after acquiring the process lock so disabling or changing
            # an interval can win a race without any network access.
            eligibility = self._eligibility(normalized_kind)
            if eligibility is not None:
                return eligibility
            preferences = self.preferences.load()
            interval = _interval_from_preferences(preferences, normalized_kind)
            now = int(self.clock())
            if not self.cadence.is_due(normalized_kind, interval, now):
                return BackgroundRunResult.NOT_DUE
            self.monitor.resume()
            self.monitor.run_once(normalized_kind)
            if self.monitor.cancelled:
                return BackgroundRunResult.CANCELLED
            self.cadence.mark_completed(normalized_kind, int(self.clock()))
            return BackgroundRunResult.COMPLETED
        finally:
            self.run_lock.release(normalized_kind)

    def cancel(self) -> None:
        self.monitor.cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.monitor.cancel()
        if self._close_callback is not None:
            self._close_callback()
        close_lock = getattr(self.run_lock, "close", None)
        if close_lock is not None:
            close_lock()

    def _eligibility(self, kind: MonitorKind) -> BackgroundRunResult | None:
        preferences = self.preferences.load()
        if preferences.get(BACKGROUND_CHECKS_ENABLED) is not True:
            return BackgroundRunResult.DISABLED
        if _interval_from_preferences(preferences, kind) <= 0:
            return BackgroundRunResult.MANUAL
        return None


def create_background_check_runner() -> BackgroundCheckRunner:
    paths = XdgPaths.from_environment()
    preferences = JsonPreferences(paths=paths)
    client = FeedClient()
    notifier = BackgroundNotificationSink(preferences)
    monitor = FeedMonitor(
        client,
        FeedStore(paths=paths, preferences=preferences),
        PodcastStore(paths=paths),
        checkpoints=CheckpointStore(JsonCheckpointBackend()),
        notification_callback=notifier,
        error_callback=_log_monitor_error,
    )
    return BackgroundCheckRunner(
        preferences,
        monitor,
        BackgroundCadenceStore(),
        FileMonitorRunLock(),
        close_callback=client.close,
    )


def background_main(kind_value: str) -> int:
    try:
        kind = MonitorKind(kind_value)
    except ValueError:
        print("Background kind must be 'rss' or 'podcast'.", file=sys.stderr)
        return 2
    runner = create_background_check_runner()
    previous_handlers: dict[int, Any] = {}

    def cancel(_signal_number: int, _frame: FrameType | None) -> None:
        runner.cancel()

    try:
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, cancel)
        runner.run(kind)
    except Exception as error:
        print(f"ARSS background monitor failed: {error}", file=sys.stderr)
        return 1
    finally:
        runner.close()
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)
    return 0


def _interval_from_preferences(
    preferences: Mapping[str, object],
    kind: MonitorKind,
) -> int:
    key = (
        "rss_check_interval_minutes"
        if kind is MonitorKind.RSS
        else "podcast_check_interval_minutes"
    )
    value = preferences.get(key, 0)
    return value if type(value) is int and value in SUPPORTED_BACKGROUND_INTERVALS else 0


def _timer_unit(kind: MonitorKind) -> str:
    return TIMER_UNIT_TEMPLATE.format(kind=MonitorKind(kind).value)


def _service_unit(kind: MonitorKind) -> str:
    return SERVICE_UNIT_TEMPLATE.format(kind=MonitorKind(kind).value)


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


def _log_monitor_error(
    kind: MonitorKind,
    subscription: Any | None,
    error: BaseException,
) -> None:
    source = getattr(subscription, "title", "") if subscription is not None else ""
    prefix = f"ARSS {kind.value} background monitor"
    if source:
        prefix += f" ({source})"
    print(f"{prefix}: {error}", file=sys.stderr)
