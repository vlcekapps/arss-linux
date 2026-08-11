from __future__ import annotations

from datetime import date, datetime, timezone
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from arss.gtk_helpers import AccessibleMenuPopover, Gdk, Gtk
from arss.i18n import Translator
from arss.models import FeedArticle, FeedSubscription
from arss.ui import (
    MAIN_PAGE_NAMES,
    THANK_AUTHOR_URL,
    MainWindow,
    SettingsPage,
    SubscriptionPage,
    application_version,
    current_guide_date,
    feed_item_title,
    directory_search_locale,
    format_duration,
    format_guide_date,
    has_unknown_audio_description,
    is_currently_airing,
    next_program_boundary_delay_ms,
    opml_import_message,
    playback_control_sensitivity,
    preferred_program_index,
    program_title,
    record_manual_checkpoint,
    subscription_matches_filter,
    subscription_successor_after_removal,
    valid_web_url,
)


class UiHelperTest(unittest.TestCase):
    def test_duration_uses_compact_hours_only_when_needed(self) -> None:
        self.assertEqual("0:00", format_duration(None))
        self.assertEqual("1:05", format_duration(65))
        self.assertEqual("1:01:01", format_duration(3661))

    def test_prague_default_date_uses_the_shared_guide_domain_rule(self) -> None:
        instant = int(
            datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(date(2026, 1, 2), current_guide_date(instant))

    def test_blank_item_and_programme_titles_have_localized_fallbacks(self) -> None:
        blank = FeedArticle("  ", "https://example.test/item")
        self.assertEqual(
            "The article title is unavailable.",
            feed_item_title(blank, "rss", Translator("en")),
        )
        self.assertEqual(
            "Nelze najít název epizody.",
            feed_item_title(blank, "podcast", Translator("cs")),
        )
        self.assertEqual(
            "Untitled programme",
            program_title(SimpleNamespace(title=""), Translator("en")),
        )

    def test_version_fallback_uses_the_installed_package_version(self) -> None:
        self.assertEqual("1.6.12", application_version(SimpleNamespace()))
        self.assertEqual(
            "custom",
            application_version(SimpleNamespace(version="custom")),
        )

    def test_unknown_duration_disables_timeline_seek_controls(self) -> None:
        self.assertEqual(
            (True, False),
            playback_control_sensitivity(
                SimpleNamespace(phase="ready", duration_ms=0)
            ),
        )
        self.assertEqual(
            (True, True),
            playback_control_sensitivity(
                SimpleNamespace(phase="playing", duration_ms=60_000)
            ),
        )
        self.assertEqual(
            (False, False),
            playback_control_sensitivity(
                SimpleNamespace(phase="preparing", duration_ms=60_000)
            ),
        )

    def test_filtered_removal_chooses_only_a_still_visible_successor(self) -> None:
        first = FeedSubscription("Visible first", "https://example.test/first")
        hidden = FeedSubscription("Hidden", "https://example.test/hidden")
        last = FeedSubscription("Visible last", "https://example.test/last")
        page = SimpleNamespace(
            items=[first, hidden, last],
            filter_entry=SimpleNamespace(get_text=lambda: "visible"),
        )
        visible_before = SubscriptionPage._visible_items(page)
        page.items = [hidden, last]
        visible_after = SubscriptionPage._visible_items(page)

        self.assertEqual([first, last], visible_before)
        self.assertEqual([last], visible_after)
        self.assertIs(
            last,
            subscription_successor_after_removal(
                first,
                visible_before,
                visible_after,
            ),
        )

        page.items = [hidden]
        self.assertIsNone(
            subscription_successor_after_removal(
                last,
                visible_after,
                SubscriptionPage._visible_items(page),
            )
        )

    def test_directory_disclosure_and_help_describe_desktop_behaviour(self) -> None:
        translator = Translator("en")
        self.assertIn("sent to both services", translator("podcast_directory_note"))
        help_text = translator("help_text")
        self.assertIn("systemd user timers", help_text)
        self.assertIn("GNOME notifications", help_text)
        self.assertIn("exclusively controls the system notification sound", help_text)
        self.assertIn("ordered by broadcaster", help_text)
        self.assertIn("Shift+Tab", help_text)

    def test_only_absolute_http_addresses_are_external_web_links(self) -> None:
        self.assertTrue(valid_web_url("https://example.test/path"))
        self.assertTrue(valid_web_url("http://example.test"))
        self.assertFalse(valid_web_url("file:///etc/passwd"))
        self.assertFalse(valid_web_url("javascript:alert(1)"))
        self.assertFalse(valid_web_url("relative/path"))

    def test_subscription_filter_matches_all_normalized_title_terms(self) -> None:
        subscription = FeedSubscription(
            "Český—rozhlas: Vinohradská 12",
            "https://example.test/private-url-token",
        )
        self.assertTrue(subscription_matches_filter("rozhlas cesky", subscription))
        self.assertTrue(subscription_matches_filter("VINOH-RADSKA! 12", subscription))
        self.assertFalse(subscription_matches_filter("rozhlas sport", subscription))
        self.assertFalse(subscription_matches_filter("private url", subscription))

    def test_podcast_directory_locale_follows_application_language(self) -> None:
        self.assertEqual("cs-CZ", directory_search_locale("system", "cs_CZ.UTF-8"))
        self.assertEqual("cs-CZ", directory_search_locale("cs", "en_US"))
        self.assertEqual("en-CZ", directory_search_locale("en", "cs_CZ"))

    def test_manual_checkpoint_uses_shared_service_port_and_playable_episodes(self) -> None:
        playable = FeedArticle(
            "Playable",
            "https://example.test/one",
            media_url="https://example.test/one.mp3",
        )
        text_only = FeedArticle("Text", "https://example.test/two")
        services = SimpleNamespace(record_feed_seen=Mock())

        record_manual_checkpoint(
            services,
            "podcast",
            "https://example.test/feed",
            (playable, text_only),
        )

        services.record_feed_seen.assert_called_once_with(
            "podcast",
            "https://example.test/feed",
            (playable,),
        )
        failing = SimpleNamespace(
            record_feed_seen=Mock(side_effect=OSError("checkpoint unavailable"))
        )
        record_manual_checkpoint(failing, "rss", "feed", (text_only,))

    def test_opml_message_reports_rejected_podcasts(self) -> None:
        translator = Translator("en")
        self.assertIn("No playable podcasts", opml_import_message(translator, 0, 2))
        message = opml_import_message(translator, 3, 2)
        self.assertIn("Items imported: 3", message)
        self.assertIn("skipped: 2", message)

    def test_program_helpers_select_now_and_refresh_at_next_boundary(self) -> None:
        entries = (
            SimpleNamespace(start_millis=1_000, end_millis=2_000),
            SimpleNamespace(start_millis=2_000, end_millis=3_000),
        )
        self.assertTrue(is_currently_airing(entries[0], 1_500))
        self.assertFalse(is_currently_airing(entries[0], 2_000))
        self.assertEqual(0, preferred_program_index(entries, 1_500))
        self.assertEqual(1, preferred_program_index(entries, 2_000))
        self.assertEqual(750, next_program_boundary_delay_ms(entries, 1_500))
        self.assertIsNone(next_program_boundary_delay_ms(entries, 3_000))

    def test_program_date_and_audio_unknown_match_android_rules(self) -> None:
        self.assertEqual(
            "2. srpna 2026",
            format_guide_date(date(2026, 8, 2), Translator("cs")),
        )
        self.assertEqual(
            "August 2, 2026",
            format_guide_date(date(2026, 8, 2), Translator("en")),
        )
        unknown = SimpleNamespace(
            audio_description=False,
            audio_description_known=False,
        )
        self.assertTrue(
            has_unknown_audio_description(SimpleNamespace(id="centrum:1"), unknown)
        )
        self.assertFalse(
            has_unknown_audio_description(SimpleNamespace(id="rozhlas:test"), unknown)
        )

    def test_stable_main_pages_and_specific_author_url(self) -> None:
        self.assertEqual(("rss", "podcast", "guide", "settings"), MAIN_PAGE_NAMES)
        self.assertEqual(
            "https://obchod.pvlcek.cz/produkt/kupte-autorovi-kavu-podpora-tvorby-podcastu-a-karosy/",
            THANK_AUTHOR_URL,
        )

    def test_main_pages_install_alt_shortcuts_and_close_shortcut(self) -> None:
        application = Mock()
        window = SimpleNamespace(
            _activate_page=Mock(),
            add_action=Mock(),
            close=Mock(),
        )

        MainWindow._install_actions(window, application)

        self.assertEqual(
            [
                call("win.page(0)", ["<Alt>1"]),
                call("win.page(1)", ["<Alt>2"]),
                call("win.page(2)", ["<Alt>3"]),
                call("win.page(3)", ["<Alt>4"]),
                call("win.close", ["<Ctrl>q"]),
            ],
            application.set_accels_for_action.call_args_list,
        )
        self.assertEqual(2, window.add_action.call_count)

    def test_page_shortcut_selects_and_focuses_its_named_tab(self) -> None:
        tabs = tuple(Mock(name=f"tab-{index}") for index in range(4))
        window = SimpleNamespace(
            select_page=Mock(),
            page_tabs=lambda: tabs,
        )
        parameter = SimpleNamespace(get_int32=lambda: 2)

        with patch("arss.ui.focus_later") as move_focus:
            MainWindow._activate_page(window, Mock(), parameter)

        window.select_page.assert_called_once_with(2)
        move_focus.assert_called_once_with(tabs[2])

    def test_only_filter_layout_change_reflows_subscription_pages(self) -> None:
        application = SimpleNamespace(preferences_changed=Mock())
        rss_page = SimpleNamespace(reflow=Mock())
        podcast_page = SimpleNamespace(reflow=Mock())
        window = SimpleNamespace(
            rss_page=rss_page,
            podcast_page=podcast_page,
            get_application=lambda: application,
        )

        MainWindow.preferences_changed(window, "show_article_dates")
        rss_page.reflow.assert_not_called()
        podcast_page.reflow.assert_not_called()
        application.preferences_changed.assert_called_once_with(
            "show_article_dates"
        )

        MainWindow.preferences_changed(window, "filter_after_list")
        rss_page.reflow.assert_called_once_with()
        podcast_page.reflow.assert_called_once_with()
        self.assertEqual(
            call("filter_after_list"),
            application.preferences_changed.call_args_list[-1],
        )

    def test_open_player_replaces_the_previous_window(self) -> None:
        episode = FeedArticle(
            "Episode",
            "https://example.test/episode",
            media_url="https://example.test/audio.mp3",
        )
        previous = Mock()
        created = Mock()
        window = SimpleNamespace(_player_window=previous)

        with patch("arss.ui.PlayerWindow", return_value=created) as player_window:
            result = MainWindow.open_player(window, episode, "Example feed")

        previous.close.assert_called_once_with()
        player_window.assert_called_once_with(
            window,
            episode,
            feed_title="Example feed",
        )
        created.present.assert_called_once_with()
        self.assertIs(created, window._player_window)
        self.assertIs(created, result)

    def test_async_work_is_not_submitted_after_shutdown_begins(self) -> None:
        shutdown_event = threading.Event()
        shutdown_event.set()
        application = type("Application", (), {"shutdown_event": shutdown_event})()
        window = type(
            "Window",
            (),
            {"get_application": lambda self: application},
        )()
        called: list[str] = []

        future = MainWindow.run_async(
            window,
            lambda: called.append("work"),
            lambda _result: called.append("success"),
            lambda _error: called.append("failure"),
        )

        self.assertTrue(future.cancelled())
        self.assertEqual([], called)

    def test_background_switch_calls_application_port_asynchronously(self) -> None:
        values: dict[str, object] = {"background_checks_enabled": False}
        callback = Mock(
            side_effect=lambda enabled: values.__setitem__(
                "background_checks_enabled",
                enabled,
            )
        )
        application = SimpleNamespace(set_background_checks_enabled=callback)

        def run_async(work, success, failure):
            try:
                success(work())
            except BaseException as error:
                failure(error)

        window = SimpleNamespace(
            t=Translator("en"),
            get_application=lambda: application,
            state=SimpleNamespace(get=lambda key, default=None: values.get(key, default)),
            run_async=run_async,
        )
        switch = Mock()
        switch.get_state.return_value = False
        page = SimpleNamespace(
            _background_updating=False,
            window=window,
            background_status=Mock(),
            _set_background_switch=Mock(),
            _update_background_description=Mock(),
        )
        with patch("arss.ui.focus_later"):
            handled = SettingsPage._background_state_set(page, switch, True)

        self.assertTrue(handled)
        callback.assert_called_once_with(True)
        page._set_background_switch.assert_called_once_with(True)
        page._update_background_description.assert_called_once_with(True)
        self.assertEqual(
            [False, True],
            [call.args[0] for call in switch.set_sensitive.call_args_list],
        )
        self.assertEqual(
            "Background checks are enabled.",
            page.background_status.set_status.call_args_list[-1].args[0],
        )

    def test_background_switch_rolls_back_and_announces_failure_high(self) -> None:
        values: dict[str, object] = {"background_checks_enabled": True}

        def fail_after_persisting_disabled(_enabled: bool) -> None:
            values["background_checks_enabled"] = False
            raise OSError("systemd failed")

        application = SimpleNamespace(
            set_background_checks_enabled=Mock(
                side_effect=fail_after_persisting_disabled
            )
        )

        def run_async(work, success, failure):
            try:
                success(work())
            except BaseException as error:
                failure(error)

        window = SimpleNamespace(
            t=Translator("en"),
            get_application=lambda: application,
            state=SimpleNamespace(
                get=lambda key, default=None: values.get(key, default)
            ),
            run_async=run_async,
        )
        switch = Mock()
        switch.get_state.return_value = True
        page = SimpleNamespace(
            _background_updating=False,
            window=window,
            background_status=Mock(),
            _set_background_switch=Mock(),
            _update_background_description=Mock(),
        )
        with patch("arss.ui.focus_later"):
            SettingsPage._background_state_set(page, switch, False)

        page._set_background_switch.assert_called_once_with(False)
        page._update_background_description.assert_called_once_with(False)
        message = page.background_status.set_status.call_args_list[-1].args[0]
        self.assertIn("systemd failed", message)
        page.background_status.announce.assert_called_once()
        self.assertEqual(
            Gtk.AccessibleAnnouncementPriority.HIGH,
            page.background_status.announce.call_args.args[1],
        )

    def test_background_refresh_reflects_persisted_state_and_announces_error(self) -> None:
        values: dict[str, object] = {"background_checks_enabled": False}
        page = SimpleNamespace(
            window=SimpleNamespace(
                t=Translator("en"),
                state=SimpleNamespace(
                    get=lambda key, default=None: values.get(key, default)
                ),
            ),
            _set_background_switch=Mock(),
            _update_background_description=Mock(),
            background_status=Mock(),
        )

        SettingsPage.refresh_background_checks_state(
            page,
            OSError("timer sync failed"),
        )

        page._set_background_switch.assert_called_once_with(False)
        page._update_background_description.assert_called_once_with(False)
        message = page.background_status.set_status.call_args.args[0]
        self.assertIn("timer sync failed", message)
        self.assertFalse(
            page.background_status.set_status.call_args.kwargs["announce"]
        )
        page.background_status.announce.assert_called_once_with(
            message,
            Gtk.AccessibleAnnouncementPriority.HIGH,
        )

    def test_accessible_menu_keyboard_navigation_and_escape(self) -> None:
        items = [Mock(name=f"item-{index}") for index in range(3)]
        root = SimpleNamespace(get_focus=lambda: items[1])
        opener = Mock()
        menu = SimpleNamespace(
            items=items,
            get_root=lambda: root,
            popdown=Mock(),
            opener=opener,
        )
        cases = (
            (Gdk.KEY_Down, 2),
            (Gdk.KEY_KP_Down, 2),
            (Gdk.KEY_Up, 0),
            (Gdk.KEY_KP_Up, 0),
            (Gdk.KEY_Home, 0),
            (Gdk.KEY_KP_Home, 0),
            (Gdk.KEY_End, 2),
            (Gdk.KEY_KP_End, 2),
        )
        for keyval, target in cases:
            self.assertTrue(
                AccessibleMenuPopover._key_pressed(menu, Mock(), keyval, 0, Gdk.ModifierType(0))
            )
            items[target].grab_focus.assert_called_once_with()
            items[target].grab_focus.reset_mock()
        self.assertTrue(
            AccessibleMenuPopover._key_pressed(
                menu,
                Mock(),
                Gdk.KEY_Escape,
                0,
                Gdk.ModifierType(0),
            )
        )
        menu.popdown.assert_called_once_with()
        opener.grab_focus.assert_called_once_with()
        self.assertFalse(
            AccessibleMenuPopover._key_pressed(menu, Mock(), Gdk.KEY_F1, 0, Gdk.ModifierType(0))
        )

    def test_accessible_menu_opens_with_enter_keypad_enter_and_space(self) -> None:
        menu = SimpleNamespace(popup=Mock())
        for keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.assertTrue(
                AccessibleMenuPopover._opener_key_pressed(
                    menu,
                    Mock(),
                    keyval,
                    0,
                    Gdk.ModifierType(0),
                )
            )
        self.assertEqual(3, menu.popup.call_count)
        self.assertFalse(
            AccessibleMenuPopover._opener_key_pressed(
                menu,
                Mock(),
                Gdk.KEY_F1,
                0,
                Gdk.ModifierType(0),
            )
        )


if __name__ == "__main__":
    unittest.main()
