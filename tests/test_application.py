from __future__ import annotations

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

import arss.application as application_module
from arss.application import ArssApplication, DesktopServices, DesktopState, data_file
from arss.models import FeedArticle, FeedSubscription
from arss.monitor import MonitorKind, NewFeedItem, article_identifiers
from arss.storage import DEFAULT_PREFERENCES, PreferencesError, XdgPaths


class DesktopStateTest(unittest.TestCase):
    def test_corrupt_preferences_are_backed_up_before_defaults_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DesktopState(XdgPaths(root / "data", root / "config"))
            state.preferences.path.parent.mkdir(parents=True)
            state.preferences.path.write_text("broken", encoding="utf-8")
            with self.assertRaises(PreferencesError):
                state.preferences.load()
            backup = state.back_up_and_reset_preferences()
            self.assertEqual("broken", backup.read_text(encoding="utf-8"))
            self.assertEqual(DEFAULT_PREFERENCES, state.preferences.load())

    def test_packaged_runtime_assets_are_resolvable(self) -> None:
        self.assertTrue(data_file("rss_directory.opml").is_file())
        self.assertTrue(data_file("style.css").is_file())
        self.assertFalse(data_file("sounds").exists())


class NotificationTest(unittest.TestCase):
    def _harness(self):
        sent: list[tuple[str, object]] = []
        state = SimpleNamespace(
            get=lambda key, default=None: {"language": "en"}.get(key, default)
        )
        return SimpleNamespace(
            shutdown_event=threading.Event(),
            window=None,
            state=state,
            services=SimpleNamespace(),
            send_notification=lambda notification_id, notification: sent.append(
                (notification_id, notification)
            ),
        ), sent

    @staticmethod
    def _item(kind: MonitorKind, *, media: bool = False, stable_id: str = "one") -> NewFeedItem:
        return NewFeedItem(
            kind,
            FeedSubscription("Source", "https://example.test/feed.xml"),
            FeedArticle(
                "Item",
                "https://example.test/item",
                media_url="https://example.test/audio.mp3" if media else None,
            ),
            stable_id,
        )

    def test_each_notification_action_branch_uses_the_supported_gio_api(self) -> None:
        harness, sent = self._harness()
        actions: list[tuple[str, str, object]] = []

        class NotificationSpy:
            @classmethod
            def new(cls, _title: str):
                return cls()

            def set_body(self, _body: str) -> None:
                return None

            def set_priority(self, _priority: object) -> None:
                return None

            def set_default_action_and_target(
                self,
                action: str,
                target: GLib.Variant,
            ) -> None:
                actions.append((action, target.get_type_string(), target.unpack()))

        cases = (
            (MonitorKind.RSS, (self._item(MonitorKind.RSS),)),
            (MonitorKind.PODCAST, (self._item(MonitorKind.PODCAST, media=True),)),
            (
                MonitorKind.RSS,
                (
                    self._item(MonitorKind.RSS, stable_id="one"),
                    self._item(MonitorKind.RSS, stable_id="two"),
                ),
            ),
        )
        with patch.object(application_module.Gio, "Notification", NotificationSpy):
            for kind, items in cases:
                result = ArssApplication._deliver_notification(harness, kind, items)
                self.assertEqual(GLib.SOURCE_REMOVE, result)
        self.assertEqual(5, len(sent))
        self.assertEqual(
            [
                ("app.open-article", "s", "https://example.test/item"),
                (
                    "app.play-episode",
                    "(ssss)",
                    (
                        "Item",
                        "https://example.test/item",
                        "https://example.test/audio.mp3",
                        "",
                    ),
                ),
                ("app.open-article", "s", "https://example.test/item"),
                ("app.open-article", "s", "https://example.test/item"),
                ("app.show-kind", "s", "rss"),
            ],
            actions,
        )

    def test_active_window_receives_only_the_gnotification_set(self) -> None:
        harness, sent = self._harness()
        window = SimpleNamespace(
            t=lambda key, **_values: key,
            is_active=lambda: True,
            toast=Mock(),
        )
        harness.window = window

        result = ArssApplication._deliver_notification(
            harness,
            MonitorKind.RSS,
            (self._item(MonitorKind.RSS),),
        )

        self.assertEqual(GLib.SOURCE_REMOVE, result)
        self.assertEqual(1, len(sent))
        window.toast.assert_not_called()

    def test_manual_load_records_stable_ids_in_the_shared_checkpoint(self) -> None:
        article = FeedArticle(
            "Episode",
            "https://example.test/episode",
            source_id="episode-" + "x" * 600,
            media_url="https://example.test/audio.mp3",
        )
        checkpoints = Mock()
        harness = SimpleNamespace(checkpoints=checkpoints)

        DesktopServices.record_feed_seen(
            harness,
            "podcast",
            "https://example.test/feed.xml",
            (article,),
        )

        identity = article_identifiers(article)
        call = checkpoints.record_successful_fetch.call_args
        self.assertEqual(
            (MonitorKind.PODCAST, "https://example.test/feed.xml"),
            call.args[:2],
        )
        self.assertEqual((identity.primary,), tuple(call.args[2]))
        self.assertEqual(
            {identity.primary: identity.legacy_aliases},
            call.args[3],
        )

    def test_exported_play_action_rejects_a_local_media_target(self) -> None:
        window = SimpleNamespace(
            t=lambda key, **_values: key,
            open_player=Mock(),
        )
        harness = SimpleNamespace(activate=Mock(), window=window)
        target = GLib.Variant(
            "(ssss)",
            ("Episode", "https://example.test/item", "file:///etc/passwd", "1:00"),
        )
        with patch("arss.application.alert") as show_alert:
            ArssApplication._play_episode(harness, Mock(), target)
        show_alert.assert_called_once_with(window, "error", "invalid_address")
        window.open_player.assert_not_called()

    def test_exported_play_action_uses_the_single_player_coordinator(self) -> None:
        window = SimpleNamespace(
            t=lambda key, **_values: key,
            open_player=Mock(),
        )
        harness = SimpleNamespace(activate=Mock(), window=window)
        target = GLib.Variant(
            "(ssss)",
            (
                "Episode",
                "https://example.test/item",
                "https://example.test/audio.mp3",
                "1:00",
            ),
        )

        ArssApplication._play_episode(harness, Mock(), target)

        window.open_player.assert_called_once_with(
            FeedArticle(
                title="Episode",
                url="https://example.test/item",
                media_url="https://example.test/audio.mp3",
                duration_text="1:00",
            )
        )


