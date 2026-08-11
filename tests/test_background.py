from __future__ import annotations

from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from arss.background import (
    BACKGROUND_CHECKS_ENABLED,
    BackgroundCadenceStore,
    BackgroundCheckRunner,
    BackgroundMonitorError,
    BackgroundMonitorManager,
    BackgroundNotificationSink,
    BackgroundRunResult,
    CommandResult,
    FileMonitorRunLock,
    calendar_expressions,
    default_user_unit_config_directory,
    render_timer_drop_in,
)
from arss.models import FeedArticle, FeedSubscription
from arss.monitor import MonitorKind, NewFeedItem
from arss.notifications import GNotificationPublisher


class FakePreferences:
    def __init__(self, **values: object) -> None:
        self.values: dict[str, object] = {
            BACKGROUND_CHECKS_ENABLED: False,
            "language": "en",
            "rss_check_interval_minutes": 5,
            "podcast_check_interval_minutes": 10,
            **values,
        }

    def load(self) -> dict[str, object]:
        return dict(self.values)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


class FakeMonitor:
    def __init__(self, *, cancel_during_run: bool = False) -> None:
        self.runs: list[MonitorKind] = []
        self.resume_calls = 0
        self.cancel_calls = 0
        self.cancelled = False
        self.cancel_during_run = cancel_during_run

    def run_once(self, kind: MonitorKind) -> tuple[NewFeedItem, ...]:
        self.runs.append(kind)
        if self.cancel_during_run:
            self.cancelled = True
        return ()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled = True

    def resume(self) -> None:
        self.resume_calls += 1
        self.cancelled = False


class FakeCadence:
    def __init__(self, *, due: bool = True) -> None:
        self.due = due
        self.checks: list[tuple[MonitorKind, int, int]] = []
        self.completed: list[tuple[MonitorKind, int]] = []

    def is_due(self, kind: MonitorKind, interval: int, now: int) -> bool:
        self.checks.append((kind, interval, now))
        return self.due

    def mark_completed(self, kind: MonitorKind, completed_at: int) -> None:
        self.completed.append((kind, completed_at))


class FakeRunLock:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.acquired: list[MonitorKind] = []
        self.released: list[MonitorKind] = []

    def try_acquire(self, kind: MonitorKind) -> bool:
        self.acquired.append(kind)
        return self.available

    def release(self, kind: MonitorKind) -> None:
        self.released.append(kind)


class CalendarScheduleTest(unittest.TestCase):
    def test_supported_intervals_have_exact_calendar_expressions(self) -> None:
        self.assertEqual(("*-*-* *:0/5:00",), calendar_expressions(5))
        self.assertEqual(("*-*-* *:00:00",), calendar_expressions(60))
        self.assertEqual(("*-*-* 0/3:00:00",), calendar_expressions(180))
        self.assertEqual(("*-*-* 0/12:00:00",), calendar_expressions(720))

        forty_five = calendar_expressions(45)
        self.assertEqual(32, len(forty_five))
        self.assertEqual("*-*-* 00:00:00", forty_five[0])
        self.assertEqual("*-*-* 00:45:00", forty_five[1])
        self.assertEqual("*-*-* 23:15:00", forty_five[-1])
        with self.assertRaises(ValueError):
            calendar_expressions(2)

    def test_drop_in_resets_vendor_schedule_and_is_persistent(self) -> None:
        text = render_timer_drop_in(15).decode("utf-8")
        self.assertIn("[Timer]\nOnCalendar=\n", text)
        self.assertIn("OnCalendar=*-*-* *:0/15:00", text)
        self.assertTrue(text.endswith("Persistent=true\n"))

    def test_user_unit_path_obeys_only_absolute_xdg_config_home(self) -> None:
        self.assertEqual(
            Path("/var/config/systemd/user"),
            default_user_unit_config_directory(
                {"XDG_CONFIG_HOME": "/var/config"},
                home=Path("/home/tester"),
            ),
        )
        self.assertEqual(
            Path("/home/tester/.config/systemd/user"),
            default_user_unit_config_directory(
                {"XDG_CONFIG_HOME": "relative"},
                home=Path("/home/tester"),
            ),
        )


