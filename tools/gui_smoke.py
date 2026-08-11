#!/usr/bin/env python3
"""Exercise every GTK window against deterministic in-memory services."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from dataclasses import replace
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import traceback

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from arss.directory import DirectoryEntry  # noqa: E402
from arss.guide import GuideMedium, GuideProgramEntry, GuideStation  # noqa: E402
from arss.gtk_helpers import (  # noqa: E402
    list_item_child,
    list_item_focus_widget,
)
from arss.models import FeedArticle, FeedSubscription, ParsedFeed  # noqa: E402
from arss.playback import PlaybackMetadata, PlaybackPhase, PlaybackState  # noqa: E402
from arss.ui import (  # noqa: E402
    AddSubscriptionWindow,
    ArticleInfoWindow,
    DirectoryWindow,
    ItemsWindow,
    MainWindow,
    PlayerWindow,
    ProgramDetailWindow,
    ProgramWindow,
    RenameWindow,
)


UNTITLED_ARTICLE = FeedArticle(
    "",
    "https://example.test/untitled",
    source_id="untitled",
    media_url="https://example.test/untitled.mp3",
    media_type="audio/mpeg",
)
ARTICLE = FeedArticle(
    "Test article",
    "https://example.test/article",
    source_id="one",
    published_text="2026-08-02",
    published_at_millis=1_775_347_200_000,
    media_url="https://example.test/audio.mp3",
    media_type="audio/mpeg",
    duration_text="12:34",
)
SUBSCRIPTION = FeedSubscription("Example feed", "https://example.test/feed.xml")
SECOND_SUBSCRIPTION = FeedSubscription(
    "Second feed",
    "https://example.test/second.xml",
)
STATION = GuideStation("centrum:1", "ČT1", GuideMedium.TELEVISION)
GUIDE_STATIONS = (
    GuideStation("centrum:4", "Prima", GuideMedium.TELEVISION),
    GuideStation("centrum:465", "Nova Sport 2", GuideMedium.TELEVISION),
    GuideStation("sms:Nova Sport 6", "Nova Sport 6", GuideMedium.TELEVISION),
    STATION,
    GuideStation("centrum:3", "Nova", GuideMedium.TELEVISION),
)
RADIO_STATION = GuideStation(
    "rozhlas:radiozurnal",
    "Radiožurnál",
    GuideMedium.RADIO,
)
UNTITLED_PROGRAMME = SimpleNamespace(
    title="",
    start_millis=1_775_347_200_000,
    end_millis=1_775_350_800_000,
    description="",
    audio_description=False,
    program_url=None,
    archive_url=None,
)
PROGRAMME = GuideProgramEntry(
    "programme",
    1_775_347_200_000,
    1_775_350_800_000,
    "Test programme",
    "Programme description",
    audio_description=True,
    audio_description_known=True,
    program_url="https://example.test/programme",
)


def is_focus_within(focus: Gtk.Widget | None, container: Gtk.Widget) -> bool:
    while focus is not None:
        if focus is container:
            return True
        focus = focus.get_parent()
    return False


def widget_children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    result: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


def widget_descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    result: list[Gtk.Widget] = []
    pending = widget_children(widget)
    while pending:
        current = pending.pop(0)
        result.append(current)
        pending[0:0] = widget_children(current)
    return result


class MemoryState:
    def __init__(self, language: str = "en") -> None:
        self.values: dict[str, object] = {
            "language": language,
            "default_feed_url": SUBSCRIPTION.url,
            "guide_television_station_id": "sms:Nova Sport 6",
        }
        self.feeds = {
            "rss": [SUBSCRIPTION, SECOND_SUBSCRIPTION],
            "podcast": [SUBSCRIPTION, SECOND_SUBSCRIPTION],
        }

    def subscriptions(self, kind: str) -> list[FeedSubscription]:
        return list(self.feeds[kind])

    def save_subscriptions(self, kind: str, items: list[FeedSubscription]) -> None:
        self.feeds[kind] = list(items)

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


class FakePlayer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.state = PlaybackState()
        self.metadata = PlaybackMetadata()
        self.state_listeners = []
        self.close_callbacks = []
        self.pause_calls = 0
        self.volume_calls: list[float] = []
        self.close_calls = 0

    def _publish(self) -> None:
        self.callback(self.state)
        for listener in tuple(self.state_listeners):
            listener(self.state)

    def open(self, _uri: str) -> bool:
        self.state = replace(
            self.state,
            phase=PlaybackPhase.READY,
            position_ms=0,
            duration_ms=60_000,
        )
        self._publish()
        return True

    prepare = open

    def toggle(self) -> bool:
        phase = PlaybackPhase.PAUSED if self.state.phase is PlaybackPhase.PLAYING else PlaybackPhase.PLAYING
        self.state = replace(self.state, phase=phase)
        self._publish()
        return True

    def pause(self) -> bool:
        if self.state.phase is not PlaybackPhase.PLAYING:
            return False
        self.state = replace(self.state, phase=PlaybackPhase.PAUSED)
        self.pause_calls += 1
        self._publish()
        return True

    def seek_by(self, _delta: int) -> bool:
        return True

    def set_volume(self, volume: float) -> bool:
        self.volume_calls.append(volume)
        self.state = replace(self.state, volume=volume)
        self._publish()
        return True

    def seek_to(self, _position: int) -> bool:
        return True

    def set_speed(self, speed: float) -> bool:
        self.state = replace(self.state, speed=speed)
        self._publish()
        return True

    def set_metadata(self, **changes: str) -> None:
        self.metadata = replace(self.metadata, **changes)
        self._publish()

    def add_state_listener(self, callback) -> None:
        self.state_listeners.append(callback)

    def remove_state_listener(self, callback) -> None:
        if callback in self.state_listeners:
            self.state_listeners.remove(callback)

    def add_close_callback(self, callback) -> None:
        self.close_callbacks.append(callback)

    def remove_close_callback(self, callback) -> None:
        if callback in self.close_callbacks:
            self.close_callbacks.remove(callback)

    def close(self) -> None:
        self.close_calls += 1
        for callback in tuple(self.close_callbacks):
            callback()
        self.close_callbacks.clear()


class FakeServices:
    def __init__(self) -> None:
        self.loaded_stations: list[GuideStation] = []

    def fetch_feed(self, _url: str) -> ParsedFeed:
        return ParsedFeed("Example feed", (ARTICLE, UNTITLED_ARTICLE))

    def record_feed_seen(self, _kind: str, _url: str, _articles) -> None:
        return None

    def search_directory(self, _kind: str, _query: str):
        return [DirectoryEntry("Directory result", SUBSCRIPTION.url, "Provider")]

    def guide_stations(self, medium: str):
        if medium == GuideMedium.TELEVISION.value:
            return list(GUIDE_STATIONS)
        return [RADIO_STATION]

    def load_program(self, station, _date):
        self.loaded_stations.append(station)
        return [PROGRAMME]

    def create_player(self, callback):
        return FakePlayer(callback)


class SmokeApplication(Adw.Application):
    version = "smoke"

    def __init__(self) -> None:
        super().__init__(application_id="cz.pvlcek.arss.GuiSmoke", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.shutdown_event = threading.Event()
        self.state = MemoryState()
        self.services = FakeServices()
        self.main_window = None
        self.windows = []
        self.passed = False

    def do_activate(self) -> None:
        try:
            self.main_window = MainWindow(self, self.state, self.services)
            self.main_window.present()
            GLib.timeout_add(150, self.exercise)
        except BaseException:
            traceback.print_exc()
            self.quit()

    def exercise(self) -> bool:
        try:
            assert self.main_window is not None
            main = self.main_window
            station = main.guide_page.station
            station_expression = station.get_expression()
            assert station.get_enable_search()
            assert station.get_tooltip_text() == (
                "Open the list and press Shift+Tab to reach the search field."
            )
            assert station_expression is not None
            assert station_expression.get_value_type() == GObject.TYPE_STRING
            assert (
                station.get_search_match_mode()
                == Gtk.StringFilterMatchMode.PREFIX
            )
            assert (
                station.get_accessible_role()
                == Gtk.AccessibleRole.COMBO_BOX
            )
            expected_page_actions = (
                "Add address",
                "Search directory",
                "Import OPML",
                "Export OPML",
            )
            for page, name, direct_count in (
                (main.rss_page, "RSS options", 2),
                (main.podcast_page, "Podcast options", 1),
            ):
                assert page.options_button.get_focusable()
                assert page.options_button.get_tooltip_text() == name
                assert page.options_button.get_popover() is page.options_menu
                assert [
                    item.get_label() for item in page.options_menu.items
                ] == list(expected_page_actions)
                assert all(
                    item.get_accessible_role() == Gtk.AccessibleRole.MENU_ITEM
                    for item in page.options_menu.items
                )
                direct_actions = widget_children(page.actions)
                assert len(direct_actions) == direct_count
                assert page.options_button in direct_actions
                assert not any(
                    isinstance(widget, Gtk.Button)
                    and widget.get_label() in expected_page_actions[:2]
                    for widget in direct_actions
                )

            background_switch = main.settings_page.background_switch
            background_box = background_switch.get_parent()
            assert isinstance(background_box, Gtk.Box)
            background_label = background_box.get_first_child()
            assert isinstance(background_label, Gtk.Label)
            assert background_label.get_mnemonic_keyval() == Gdk.KEY_VoidSymbol
            assert background_switch.get_accessible_role() == Gtk.AccessibleRole.SWITCH

            source_list = main.rss_page.list_box
            assert source_list.get_tab_behavior() == Gtk.ListTabBehavior.ITEM
            first_source_content = list_item_child(source_list, 0)
            second_source_content = list_item_child(source_list, 1)
            first_source = list_item_focus_widget(source_list, 0)
            second_source = list_item_focus_widget(source_list, 1)
            assert isinstance(first_source_content, Adw.WrapBox)
            assert isinstance(second_source_content, Adw.WrapBox)
            assert first_source is not None
            assert second_source is not None
            first_title = first_source_content.get_first_child()
            first_options = first_source_content.get_last_child()
            assert first_title is not None
            assert isinstance(first_options, Gtk.MenuButton)
            assert not first_title.get_focusable()
            assert (
                first_title.get_accessible_role()
                == Gtk.AccessibleRole.PRESENTATION
            )
            assert first_source.get_focusable()
            assert (
                first_source.get_accessible_role()
                == Gtk.AccessibleRole.LIST_ITEM
            )
            first_source.grab_focus()
            assert main.get_focus() is first_source
            assert source_list.get_model().get_selected() == 0
            assert main.child_focus(Gtk.DirectionType.DOWN)
            assert main.get_focus() is second_source
            assert main.child_focus(Gtk.DirectionType.UP)
            assert main.get_focus() is first_source
            main.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
            assert is_focus_within(main.get_focus(), first_options)
            main.emit("move-focus", Gtk.DirectionType.TAB_BACKWARD)
            assert main.get_focus() is first_source
            player_window = main.open_player(
                UNTITLED_ARTICLE,
                SUBSCRIPTION.title,
            )
            self.windows = [
                AddSubscriptionWindow(main, "rss"),
                RenameWindow(main, main.rss_page, SUBSCRIPTION),
                ItemsWindow(main, "rss", SUBSCRIPTION),
                DirectoryWindow(main, "rss"),
                player_window,
                ProgramWindow(main, STATION, date(2026, 8, 2)),
                ProgramDetailWindow(main, PROGRAMME),
                ArticleInfoWindow(main, ARTICLE),
                ProgramDetailWindow(main, UNTITLED_PROGRAMME),
                ArticleInfoWindow(main, UNTITLED_ARTICLE),
            ]
            for item_list in (
                main.podcast_page.list_box,
                self.windows[2].list_box,
                self.windows[3].results,
                self.windows[5].list_box,
            ):
                assert (
                    item_list.get_tab_behavior()
                    == Gtk.ListTabBehavior.ITEM
                )
            assert player_window.episode_title == "The episode title is unavailable."
            assert player_window.player.metadata.title == player_window.episode_title
            assert player_window.player.metadata.artist == SUBSCRIPTION.title
            assert player_window.player.metadata.media_uri == UNTITLED_ARTICLE.media_url
            assert player_window.volume.get_focusable()
            assert player_window.volume.get_accessible_role() == Gtk.AccessibleRole.SLIDER
            adjustment = player_window.volume.get_adjustment()
            assert adjustment.get_lower() == 0
            assert adjustment.get_upper() == 100
            assert player_window.volume.get_sensitive()
            assert adjustment.get_step_increment() == 1
            player_window.volume.set_value(35)
            assert player_window.player.state.volume == 0.35
            volume_calls = len(player_window.player.volume_calls)
            external_state = replace(player_window.player.state, volume=0.65)
            player_window._render(external_state)
            assert player_window.volume.get_value() == 65
            assert len(player_window.player.volume_calls) == volume_calls
            assert player_window.player.toggle()
            assert player_window.player.state.phase is PlaybackPhase.PLAYING
            assert adjustment.get_page_increment() == 10
            details = self.windows[6].details
            assert details.get_focusable()
            assert details.get_selectable()
            assert details.get_accessible_role() == Gtk.AccessibleRole.LABEL
            assert "Programme description" in details.get_text()
            player_window._render(PlaybackState(PlaybackPhase.READY, 0, 0))
            assert not player_window.scale.get_sensitive()
            assert not player_window.seek_back.get_sensitive()
            assert not player_window.seek_forward.get_sensitive()
            assert player_window.play.get_sensitive()
            player_window._render(PlaybackState(PlaybackPhase.READY, 0, 60_000))
            assert player_window.scale.get_sensitive()
            assert self.windows[-1].shown_title == "The article title is unavailable."
            add_window = self.windows[0]
            add_window._add(add_window.get_default_widget())
            assert add_window.status.get_visible()
            rename_window = self.windows[1]
            rename_window.entry.set_text("")
            rename_window._save(None)
            assert rename_window.status.get_visible()
            directory_window = self.windows[3]
            directory_window.present()
            directory_window._search()
            assert directory_window.status.get_visible()
            directory_window.query.set_text("example")
            directory_window._search()
            # Mapping two data-backed windows also checks their async delivery path.
            self.windows[2].present()
            self.windows[5].present()
            main.select_page("guide")
            GLib.timeout_add(700, self.finish)
        except BaseException:
            traceback.print_exc()
            self.quit()
        return GLib.SOURCE_REMOVE

    def finish(self) -> bool:
        try:
            assert self.main_window is not None
            guide = self.main_window.guide_page
            assert guide.station.get_sensitive()
            assert [station.id for station in guide.stations] == [
                "centrum:1",
                "centrum:3",
                "centrum:465",
                "sms:Nova Sport 6",
                "centrum:4",
            ]
            model = guide.station.get_model()
            assert model is not None
            assert [
                model.get_item(position).get_string()
                for position in range(model.get_n_items())
            ] == ["ČT1", "Nova", "Nova Sport 2", "Nova Sport 6", "Prima"]
            selected = guide.station.get_selected()
            assert guide.stations[selected].id == "sms:Nova Sport 6"

            self.station_popover = next(
                widget
                for widget in widget_descendants(guide.station)
                if isinstance(widget, Gtk.Popover)
            )
            self.station_popover.popup()
            GLib.timeout_add(150, self.finish_station_popup)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def finish_station_popup(self) -> bool:
        try:
            assert self.main_window is not None
            station = self.main_window.guide_page.station
            descendants = widget_descendants(station)
            search = next(
                widget
                for widget in descendants
                if isinstance(widget, Gtk.SearchEntry)
                and widget.get_mapped()
            )
            station_lists = [
                widget
                for widget in descendants
                if isinstance(widget, Gtk.ListView)
                and widget.get_mapped()
            ]
            assert len(station_lists) == 1
            self.station_search = search
            self.station_popup_list = station_lists[0]
            search.set_text("Pr")
            GLib.timeout_add(500, self.finish_station_search)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def finish_station_search(self) -> bool:
        try:
            assert self.main_window is not None
            filtered = self.station_popup_list.get_model()
            assert filtered is not None
            filtered_names = [
                filtered.get_item(position).get_string()
                for position in range(filtered.get_n_items())
            ]
            assert filtered_names == ["Prima"], filtered_names
            self.station_search.set_text("")

            guide = self.main_window.guide_page
            self.station_popover.popdown()
            existing = set(self.get_windows())
            guide._show(guide.show)
            opened = next(
                window
                for window in self.get_windows()
                if window not in existing
                and isinstance(window, ProgramWindow)
            )
            assert opened.station.id == "sms:Nova Sport 6"
            assert (
                self.main_window.state.get("guide_television_station_id")
                == "sms:Nova Sport 6"
            )
            opened.close()

            items_window = self.windows[2]
            items_window.present()
            self._items_navigation_waits = 0
            GLib.timeout_add(25, self.wait_for_items_navigation)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def wait_for_items_navigation(self) -> bool:
        try:
            items_window = self.windows[2]
            rows = (
                list_item_focus_widget(items_window.list_box, 0),
                list_item_focus_widget(items_window.list_box, 1),
            )
            if all(
                row is not None
                and row.get_mapped()
                and row.get_width() > 0
                and row.get_height() > 0
                for row in rows
            ):
                assert rows[0] is not None
                rows[0].grab_focus()
                GLib.timeout_add(300, self.finish_items_navigation)
                return GLib.SOURCE_REMOVE
            self._items_navigation_waits += 1
            if self._items_navigation_waits >= 40:
                raise AssertionError("Article rows did not finish allocating")
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def finish_items_navigation(self) -> bool:
        try:
            items_window = self.windows[2]
            first_content = list_item_child(items_window.list_box, 0)
            first_article = list_item_focus_widget(items_window.list_box, 0)
            second_article = list_item_focus_widget(items_window.list_box, 1)
            assert isinstance(first_content, Adw.WrapBox)
            assert first_article is not None
            assert second_article is not None
            assert first_article.get_focusable()
            assert (
                first_article.get_accessible_role()
                == Gtk.AccessibleRole.LIST_ITEM
            )
            info = first_content.get_last_child()
            assert isinstance(info, Gtk.Button)
            assert items_window.get_focus() is first_article
            items_window.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
            assert items_window.get_focus() is info
            items_window.emit("move-focus", Gtk.DirectionType.TAB_BACKWARD)
            assert items_window.get_focus() is first_article

            directory_window = self.windows[3]
            directory_window.present()
            result = list_item_focus_widget(directory_window.results, 0)
            assert result is not None
            result.grab_focus()
            directory_window.results.emit("activate", 0)
            GLib.timeout_add(400, self.finish_directory)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def finish_directory(self) -> bool:
        try:
            directory_window = self.windows[3]
            result = list_item_focus_widget(directory_window.results, 0)
            assert result is not None
            assert directory_window.get_focus() is result, (
                "Directory did not restore its result row: "
                f"{type(directory_window.get_focus()).__name__}"
            )
            player_window = self.windows[4]
            assert player_window.player.state.phase is PlaybackPhase.PLAYING
            assert player_window.player.pause_calls == 0

            assert self.main_window is not None
            for kind in ("rss", "podcast"):
                self.main_window.state.feeds[kind] = []
            self.main_window.refresh_subscriptions()
            for page in (self.main_window.rss_page, self.main_window.podcast_page):
                assert not page.list_scroll.get_visible()
                assert page.empty.get_visible()
                assert page.empty.get_focusable()
                assert page.empty.get_selectable()
                assert page.empty.get_accessible_role() == Gtk.AccessibleRole.LABEL
                assert page.empty.get_text().strip()

            player_window.close()
            assert player_window.player.close_calls == 1
            self.passed = True
        except BaseException:
            traceback.print_exc()
        finally:
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _close_and_quit(self) -> None:
        for window in self.windows:
            window.close()
        if self.main_window is not None:
            self.main_window.close()
        self.quit()

    def preferences_changed(self, _key: str | None = None) -> None:
        return None

    def do_shutdown(self) -> None:
        self.shutdown_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        Adw.Application.do_shutdown(self)


if __name__ == "__main__":
    smoke = SmokeApplication()
    status = smoke.run([sys.argv[0]])
    raise SystemExit(status if status else (0 if smoke.passed else 1))