class BackgroundIntegrationTest(unittest.TestCase):
    def test_systemd_opt_in_disables_the_in_process_interval(self) -> None:
        values = {
            "background_checks_enabled": True,
            "rss_check_interval_minutes": 15,
        }
        harness = SimpleNamespace(
            state=SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        )

        self.assertEqual(
            0,
            ArssApplication._monitor_interval(harness, MonitorKind.RSS),
        )
        values["background_checks_enabled"] = False
        self.assertEqual(
            15,
            ArssApplication._monitor_interval(harness, MonitorKind.RSS),
        )

    def test_non_monitor_preference_does_not_touch_scheduler_or_network(self) -> None:
        harness = SimpleNamespace(
            monitor_scheduler=SimpleNamespace(refresh=Mock(), run_now=Mock()),
            _request_background_sync=Mock(),
            _run_enabled_monitors=Mock(),
        )

        ArssApplication.preferences_changed(
            harness,
            "show_article_dates",
        )

        harness.monitor_scheduler.refresh.assert_not_called()
        harness.monitor_scheduler.run_now.assert_not_called()
        harness._request_background_sync.assert_not_called()
        harness._run_enabled_monitors.assert_not_called()

    def test_one_interval_change_runs_only_its_monitor_family(self) -> None:
        scheduler = SimpleNamespace(refresh=Mock(), run_now=Mock())
        harness = SimpleNamespace(
            state=SimpleNamespace(get=lambda _key, default=None: default),
            monitor_scheduler=scheduler,
            _monitor_interval=lambda kind: 5
            if kind is MonitorKind.RSS
            else 0,
            _request_background_sync=Mock(),
            _run_enabled_monitors=Mock(),
        )

        ArssApplication.preferences_changed(
            harness,
            "rss_check_interval_minutes",
        )

        scheduler.refresh.assert_called_once_with()
        scheduler.run_now.assert_called_once_with(MonitorKind.RSS)
        harness._run_enabled_monitors.assert_not_called()

    def test_enabled_preference_change_requests_nonblocking_systemd_sync(self) -> None:
        harness = SimpleNamespace(
            state=SimpleNamespace(
                get=lambda key, default=None: True
                if key == "background_checks_enabled"
                else default
            ),
            _request_background_sync=Mock(),
            monitor_scheduler=SimpleNamespace(refresh=Mock()),
            _run_enabled_monitors=Mock(),
        )

        ArssApplication.preferences_changed(harness)

        harness._request_background_sync.assert_called_once_with()
        harness.monitor_scheduler.refresh.assert_called_once_with()
        harness._run_enabled_monitors.assert_called_once_with()

    def test_background_sync_requests_are_coalesced_while_one_is_running(self) -> None:
        harness = SimpleNamespace(
            _background_sync_lock=threading.Lock(),
            shutdown_event=threading.Event(),
            _background_sync_in_flight=False,
            _background_sync_pending=False,
            _submit_background_sync=Mock(),
        )

        ArssApplication._request_background_sync(harness)
        ArssApplication._request_background_sync(harness)

        harness._submit_background_sync.assert_called_once_with()
        self.assertTrue(harness._background_sync_in_flight)
        self.assertTrue(harness._background_sync_pending)

    def test_background_sync_failure_is_forwarded_to_main_context(self) -> None:
        error = RuntimeError("systemd unavailable")
        future = SimpleNamespace(result=Mock(side_effect=error))
        harness = SimpleNamespace(
            _background_sync_lock=threading.Lock(),
            _background_sync_pending=False,
            _background_sync_in_flight=True,
            _background_sync_error=None,
            shutdown_event=threading.Event(),
            _submit_background_sync=Mock(),
            _finish_background_sync=Mock(),
        )

        with patch.object(application_module.GLib, "idle_add") as idle_add:
            ArssApplication._background_sync_done(harness, future)

        self.assertFalse(harness._background_sync_in_flight)
        self.assertIsNone(harness._background_sync_error)
        harness._submit_background_sync.assert_not_called()
        idle_add.assert_called_once_with(
            harness._finish_background_sync,
            error,
        )

    def test_coalesced_sync_keeps_first_error_until_final_refresh(self) -> None:
        error = RuntimeError("first sync failed")
        harness = SimpleNamespace(
            _background_sync_lock=threading.Lock(),
            _background_sync_pending=True,
            _background_sync_in_flight=True,
            _background_sync_error=None,
            shutdown_event=threading.Event(),
            _submit_background_sync=Mock(),
            _finish_background_sync=Mock(),
        )

        with patch.object(application_module.GLib, "idle_add") as idle_add:
            ArssApplication._background_sync_done(
                harness,
                SimpleNamespace(result=Mock(side_effect=error)),
            )
            harness._submit_background_sync.assert_called_once_with()
            idle_add.assert_not_called()

            ArssApplication._background_sync_done(
                harness,
                SimpleNamespace(result=Mock(return_value=None)),
            )

        idle_add.assert_called_once_with(
            harness._finish_background_sync,
            error,
        )
        self.assertFalse(harness._background_sync_in_flight)
        self.assertIsNone(harness._background_sync_error)

    def test_background_sync_completion_refreshes_ui_from_persisted_state(self) -> None:
        error = RuntimeError("systemd unavailable")
        settings = SimpleNamespace(refresh_background_checks_state=Mock())
        harness = SimpleNamespace(
            shutdown_event=threading.Event(),
            monitor_scheduler=SimpleNamespace(refresh=Mock()),
            _run_enabled_monitors=Mock(),
            window=SimpleNamespace(settings_page=settings),
        )

        result = ArssApplication._finish_background_sync(harness, error)

        self.assertEqual(GLib.SOURCE_REMOVE, result)
        harness.monitor_scheduler.refresh.assert_called_once_with()
        harness._run_enabled_monitors.assert_called_once_with()
        settings.refresh_background_checks_state.assert_called_once_with(error)

    def test_failed_opt_in_always_refreshes_foreground_scheduler(self) -> None:
        manager = Mock()
        manager.set_enabled.side_effect = RuntimeError("systemd unavailable")
        harness = SimpleNamespace(
            background_monitor_manager=manager,
            monitor_scheduler=SimpleNamespace(refresh=Mock()),
            _run_enabled_monitors=Mock(),
        )

        with self.assertRaises(RuntimeError):
            ArssApplication.set_background_checks_enabled(harness, True)

        harness.monitor_scheduler.refresh.assert_called_once_with()
        harness._run_enabled_monitors.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