class BackgroundMonitorManagerTest(unittest.TestCase):
    def test_explicit_enable_writes_schedules_and_controls_only_due_timers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preferences = FakePreferences(podcast_check_interval_minutes=0)
            commands: list[tuple[str, ...]] = []

            def command(command: object) -> CommandResult:
                commands.append(tuple(command))  # type: ignore[arg-type]
                return CommandResult(0)

            manager = BackgroundMonitorManager(
                preferences,
                unit_config_directory=Path(temporary),
                command_runner=command,
            )

            self.assertFalse(manager.enabled)
            manager.set_enabled(True)

            self.assertTrue(manager.enabled)
            rss_drop_in = (
                Path(temporary)
                / "arss-monitor@rss.timer.d"
                / "schedule.conf"
            )
            self.assertTrue(rss_drop_in.is_file())
            self.assertEqual(0o600, stat.S_IMODE(rss_drop_in.stat().st_mode))
            self.assertFalse(
                (Path(temporary) / "arss-monitor@podcast.timer.d/schedule.conf").exists()
            )
            self.assertIn(
                ("systemctl", "--user", "enable", "arss-monitor@rss.timer"),
                commands,
            )
            self.assertIn(
                (
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    "arss-monitor@podcast.timer",
                ),
                commands,
            )

            manager.set_enabled(False)
            self.assertFalse(manager.enabled)
            self.assertIn(
                (
                    "systemctl",
                    "--user",
                    "stop",
                    "--no-block",
                    "arss-monitor@rss.service",
                    "arss-monitor@podcast.service",
                ),
                commands,
            )

    def test_failed_enable_rolls_back_opt_in_and_disables_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preferences = FakePreferences()
            commands: list[tuple[str, ...]] = []

            def command(command: object) -> CommandResult:
                normalized = tuple(command)  # type: ignore[arg-type]
                commands.append(normalized)
                if "restart" in normalized:
                    return CommandResult(1, "user manager unavailable")
                return CommandResult(0)

            manager = BackgroundMonitorManager(
                preferences,
                unit_config_directory=Path(temporary),
                command_runner=command,
            )

            with self.assertRaises(BackgroundMonitorError):
                manager.set_enabled(True)

            self.assertFalse(manager.enabled)
            self.assertTrue(
                any("disable" in command and "arss-monitor@rss.timer" in command for command in commands)
            )

    def test_disable_still_stops_services_when_daemon_reload_fails(self) -> None:
        preferences = FakePreferences(background_checks_enabled=True)
        commands: list[tuple[str, ...]] = []

        def command(command: object) -> CommandResult:
            normalized = tuple(command)  # type: ignore[arg-type]
            commands.append(normalized)
            return CommandResult(1 if "daemon-reload" in normalized else 0, "failed")

        manager = BackgroundMonitorManager(preferences, command_runner=command)
        with self.assertRaises(BackgroundMonitorError):
            manager.set_enabled(False)

        self.assertFalse(manager.enabled)
        self.assertTrue(any("stop" in command for command in commands))


class BackgroundCadenceAndLockTest(unittest.TestCase):
    def test_cadence_is_separate_per_kind_and_handles_backward_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = BackgroundCadenceStore(Path(temporary))
            self.assertTrue(store.is_due(MonitorKind.RSS, 5, 1000))
            store.mark_completed(MonitorKind.RSS, 1000)
            self.assertFalse(store.is_due(MonitorKind.RSS, 5, 1299))
            self.assertTrue(store.is_due(MonitorKind.RSS, 5, 1300))
            self.assertTrue(store.is_due(MonitorKind.PODCAST, 5, 1001))
            self.assertTrue(store.is_due(MonitorKind.RSS, 5, 999))

    def test_process_lock_excludes_same_kind_but_not_other_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = FileMonitorRunLock(Path(temporary))
            second = FileMonitorRunLock(Path(temporary))
            self.assertTrue(first.try_acquire(MonitorKind.RSS))
            self.assertFalse(second.try_acquire(MonitorKind.RSS))
            self.assertTrue(second.try_acquire(MonitorKind.PODCAST))
            first.release(MonitorKind.RSS)
            self.assertTrue(second.try_acquire(MonitorKind.RSS))
            first.close()
            second.close()


