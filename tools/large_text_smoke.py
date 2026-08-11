#!/usr/bin/env python3
"""Verify ARSS reflow at roughly 200 percent text size and 320 CSS pixels."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import sys
import threading
import traceback

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from arss.application import PreferencesRecoveryWindow  # noqa: E402
from arss.gtk_helpers import FormWindow, list_item_child  # noqa: E402
from arss.ui import (  # noqa: E402
    AddSubscriptionWindow,
    ArticleInfoWindow,
    DirectoryWindow,
    ItemsWindow,
    MainWindow,
    ProgramDetailWindow,
    ProgramWindow,
    RenameWindow,
)
from tools.gui_smoke import (  # noqa: E402
    ARTICLE,
    PROGRAMME,
    STATION,
    SUBSCRIPTION,
    FakeServices,
    MemoryState,
    UNTITLED_ARTICLE,
    UNTITLED_PROGRAMME,
    widget_descendants,
)


MAX_REFLOW_WIDTH = 320
PLATFORM_WINDOW_MINIMUM = 360


def minimum_width(widget: Gtk.Widget) -> int:
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1).minimum


def matching_labels(widget: Gtk.Widget, text: str) -> list[Gtk.Label]:
    return [
        child
        for child in widget_descendants(widget)
        if isinstance(child, Gtk.Label) and child.get_text() == text
    ]


class LargeTextApplication(Adw.Application):
    version = "large-text-smoke"

    def __init__(self, language: str) -> None:
        suffix = "Cs" if language == "cs" else "En"
        super().__init__(
            application_id=f"cz.pvlcek.arss.LargeText{suffix}",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.language = language
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.shutdown_event = threading.Event()
        self.state = MemoryState(language)
        self.services = FakeServices()
        self.main_window: MainWindow | None = None
        self.windows: list[Gtk.Window] = []
        self.passed = False
        self.provider: Gtk.CssProvider | None = None

    def do_activate(self) -> None:
        try:
            provider = Gtk.CssProvider()
            provider.load_from_string("* { font-size: 22pt; }")
            display = Gdk.Display.get_default()
            assert display is not None
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self.provider = provider
            self.main_window = MainWindow(self, self.state, self.services)
            self.main_window.set_default_size(320, 480)
            self.main_window.present()
            GLib.timeout_add(500, self.exercise)
        except BaseException:
            traceback.print_exc()
            self.quit()

    def exercise(self) -> bool:
        try:
            assert self.main_window is not None
            main = self.main_window
            player = main.open_player(
                UNTITLED_ARTICLE,
                SUBSCRIPTION.title,
            )
            form_windows: list[FormWindow] = [
                AddSubscriptionWindow(main, "rss"),
                RenameWindow(main, main.rss_page, SUBSCRIPTION),
                ItemsWindow(main, "rss", SUBSCRIPTION),
                DirectoryWindow(main, "rss"),
                player,
                ProgramWindow(main, STATION, date(2026, 8, 2)),
                ProgramDetailWindow(main, PROGRAMME),
                ArticleInfoWindow(main, ARTICLE),
                ProgramDetailWindow(main, UNTITLED_PROGRAMME),
                ArticleInfoWindow(main, UNTITLED_ARTICLE),
            ]
            items_window = form_windows[2]
            assert isinstance(items_window, ItemsWindow)
            items_window._loaded(
                self.services.fetch_feed(SUBSCRIPTION.url)
            )
            recovery = PreferencesRecoveryWindow(
                self,
                ValueError("broken/preferences/value/without/spaces"),
            )
            self.windows = [*form_windows, recovery]

            assert main.get_width() <= PLATFORM_WINDOW_MINIMUM, (
                self.language,
                main.get_width(),
            )
            for page in main.pages:
                assert minimum_width(page) <= MAX_REFLOW_WIDTH, (
                    self.language,
                    type(page).__name__,
                    minimum_width(page),
                )
            assert minimum_width(main.page_switcher) <= MAX_REFLOW_WIDTH

            tabs = main.page_tabs()
            assert len(tabs) == len(main._page_labels) == 4
            for tab, text in zip(tabs, main._page_labels, strict=True):
                assert tab.get_tooltip_text() == text
                visible = [
                    label
                    for label in matching_labels(tab, text)
                    if label.get_mapped()
                ]
                assert visible, (self.language, text)
                assert all(
                    not label.get_layout().is_ellipsized()
                    and 1 <= label.get_layout().get_line_count() <= 3
                    for label in visible
                ), (self.language, text)

            for window in form_windows:
                width = minimum_width(window.content)
                assert width <= MAX_REFLOW_WIDTH, (
                    self.language,
                    type(window).__name__,
                    width,
                )
            recovery_scroll = recovery.get_content()
            assert isinstance(recovery_scroll, Gtk.ScrolledWindow)
            recovery_body = recovery_scroll.get_child()
            assert recovery_body is not None
            assert minimum_width(recovery_body) <= MAX_REFLOW_WIDTH

            first_source = list_item_child(main.rss_page.list_box, 0)
            first_article = list_item_child(form_windows[2].list_box, 0)
            assert isinstance(first_source, Adw.WrapBox)
            assert isinstance(first_article, Adw.WrapBox)
            row_menu_button = first_source.get_last_child()
            assert isinstance(row_menu_button, Gtk.MenuButton)
            menus = (
                main.rss_page.options_menu,
                main.podcast_page.options_menu,
                row_menu_button.get_popover(),
            )
            assert all(menu is not None for menu in menus)
            for menu in menus:
                assert menu is not None
                assert minimum_width(menu) <= MAX_REFLOW_WIDTH
                for item in menu.items:
                    labels = matching_labels(item, item.get_label())
                    assert labels and all(label.get_wrap() for label in labels)

            for root in (main, *form_windows, recovery):
                for widget in (root, *widget_descendants(root)):
                    if isinstance(widget, Gtk.CheckButton):
                        text = widget.get_label()
                    elif isinstance(widget, Gtk.Button):
                        text = widget.get_label()
                    else:
                        continue
                    if not text:
                        continue
                    labels = matching_labels(widget, text)
                    assert labels, (type(widget).__name__, text)
                    assert all(
                        label.get_wrap()
                        and label.get_wrap_mode().value_nick == "word-char"
                        for label in labels
                    ), (type(widget).__name__, text)

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


def run_language(language: str) -> bool:
    app = LargeTextApplication(language)
    status = app.run([sys.argv[0]])
    return status == 0 and app.passed


if __name__ == "__main__":
    if not all(run_language(language) for language in ("en", "cs")):
        raise SystemExit(1)
    print("Large-text smoke test passed for English and Czech")