class BackgroundCheckRunnerTest(unittest.TestCase):
    def test_disabled_and_manual_modes_never_acquire_or_run(self) -> None:
        preferences = FakePreferences()
        monitor = FakeMonitor()
        cadence = FakeCadence()
        run_lock = FakeRunLock()
        runner = BackgroundCheckRunner(
            preferences,
            monitor,
            cadence,
            run_lock,
            clock=lambda: 1000,
        )

        self.assertEqual(BackgroundRunResult.DISABLED, runner.run(MonitorKind.RSS))
        preferences.values[BACKGROUND_CHECKS_ENABLED] = True
        preferences.values["rss_check_interval_minutes"] = 0
        self.assertEqual(BackgroundRunResult.MANUAL, runner.run(MonitorKind.RSS))
        self.assertEqual([], run_lock.acquired)
        self.assertEqual([], monitor.runs)

    def test_due_run_is_kind_specific_and_commits_only_after_completion(self) -> None:
        preferences = FakePreferences(background_checks_enabled=True)
        monitor = FakeMonitor()
        cadence = FakeCadence()
        run_lock = FakeRunLock()
        ticks = iter((1000, 1002))
        runner = BackgroundCheckRunner(
            preferences,
            monitor,
            cadence,
            run_lock,
            clock=lambda: next(ticks),
        )

        result = runner.run(MonitorKind.PODCAST)

        self.assertEqual(BackgroundRunResult.COMPLETED, result)
        self.assertEqual([MonitorKind.PODCAST], monitor.runs)
        self.assertEqual([(MonitorKind.PODCAST, 10, 1000)], cadence.checks)
        self.assertEqual([(MonitorKind.PODCAST, 1002)], cadence.completed)
        self.assertEqual([MonitorKind.PODCAST], run_lock.released)

    def test_busy_not_due_and_cancelled_runs_do_not_commit(self) -> None:
        preferences = FakePreferences(background_checks_enabled=True)
        busy = BackgroundCheckRunner(
            preferences,
            FakeMonitor(),
            FakeCadence(),
            FakeRunLock(available=False),
        )
        self.assertEqual(BackgroundRunResult.BUSY, busy.run(MonitorKind.RSS))

        cadence = FakeCadence(due=False)
        not_due = BackgroundCheckRunner(
            preferences,
            FakeMonitor(),
            cadence,
            FakeRunLock(),
            clock=lambda: 1000,
        )
        self.assertEqual(BackgroundRunResult.NOT_DUE, not_due.run(MonitorKind.RSS))
        self.assertEqual([], cadence.completed)

        cancelled_cadence = FakeCadence()
        cancelled = BackgroundCheckRunner(
            preferences,
            FakeMonitor(cancel_during_run=True),
            cancelled_cadence,
            FakeRunLock(),
            clock=lambda: 1000,
        )
        self.assertEqual(BackgroundRunResult.CANCELLED, cancelled.run(MonitorKind.RSS))
        self.assertEqual([], cancelled_cadence.completed)


class BackgroundNotificationTest(unittest.TestCase):
    def test_notification_keeps_dbus_activatable_article_action(self) -> None:
        preferences = FakePreferences(background_checks_enabled=True)
        actions: list[tuple[str, str, object]] = []
        sent: list[tuple[str, object]] = []

        class NotificationSpy:
            def set_body(self, _body: str) -> None:
                return None

            def set_priority(self, _priority: object) -> None:
                return None

            def set_default_action_and_target(self, action: str, target: object) -> None:
                actions.append((action, target.get_type_string(), target.unpack()))

        application = SimpleNamespace(
            register=lambda _cancellable: True,
            send_notification=lambda identifier, notification: sent.append(
                (identifier, notification)
            ),
        )
        fake_gio = SimpleNamespace(
            Notification=SimpleNamespace(new=lambda _title: NotificationSpy()),
            NotificationPriority=SimpleNamespace(LOW="low", NORMAL="normal"),
        )
        sink = BackgroundNotificationSink(
            preferences,
            application_factory=lambda: application,
            publisher_factory=lambda registered: GNotificationPublisher(
                registered,
                gio=fake_gio,
                glib=GLib,
            ),
        )
        item = NewFeedItem(
            MonitorKind.RSS,
            FeedSubscription("News", "https://example.test/feed"),
            FeedArticle("Article", "https://example.test/article"),
            "id:article",
        )

        sink(MonitorKind.RSS, (item,))

        self.assertEqual(
            [("app.open-article", "s", "https://example.test/article")],
            actions,
        )
        self.assertTrue(sent[0][0].startswith("arss-rss-item-"))

    def test_disabled_notification_does_not_register_an_application(self) -> None:
        preferences = FakePreferences(background_checks_enabled=False)
        factory = unittest.mock.Mock()
        sink = BackgroundNotificationSink(preferences, application_factory=factory)

        sink(MonitorKind.RSS, ())

        factory.assert_not_called()


class HeadlessEntrypointTest(unittest.TestCase):
    def test_background_argument_routes_without_graphical_startup(self) -> None:
        from arss import __main__

        with patch("arss.background.background_main", return_value=7) as background:
            self.assertEqual(7, __main__.main(["--background-check", "rss"]))
        background.assert_called_once_with("rss")
        self.assertEqual(2, __main__.main(["--background-check"]))


if __name__ == "__main__":
    unittest.main()
