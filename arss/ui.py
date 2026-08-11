"""Accessible GTK 4 user interface for ARSS.

Business logic is injected through ``DesktopState`` and ``DesktopServices``-like
objects.  Keeping GTK out of the parser and persistence modules makes those
parts testable without a display and keeps the AT-SPI tree deliberately small.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date as Date
import locale as system_locale
from pathlib import Path
import time
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import __version__
from .directory import podcast_search_locale, search_text_matches
from .guide import (
    GuideMedium,
    guide_date_at as guide_date_for_instant,
    order_guide_stations,
)
from .gtk_helpers import (
    AccessibleMenuPopover,
    BusyBlock,
    FormWindow,
    LiveStatus,
    PresentationLabel,
    ReadableStatus,
    alert,
    append_list_item,
    clear_box,
    clear_list,
    confirm,
    description,
    focus_exact_later,
    focus_list_item_later,
    focus_later,
    heading,
    labelled,
    navigable_list,
    readable_description,
    scrolled_content,
    set_invalid,
    update_list_item,
    wrapping_button,
    wrapping_check_button,
)
from .i18n import Translator
from .mpris import MprisService
from .models import FeedArticle, FeedSubscription, ParsedFeed
from .opml import merge_subscriptions, read_opml, write_opml


T = TypeVar("T")
MAIN_PAGE_NAMES = ("rss", "podcast", "guide", "settings")

THANK_AUTHOR_URL = (
    "https://obchod.pvlcek.cz/produkt/"
    "kupte-autorovi-kavu-podpora-tvorby-podcastu-a-karosy/"
)


@dataclass(frozen=True, slots=True)
class OpmlImportResult:
    """New OPML candidates accepted by validation and the rejected count."""

    accepted: tuple[FeedSubscription, ...]
    skipped_podcasts: int = 0


class State(Protocol):
    def subscriptions(self, kind: str) -> list[FeedSubscription]: ...

    def save_subscriptions(self, kind: str, items: Sequence[FeedSubscription]) -> None: ...

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


class Services(Protocol):
    def fetch_feed(self, url: str) -> ParsedFeed: ...

    def record_feed_seen(
        self,
        kind: str,
        feed_url: str,
        articles: Sequence[FeedArticle],
    ) -> None: ...

    def search_directory(self, kind: str, query: str) -> Sequence[Any]: ...

    def guide_stations(self, medium: str) -> Sequence[Any]: ...

    def load_program(self, station: Any, selected_date: Date) -> Sequence[Any]: ...

    def create_player(self, callback: Callable[[Any], None]) -> Any: ...


def valid_web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def current_guide_date(epoch_millis: float | int | None = None) -> Date:
    """Return the broadcaster date using the domain Europe/Prague rules."""

    instant_millis = (
        int(time.time() * 1000) if epoch_millis is None else int(epoch_millis)
    )
    value = guide_date_for_instant(instant_millis)
    return Date(value.year, value.month, value.day)


def feed_item_title(
    item: FeedArticle,
    kind: str,
    translator: Translator,
) -> str:
    """Return the localized Android-compatible title for a feed item."""

    title = str(item.title or "").strip()
    if title:
        return title
    return translator("unknown_episode" if kind == "podcast" else "unknown_article")


def program_title(entry: Any, translator: Translator) -> str:
    """Return a non-empty localized programme title for lists and details."""

    return str(getattr(entry, "title", "") or "").strip() or translator(
        "unknown_program"
    )


def application_version(application: object | None) -> str:
    """Return a usable runtime version without a stale hard-coded fallback."""

    value = str(getattr(application, "version", "") or "").strip()
    return value or __version__


def playback_control_sensitivity(state: Any) -> tuple[bool, bool]:
    """Return ``(prepared, timeline_seekable)`` for accessible player controls."""

    phase = str(getattr(state, "phase", "ready")).lower().split(".")[-1]
    prepared = phase in {"ready", "playing", "paused", "completed"}
    duration_ms = int(getattr(state, "duration_ms", 0) or 0)
    return prepared, prepared and duration_ms > 0


def subscription_matches_filter(query: str, subscription: FeedSubscription) -> bool:
    """Match every normalized query term against the visible subscription title."""

    return search_text_matches(query, subscription.title)


def subscription_successor_after_removal(
    removed: FeedSubscription,
    visible_before: Sequence[FeedSubscription],
    visible_after: Sequence[FeedSubscription],
) -> FeedSubscription | None:
    """Choose the next still-visible row at the removed row position."""

    if not visible_after:
        return None
    try:
        removed_index = visible_before.index(removed)
    except ValueError:
        removed_index = 0
    return visible_after[min(removed_index, len(visible_after) - 1)]


def directory_search_locale(
    language_setting: str,
    device_locale: str | None = None,
) -> str:
    """Return the podcast storefront locale selected by the Android client."""

    system_value = device_locale or system_locale.getlocale()[0] or "en-US"
    app_value = system_value if language_setting == "system" else language_setting
    return podcast_search_locale(app_value, system_value)


def record_manual_checkpoint(
    services: object,
    kind: str,
    feed_url: str,
    articles: Iterable[FeedArticle],
) -> None:
    """Advance notification state after a successful user-requested feed load.

    A checkpoint failure must never turn a successfully downloaded feed into a
    UI error, matching the best-effort checkpoint update in the Android app.
    """

    try:
        eligible = tuple(
            article
            for article in articles
            if kind != "podcast" or bool((article.media_url or "").strip())
        )
        callback = getattr(services, "record_feed_seen")
        callback(kind, feed_url, eligible)
    except Exception:
        pass


def opml_import_message(
    translator: Translator,
    imported_count: int,
    skipped_podcasts: int,
) -> str:
    if imported_count <= 0:
        return translator(
            "opml_podcasts_not_imported"
            if skipped_podcasts > 0
            else "opml_nothing"
        )
    if skipped_podcasts > 0:
        return translator(
            "opml_imported_with_skipped",
            count=imported_count,
            skipped=skipped_podcasts,
        )
    return translator("opml_imported", count=imported_count)


def open_uri(parent: Gtk.Window, uri: str, translator: Translator) -> None:
    if not valid_web_url(uri):
        alert(parent, translator("error"), translator("invalid_address"))
        return
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error as error:
        alert(parent, translator("error"), str(error))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, state: State, services: Services) -> None:
        super().__init__(application=application)
        self.state = state
        self.services = services
        self._player_window: PlayerWindow | None = None
        self.t = Translator(str(state.get("language", "system")))
        self.set_title("ARSS")
        self.set_default_size(1000, 760)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_focusable(False)
        header = Gtk.HeaderBar()
        root.append(header)
        header.set_title_widget(Gtk.Label(label="ARSS"))

        self.overlay = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.page_switcher = Adw.ViewSwitcher(
            stack=self.stack,
            policy=Adw.ViewSwitcherPolicy.NARROW,
        )
        self.page_switcher.set_hexpand(True)
        self._page_tab_handlers: dict[Gtk.Widget, int] = {}
        self._initial_page_focus_pending = True
        self.page_switcher.set_tooltip_text(self.t("app_subtitle"))
        self.page_switcher.connect("map", self._page_switcher_mapped)
        root.append(self.page_switcher)
        self.overlay.set_child(self.stack)
        root.append(self.overlay)
        self.set_content(root)

        self.rss_page = SubscriptionPage(self, "rss")
        self.podcast_page = SubscriptionPage(self, "podcast")
        self.guide_page = GuidePage(self)
        self.settings_page = SettingsPage(self)
        self.pages: list[Gtk.Widget] = [
            self.rss_page,
            self.podcast_page,
            self.guide_page,
            self.settings_page,
        ]
        self._page_labels = tuple(
            self.t(key) for key in ("rss", "podcasts", "guide", "settings")
        )
        for page, name, label in zip(
            self.pages,
            MAIN_PAGE_NAMES,
            self._page_labels,
            strict=True,
        ):
            page.set_accessible_role(Gtk.AccessibleRole.TAB_PANEL)
            self.stack.add_titled(page, name, label)

        self._install_actions(application)
        self.connect("close-request", self._close_requested)

    def _install_actions(self, application: Adw.Application) -> None:
        page_action = Gio.SimpleAction.new("page", GLib.VariantType.new("i"))
        page_action.connect("activate", self._activate_page)
        self.add_action(page_action)
        for number in range(1, 5):
            application.set_accels_for_action(
                f"win.page({number - 1})",
                [f"<Alt>{number}"],
            )
        application.set_accels_for_action("win.close", ["<Ctrl>q"])
        close_action = Gio.SimpleAction.new("close", None)
        close_action.connect("activate", lambda *_args: self.close())
        self.add_action(close_action)

    def _activate_page(self, _action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        index = parameter.get_int32()
        self.select_page(index)
        tabs = self.page_tabs()
        if 0 <= index < len(tabs):
            focus_later(tabs[index])

    def select_page(self, page: str | int) -> None:
        """Select a main section by stable name or zero-based index."""

        if isinstance(page, int):
            if not 0 <= page < len(MAIN_PAGE_NAMES):
                return
            name = MAIN_PAGE_NAMES[page]
        else:
            name = "podcast" if page == "podcasts" else page
            if name not in MAIN_PAGE_NAMES:
                return
        self.stack.set_visible_child_name(name)

    def current_page(self) -> int:
        try:
            return MAIN_PAGE_NAMES.index(self.stack.get_visible_child_name())
        except (TypeError, ValueError):
            return 0

    def page_tabs(self) -> tuple[Gtk.Widget, ...]:
        """Expose switcher tabs for deterministic accessibility smoke checks."""

        found: list[Gtk.Widget] = []

        def visit(widget: Gtk.Widget) -> None:
            child = widget.get_first_child()
            while child is not None:
                if child.get_accessible_role() == Gtk.AccessibleRole.TAB:
                    found.append(child)
                visit(child)
                child = child.get_next_sibling()

        visit(self.page_switcher)
        return tuple(found)

    @staticmethod
    def _reflow_page_tab_labels(widget: Gtk.Widget, label: str) -> None:
        child = widget.get_first_child()
        while child is not None:
            if (
                isinstance(child, Gtk.Label)
                and child.get_text() == label
                and child.get_mapped()
            ):
                child.set_ellipsize(Pango.EllipsizeMode.NONE)
                child.set_wrap(True)
                child.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                child.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
                child.set_justify(Gtk.Justification.CENTER)
            MainWindow._reflow_page_tab_labels(child, label)
            child = child.get_next_sibling()

    def _page_tab_clicked(self, tab: Gtk.Button) -> None:
        tab.grab_focus()

        def confirm_page_focus() -> bool:
            if tab.get_mapped():
                tab.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(
            confirm_page_focus, priority=GLib.PRIORITY_LOW
        )

    def _page_switcher_mapped(self, *_args: object) -> None:
        def prepare_tabs() -> tuple[Gtk.Widget, ...]:
            tabs = self.page_tabs()
            for tab, label in zip(tabs, self._page_labels, strict=False):
                tab.set_focusable(True)
                tab.set_tooltip_text(label)
                self._reflow_page_tab_labels(tab, label)
                if (
                    isinstance(tab, Gtk.Button)
                    and tab not in self._page_tab_handlers
                ):
                    tab.set_focus_on_click(True)
                    self._page_tab_handlers[tab] = tab.connect(
                        "clicked", self._page_tab_clicked
                    )
            return tabs

        tabs = prepare_tabs()
        if self._initial_page_focus_pending and tabs:
            tabs[min(self.current_page(), len(tabs) - 1)].grab_focus()

        def confirm_initial_focus() -> bool:
            tabs = prepare_tabs()
            if self._initial_page_focus_pending and tabs:
                target = tabs[min(self.current_page(), len(tabs) - 1)]
                if target.grab_focus():
                    self._initial_page_focus_pending = False
            return GLib.SOURCE_REMOVE

        # Confirm once the top-level window has completed GTK's default-focus
        # pass; otherwise it can replace the intended initial tab focus.
        GLib.timeout_add(50, confirm_initial_focus)

    def open_player(
        self,
        episode: FeedArticle,
        feed_title: str = "",
    ) -> PlayerWindow:
        """Present one lifecycle-owned player and replace any previous one."""

        current = self._player_window
        if current is not None:
            current.close()
        player_window = PlayerWindow(self, episode, feed_title=feed_title)
        self._player_window = player_window
        player_window.present()
        return player_window

    def player_window_closed(self, player_window: PlayerWindow) -> None:
        if self._player_window is player_window:
            self._player_window = None

    def _close_requested(self, *_args: object) -> bool:
        for child in tuple(self.get_application().get_windows() if self.get_application() else ()):
            if isinstance(child, PlayerWindow):
                child.close()
        return False

    def run_async(
        self,
        work: Callable[[], T],
        success: Callable[[T], None],
        failure: Callable[[BaseException], None],
    ) -> Future[T]:
        application = self.get_application()
        shutdown_event = getattr(application, "shutdown_event", None)
        if shutdown_event is not None and shutdown_event.is_set():
            cancelled: Future[T] = Future()
            cancelled.cancel()
            return cancelled
        executor = getattr(application, "executor")
        try:
            future: Future[T] = executor.submit(work)
        except RuntimeError:
            if shutdown_event is None or not shutdown_event.is_set():
                raise
            cancelled = Future()
            cancelled.cancel()
            return cancelled

        def complete(done: Future[T]) -> None:
            if shutdown_event is not None and shutdown_event.is_set():
                return

            def deliver() -> bool:
                if shutdown_event is not None and shutdown_event.is_set():
                    return GLib.SOURCE_REMOVE
                try:
                    success(done.result())
                except BaseException as error:  # errors are rendered on the GTK thread
                    failure(error)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(deliver)

        future.add_done_callback(complete)
        return future

    def toast(self, message: str) -> None:
        self.overlay.add_toast(Adw.Toast.new(message))
        self.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def refresh_subscriptions(self) -> None:
        self.rss_page.reload()
        self.podcast_page.reload()

    def preferences_changed(self, key: str) -> None:
        if key == "filter_after_list":
            self.rss_page.reflow()
            self.podcast_page.reflow()
        callback = getattr(self.get_application(), "preferences_changed", None)
        if callback is not None:
            callback(key)


class SubscriptionPage(Gtk.Box):
    def __init__(self, window: MainWindow, kind: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_focusable(False)
        self.window = window
        self.kind = kind
        self.items: list[FeedSubscription] = []
        self.store_error: BaseException | None = None
        self.mutation_buttons: list[Gtk.Widget] = []
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(18)
        self.set_margin_end(18)

        title_key = "rss_heading" if kind == "rss" else "podcasts_heading"
        self.append(heading(window.t(title_key)))
        actions = Adw.WrapBox(
            child_spacing=8,
            line_spacing=8,
            wrap_policy=Adw.WrapPolicy.NATURAL,
        )
        actions.set_focusable(False)
        actions.set_halign(Gtk.Align.START)
        if kind == "rss":
            default_button = wrapping_button(window.t("open_default"))
            default_button.connect("clicked", self._open_default)
            actions.append(default_button)
        options_key = "rss_options" if kind == "rss" else "podcast_options"
        options_label = window.t(options_key)
        more_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        more_button.set_focusable(True)
        more_button.set_tooltip_text(options_label)
        more_button.update_property(
            [
                Gtk.AccessibleProperty.LABEL,
                Gtk.AccessibleProperty.HAS_POPUP,
            ],
            [options_label, True],
        )
        more_menu = AccessibleMenuPopover(more_button, options_label)
        more_menu.append_item(
            window.t("add_address"),
            lambda: AddSubscriptionWindow(window, kind).present(),
        )
        more_menu.append_item(
            window.t("search_directory"),
            lambda: DirectoryWindow(window, kind).present(),
        )
        more_menu.append_item(window.t("import_opml"), lambda: self._import_opml(None))
        more_menu.append_item(window.t("export_opml"), lambda: self._export_opml(None))
        more_button.set_popover(more_menu)
        actions.append(more_button)
        self.mutation_buttons.append(more_button)
        self.actions = actions
        self.options_button = more_button
        self.options_menu = more_menu
        self.append(actions)

        filter_key = "filter_rss" if kind == "rss" else "filter_podcasts"
        self.filter_entry = Gtk.SearchEntry(placeholder_text=window.t(filter_key))
        self.filter_entry.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [window.t(filter_key)],
        )
        self.filter_entry.connect("search-changed", lambda _entry: self.render())
        self.filter_box = labelled(f"_{window.t(filter_key)}", self.filter_entry)
        self.list_box = navigable_list(window.t(title_key))
        self.list_scroll = scrolled_content(self.list_box)
        self.list_scroll.set_vexpand(True)
        self.empty = ReadableStatus()
        self.retry_store = wrapping_button(window.t("retry"))
        self.retry_store.connect("clicked", lambda _button: self.reload())
        self.retry_store.set_visible(False)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.body.set_focusable(False)
        self.body.set_vexpand(True)
        self.append(self.body)
        self.reload()

    def reload(self) -> None:
        try:
            self.items = sorted(self.window.state.subscriptions(self.kind), key=lambda item: item.title.casefold())
            self.store_error = None
        except Exception as error:
            self.items = []
            self.store_error = error
        available = self.store_error is None
        for button in self.mutation_buttons:
            button.set_sensitive(available)
        self.retry_store.set_visible(not available)
        self.reflow()

    def reflow(self) -> None:
        clear_box(self.body)
        filter_after = bool(self.window.state.get("filter_after_list", False))
        if not filter_after:
            self.body.append(self.filter_box)
        self.body.append(self.list_scroll)
        if filter_after:
            self.body.append(self.filter_box)
        self.body.append(self.empty)
        self.body.append(self.retry_store)
        self.render()

    def render(self, focus_url: str | None = None) -> None:
        clear_list(self.list_box)
        if self.store_error is not None:
            self.list_scroll.set_visible(False)
            self.empty.set_status(
                self.window.t("store_unavailable", detail=str(self.store_error))
            )
            return
        visible = self._visible_items()
        default_url = self.window.state.get("default_feed_url") if self.kind == "rss" else None
        empty_key = "empty_rss" if self.kind == "rss" else "empty_podcasts"
        self.list_scroll.set_visible(bool(visible))
        self.empty.set_status(self.window.t(empty_key) if not visible else "", announce=not visible)
        for index, subscription in enumerate(visible):
            self._row(subscription, default_url)
            if focus_url == subscription.url:
                focus_list_item_later(self.list_box, index)

    def _visible_items(
        self,
        items: Sequence[FeedSubscription] | None = None,
    ) -> list[FeedSubscription]:
        query = self.filter_entry.get_text()
        candidates = self.items if items is None else items
        return [
            item for item in candidates if subscription_matches_filter(query, item)
        ]

    def _row(
        self,
        subscription: FeedSubscription,
        default_url: object | None,
    ) -> None:
        line = Adw.WrapBox(
            child_spacing=8,
            line_spacing=8,
            wrap_policy=Adw.WrapPolicy.NATURAL,
        )
        line.set_focusable(False)
        is_default = self.kind == "rss" and subscription.url == default_url
        title = subscription.title + (self.window.t("default_suffix") if is_default else "")
        title_label = PresentationLabel(
            label=title,
            xalign=0,
            wrap=True,
            hexpand=True,
            halign=Gtk.Align.FILL,
        )
        title_label.add_css_class("subscription-title")
        line.append(title_label)
        options_label = self.window.t("options_for", title=subscription.title)
        menu_button = Gtk.MenuButton(label=self.window.t("options"))
        menu_button.set_focusable(True)
        menu_button.update_property(
            [
                Gtk.AccessibleProperty.LABEL,
                Gtk.AccessibleProperty.HAS_POPUP,
            ],
            [options_label, True],
        )
        menu = AccessibleMenuPopover(menu_button, options_label)
        options: list[tuple[str, Callable[[], None]]] = [
            (self.window.t("open"), lambda: self._open(subscription)),
            (
                self.window.t("open_source"),
                lambda: open_uri(self.window, subscription.url, self.window.t),
            ),
            (self.window.t("copy_address"), lambda: self._copy(subscription.url)),
            (
                self.window.t("rename"),
                lambda: RenameWindow(self.window, self, subscription).present(),
            ),
        ]
        if self.kind == "rss" and not is_default:
            options.append(
                (self.window.t("set_default"), lambda: self._set_default(subscription))
            )
        options.append((self.window.t("delete"), lambda: self._delete(subscription)))
        for label, callback in options:
            menu.append_item(label, callback)
        menu_button.set_popover(menu)
        line.append(menu_button)
        append_list_item(
            self.list_box,
            line,
            label=title,
            description="",
            callback=lambda: self._open(subscription),
        )

    def _open(self, subscription: FeedSubscription) -> None:
        ItemsWindow(self.window, self.kind, subscription).present()

    def _open_default(self, _button: Gtk.Button) -> None:
        url = self.window.state.get("default_feed_url")
        subscription = next((item for item in self.items if item.url == url), None)
        if subscription is None:
            self.window.toast(self.window.t("default_missing"))
        else:
            self._open(subscription)

    def _copy(self, value: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(GObject.Value(str, value))
            self.window.toast(self.window.t("copied"))

    def _set_default(self, subscription: FeedSubscription) -> None:
        self.window.state.set("default_feed_url", subscription.url)
        self.render(focus_url=subscription.url)
        self.window.toast(self.window.t("default_set", title=subscription.title))

    def _delete(self, subscription: FeedSubscription) -> None:
        def remove() -> None:
            visible_before = self._visible_items()
            updated = [item for item in self.items if item != subscription]
            try:
                self.window.state.save_subscriptions(self.kind, updated)
                if self.kind == "rss" and self.window.state.get("default_feed_url") == subscription.url:
                    self.window.state.set("default_feed_url", updated[0].url if updated else None)
                self.items = updated
                successor = subscription_successor_after_removal(
                    subscription,
                    visible_before,
                    self._visible_items(),
                )
                self.render(focus_url=successor.url if successor is not None else None)
                if successor is None:
                    focus_later(self.filter_entry)
                self.window.toast(self.window.t("deleted"))
            except Exception:
                alert(self.window, self.window.t("error"), self.window.t("save_error"))

        confirm(
            self.window,
            self.window.t("delete"),
            self.window.t("confirm_delete", title=subscription.title),
            self.window.t("cancel"),
            self.window.t("delete"),
            remove,
        )

    def replace(self, old: FeedSubscription, new: FeedSubscription) -> None:
        updated = [new if item == old else item for item in self.items]
        self.window.state.save_subscriptions(self.kind, updated)
        if self.kind == "rss" and self.window.state.get("default_feed_url") == old.url:
            self.window.state.set("default_feed_url", new.url)
        self.items = sorted(updated, key=lambda item: item.title.casefold())
        self.render(focus_url=new.url)
        self.window.toast(self.window.t("renamed"))

    def add_validated(self, subscription: FeedSubscription) -> bool:
        if self.store_error is not None:
            alert(
                self.window,
                self.window.t("error"),
                self.window.t("store_unavailable", detail=str(self.store_error)),
            )
            return False
        if any(item.url == subscription.url for item in self.items):
            self.window.toast(self.window.t("already_exists"))
            return False
        updated = sorted([*self.items, subscription], key=lambda item: item.title.casefold())
        self.window.state.save_subscriptions(self.kind, updated)
        if self.kind == "rss" and not self.window.state.get("default_feed_url"):
            self.window.state.set("default_feed_url", subscription.url)
        self.items = updated
        self.render(focus_url=subscription.url)
        self.window.toast(self.window.t("added", title=subscription.title))
        return True

    def _import_opml(self, _button: Gtk.Button | None) -> None:
        dialog = Gtk.FileDialog(title=self.window.t("choose_opml"), modal=True)

        def chosen(source: Gtk.FileDialog, result: Gio.AsyncResult, *_args: object) -> None:
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return

            existing_snapshot = tuple(self.items)

            def work() -> OpmlImportResult:
                ok, contents, _etag = file.load_contents(None)
                if not ok:
                    raise OSError("OPML could not be read")
                incoming = list(read_opml(contents))
                candidates, _initial_merge = merge_subscriptions(
                    existing_snapshot,
                    incoming,
                )
                if self.kind != "podcast":
                    return OpmlImportResult(candidates)

                playable: list[FeedSubscription] = []
                shutdown_event = getattr(
                    self.window.get_application(), "shutdown_event", None
                )
                for candidate in candidates:
                    if shutdown_event is not None and shutdown_event.is_set():
                        return OpmlImportResult(())
                    try:
                        parsed = self.window.services.fetch_feed(candidate.url)
                    except Exception:
                        continue
                    if any(article.media_url for article in parsed.articles):
                        playable.append(
                            FeedSubscription(
                                parsed.title.strip() or candidate.title,
                                candidate.url,
                            )
                        )
                return OpmlImportResult(
                    tuple(playable),
                    skipped_podcasts=len(candidates) - len(playable),
                )

            def loaded(result: OpmlImportResult) -> None:
                added_items, merged_items = merge_subscriptions(
                    self.items,
                    result.accepted,
                )
                merged = list(merged_items)
                added_count = len(added_items)
                if not added_count:
                    self.window.toast(
                        opml_import_message(
                            self.window.t,
                            0,
                            result.skipped_podcasts,
                        )
                    )
                    return
                self.window.state.save_subscriptions(self.kind, merged)
                self.items = sorted(merged, key=lambda item: item.title.casefold())
                if self.kind == "rss" and not self.window.state.get("default_feed_url"):
                    self.window.state.set("default_feed_url", self.items[0].url)
                self.render(focus_url=added_items[0].url)
                self.window.toast(
                    opml_import_message(
                        self.window.t,
                        added_count,
                        result.skipped_podcasts,
                    )
                )

            self.window.run_async(work, loaded, lambda error: alert(self.window, self.window.t("error"), str(error)))

        dialog.open(self.window, None, chosen)

    def _export_opml(self, _button: Gtk.Button | None) -> None:
        dialog = Gtk.FileDialog(title=self.window.t("save_opml"), modal=True)
        dialog.set_initial_name("readFeeds.opml" if self.kind == "rss" else "podcasts.opml")

        def chosen(source: Gtk.FileDialog, result: Gio.AsyncResult, *_args: object) -> None:
            try:
                file = source.save_finish(result)
            except GLib.Error:
                return
            payload = write_opml(self.items)

            def work() -> None:
                file.replace_contents(payload, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)

            self.window.run_async(work, lambda _value: self.window.toast(self.window.t("opml_exported")), lambda error: alert(self.window, self.window.t("error"), str(error)))

        dialog.save(self.window, None, chosen)


class AddSubscriptionWindow(FormWindow):
    def __init__(self, parent: MainWindow, kind: str) -> None:
        title = parent.t("new_rss" if kind == "rss" else "new_podcast")
        super().__init__(parent, title, parent.t("back"), height=330)
        self.parent_window = parent
        self.kind = kind
        self.content.append(heading(title))
        self.entry = Gtk.Entry(activates_default=True)
        self.entry.set_input_purpose(Gtk.InputPurpose.URL)
        self.content.append(labelled(f"_{parent.t('feed_address')}", self.entry))
        self.busy = BusyBlock()
        self.content.append(self.busy)
        self.status = LiveStatus()
        self.content.append(self.status)
        add = wrapping_button(parent.t("add"))
        add.add_css_class("suggested-action")
        add.connect("clicked", self._add)
        self.set_default_widget(add)
        self.content.append(add)

    def _add(self, button: Gtk.Button) -> None:
        url = self.entry.get_text().strip()
        if not valid_web_url(url):
            set_invalid(self.entry, True)
            self.entry.grab_focus()
            self.status.set_status(self.parent_window.t("invalid_address"))
            return
        set_invalid(self.entry, False)
        self.status.set_status("")
        self.entry.set_sensitive(False)
        button.set_sensitive(False)
        self.busy.start(self.parent_window.t("validating"))

        def loaded(feed: ParsedFeed) -> None:
            self.entry.set_sensitive(True)
            button.set_sensitive(True)
            self.busy.stop()
            if self.kind == "podcast" and not any(article.media_url for article in feed.articles):
                set_invalid(self.entry, True)
                self.status.set_status(self.parent_window.t("not_podcast"))
                self.entry.grab_focus()
                return
            page = self.parent_window.rss_page if self.kind == "rss" else self.parent_window.podcast_page
            title = feed.title.strip() or urlsplit(url).hostname or url
            if page.add_validated(FeedSubscription(title, url)):
                self.close()

        def failed(error: BaseException) -> None:
            self.entry.set_sensitive(True)
            button.set_sensitive(True)
            self.busy.stop()
            self.status.set_status(self.parent_window.t("load_error", detail=str(error)))

        self.parent_window.run_async(lambda: self.parent_window.services.fetch_feed(url), loaded, failed)


class RenameWindow(FormWindow):
    def __init__(self, parent: MainWindow, page: SubscriptionPage, item: FeedSubscription) -> None:
        super().__init__(parent, parent.t("rename"), parent.t("back"), height=300)
        self.parent_window = parent
        self.page = page
        self.item = item
        self.content.append(heading(parent.t("rename")))
        self.entry = Gtk.Entry(text=item.title, activates_default=True)
        self.content.append(labelled(f"_{parent.t('new_name')}", self.entry))
        self.status = LiveStatus()
        self.content.append(self.status)
        save = wrapping_button(parent.t("save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        self.set_default_widget(save)
        self.content.append(save)

    def _save(self, _button: Gtk.Button) -> None:
        title = self.entry.get_text().strip()
        if not title:
            set_invalid(self.entry, True)
            self.status.set_status(self.parent_window.t("name_required"))
            self.entry.grab_focus()
            return
        set_invalid(self.entry, False)
        self.status.set_status("")
        try:
            self.page.replace(self.item, FeedSubscription(title, self.item.url))
        except Exception:
            alert(self, self.parent_window.t("error"), self.parent_window.t("save_error"))
            return
        self.close()


class ItemsWindow(FormWindow):
    def __init__(self, parent: MainWindow, kind: str, subscription: FeedSubscription) -> None:
        title = parent.t("articles" if kind == "rss" else "episodes")
        super().__init__(parent, title, parent.t("back"), width=840, height=700)
        self.parent_window = parent
        self.kind = kind
        self.subscription = subscription
        self.title_label = heading(subscription.title)
        self.content.append(self.title_label)
        self.busy = BusyBlock()
        self.content.append(self.busy)
        self.status = LiveStatus()
        self.content.append(self.status)
        self.list_box = navigable_list(title)
        scroll = scrolled_content(self.list_box)
        scroll.set_vexpand(True)
        self.content.append(scroll)
        self.connect("map", self._start_once)
        self._started = False

    def _start_once(self, *_args: object) -> None:
        if self._started:
            return
        self._started = True
        self.load()

    def load(self) -> None:
        self.busy.start(self.parent_window.t("loading"))
        self.status.set_status("")
        clear_list(self.list_box)
        self.parent_window.run_async(
            lambda: self.parent_window.services.fetch_feed(self.subscription.url),
            self._loaded,
            self._failed,
        )

    def _loaded(self, feed: ParsedFeed) -> None:
        self.busy.stop()
        record_manual_checkpoint(
            self.parent_window.services,
            self.kind,
            self.subscription.url,
            feed.articles,
        )
        items = list(feed.articles)
        if self.kind == "podcast":
            items = [item for item in items if item.media_url]
        count_key = "articles_count" if self.kind == "rss" else "episodes_count"
        list_title = self.parent_window.t(count_key, title=self.subscription.title, count=len(items))
        self.title_label.set_text(list_title)
        self.list_box.update_property([Gtk.AccessibleProperty.LABEL], [list_title])
        if not items:
            self.status.set_status(self.parent_window.t("empty_articles" if self.kind == "rss" else "empty_episodes"))
            return
        show_dates = bool(
            self.parent_window.state.get(
                "show_episode_dates" if self.kind == "podcast" else "show_article_dates",
                False,
            )
        )
        for item in items:
            self._append_item(item, show_dates)
        focus_list_item_later(self.list_box, 0)

    def _failed(self, error: BaseException) -> None:
        self.busy.stop()
        self.status.set_status(self.parent_window.t("load_error", detail=str(error)))
        retry = wrapping_button(self.parent_window.t("retry"))
        retry.connect("clicked", lambda _button: (retry.set_visible(False), self.load()))
        self.content.append(retry)

    def _append_item(self, item: FeedArticle, show_dates: bool) -> None:
        shown_title = feed_item_title(item, self.kind, self.parent_window.t)
        if self.kind == "podcast":
            details = [
                value
                for value in (
                    item.published_text if show_dates else None,
                    item.duration_text,
                )
                if value
            ]
            label = shown_title + ("\n" + " — ".join(details) if details else "")
            child = PresentationLabel(label=label, xalign=0, wrap=True)
            hint = self.parent_window.t("episode_open_hint")
            callback = lambda: self.parent_window.open_player(
                item,
                self.subscription.title,
            )
        else:
            line = Adw.WrapBox(
                child_spacing=8,
                line_spacing=8,
                wrap_policy=Adw.WrapPolicy.NATURAL,
            )
            line.set_focusable(False)
            shown_date = item.published_text if show_dates else None
            label = shown_title + (f"\n{shown_date}" if shown_date else "")
            content = PresentationLabel(
                label=label,
                xalign=0,
                wrap=True,
                hexpand=True,
            )
            hint = self.parent_window.t("article_open_hint")
            info = wrapping_button(self.parent_window.t("information"))
            info.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [self.parent_window.t("information_for", title=shown_title)],
            )
            info.connect(
                "clicked",
                lambda _button: ArticleInfoWindow(
                    self.parent_window,
                    item,
                ).present(),
            )
            line.append(content)
            line.append(info)
            child = line
            callback = lambda: open_uri(
                self,
                item.url,
                self.parent_window.t,
            )
        append_list_item(
            self.list_box,
            child,
            label=label,
            description=hint,
            callback=callback,
        )


class ArticleInfoWindow(FormWindow):
    def __init__(self, parent: MainWindow, article: FeedArticle) -> None:
        super().__init__(parent, parent.t("information"), parent.t("back"), height=420)
        self.parent_window = parent
        self.article = article
        show_date = bool(parent.state.get("show_article_dates", False))
        shown_date = article.published_text if show_date else None
        base_title = feed_item_title(article, "rss", parent.t)
        self.shown_title = (
            f"{base_title} — {shown_date}" if shown_date else base_title
        )
        self.content.append(heading(self.shown_title))
        url_label = readable_description(article.url)
        self.content.append(url_label)
        copy = wrapping_button(parent.t("copy_title_link"))
        copy.connect("clicked", self._copy)
        self.content.append(copy)

    def _copy(self, _button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(
                GObject.Value(str, f"{self.shown_title}\n{self.article.url}")
            )
            self.parent_window.toast(self.parent_window.t("copied"))


class DirectoryWindow(FormWindow):
    def __init__(self, parent: MainWindow, kind: str) -> None:
        title = parent.t("directory_rss" if kind == "rss" else "directory_podcast")
        super().__init__(parent, title, parent.t("back"), width=840, height=700)
        self.parent_window = parent
        self.kind = kind
        self._search_request = 0
        self.content.append(heading(title))
        self.content.append(description(parent.t("rss_directory_note" if kind == "rss" else "podcast_directory_note")))
        self.query = Gtk.SearchEntry(placeholder_text=parent.t("search_phrase"))
        self.query.connect("activate", self._search)
        self.content.append(labelled(f"_{parent.t('search_phrase')}", self.query))
        self.search_button = wrapping_button(parent.t("search"))
        self.search_button.connect("clicked", self._search)
        self.content.append(self.search_button)
        self.busy = BusyBlock()
        self.content.append(self.busy)
        self.status = LiveStatus()
        self.content.append(self.status)
        self.results = navigable_list(parent.t("search_results"))
        scroll = scrolled_content(self.results)
        scroll.set_vexpand(True)
        self.content.append(scroll)

    def _search(self, *_args: object) -> None:
        self._search_request += 1
        request = self._search_request
        query = self.query.get_text().strip()
        if not query:
            set_invalid(self.query, True)
            self.status.set_status(self.parent_window.t("search_required"))
            self.query.grab_focus()
            return
        set_invalid(self.query, False)
        self._set_busy(True, self.parent_window.t("searching"))
        self.status.set_status("")
        clear_list(self.results)

        def loaded(entries: list[Any]) -> None:
            if request != self._search_request:
                return
            self._results_loaded(entries)

        def failed(error: BaseException) -> None:
            if request != self._search_request:
                return
            self._set_busy(False)
            self.status.set_status(str(error))
            focus_exact_later(self.query)

        def work() -> list[Any]:
            if self.kind == "podcast":
                directory = getattr(
                    self.parent_window.services,
                    "podcast_directory",
                    None,
                )
                search = getattr(directory, "search", None)
                if search is not None:
                    language = str(
                        self.parent_window.state.get("language", "system")
                    )
                    return list(
                        search(query, directory_search_locale(language))
                    )
            return list(
                self.parent_window.services.search_directory(self.kind, query)
            )

        self.parent_window.run_async(
            work,
            loaded,
            failed,
        )

    def _results_loaded(self, entries: list[Any]) -> None:
        self._set_busy(False)
        if not entries:
            self.status.set_status(self.parent_window.t("empty_results"))
            focus_exact_later(self.query)
            return
        for position, entry in enumerate(entries):
            detail = " — ".join(
                value
                for value in (getattr(entry, "detail", ""), entry.url)
                if value
            )
            label = f"{entry.title}\n{detail}"
            append_list_item(
                self.results,
                PresentationLabel(label=label, xalign=0, wrap=True),
                label=label,
                description=self.parent_window.t("activate_to_add"),
                callback=lambda entry=entry, position=position: self._add(
                    entry,
                    position,
                ),
            )
        focus_list_item_later(self.results, 0)

    def _add(self, entry: Any, position: int) -> None:
        self._set_busy(True, self.parent_window.t("validating"))

        def loaded(feed: ParsedFeed) -> None:
            self._set_busy(False)
            if self.kind == "podcast" and not any(item.media_url for item in feed.articles):
                self.status.set_status(self.parent_window.t("not_podcast"))
                focus_list_item_later(self.results, position)
                return
            page = self.parent_window.rss_page if self.kind == "rss" else self.parent_window.podcast_page
            subscription = FeedSubscription(feed.title.strip() or entry.title, entry.url)
            if page.add_validated(subscription):
                self.close()
            else:
                focus_list_item_later(self.results, position)

        def failed(error: BaseException) -> None:
            self._set_busy(False)
            self.status.set_status(self.parent_window.t("load_error", detail=str(error)))
            focus_list_item_later(self.results, position)

        self.parent_window.run_async(lambda: self.parent_window.services.fetch_feed(entry.url), loaded, failed)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.query.set_sensitive(not busy)
        self.search_button.set_sensitive(not busy)
        self.results.set_sensitive(not busy)
        if busy:
            self.busy.start(message)
        else:
            self.busy.stop()


class PlayerWindow(FormWindow):
    SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

    def __init__(
        self,
        parent: MainWindow,
        episode: FeedArticle,
        *,
        feed_title: str = "",
    ) -> None:
        super().__init__(parent, parent.t("player"), parent.t("back"), width=720, height=520)
        self.parent_window = parent
        self.episode = episode
        self.player = parent.services.create_player(self._render)
        self.episode_title = feed_item_title(episode, "podcast", parent.t)
        self.content.append(heading(self.episode_title))
        self.status = LiveStatus(parent.t("preparing"))
        self.content.append(self.status)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.scale.set_draw_value(False)
        self.scale.set_sensitive(False)
        self.scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [parent.t("playback_position")],
        )
        self.scale.connect("change-value", self._seek)
        self.content.append(self.scale)
        self.position = PresentationLabel(
            label=parent.t("position", position="0:00", duration="0:00"),
            xalign=0,
            wrap=True,
        )
        self.content.append(self.position)
        controls = Adw.WrapBox(
            child_spacing=8,
            line_spacing=8,
            line_homogeneous=True,
            justify=Adw.JustifyMode.FILL,
            wrap_policy=Adw.WrapPolicy.NATURAL,
        )
        controls.set_focusable(False)
        self.seek_back = wrapping_button(parent.t("seek_back"), hexpand=True)
        self.seek_back.set_sensitive(False)
        self.seek_back.connect("clicked", lambda _button: self.player.seek_by(-15_000))
        self.play = wrapping_button(parent.t("play"))
        self.play.set_hexpand(True)
        self.play.set_sensitive(False)
        self.play.connect("clicked", self._toggle)
        self.seek_forward = wrapping_button(parent.t("seek_forward"), hexpand=True)
        self.seek_forward.set_sensitive(False)
        self.seek_forward.connect("clicked", lambda _button: self.player.seek_by(30_000))
        controls.append(self.seek_back)
        controls.append(self.play)
        controls.append(self.seek_forward)
        self.content.append(controls)
        self._updating_volume = False
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_draw_value(True)
        self.volume.set_digits(0)
        self.volume.set_increments(1, 10)
        self.volume.set_value(100)
        self.volume.set_sensitive(False)
        self.volume.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.VALUE_TEXT],
            [parent.t("volume"), parent.t("volume_percent", value=100)],
        )
        self.volume.connect("value-changed", self._volume_changed)
        self.content.append(labelled(f"_{parent.t('volume')}", self.volume))
        speed_model = Gtk.StringList.new([f"{value:g}×" for value in self.SPEEDS])
        self._updating_speed = False
        self.speed = Gtk.DropDown(model=speed_model, selected=self.SPEEDS.index(1.0))
        self.speed.set_sensitive(False)
        self.speed.connect("notify::selected", self._speed_changed)
        self.content.append(labelled(f"_{parent.t('speed')}", self.speed))
        self.connect("close-request", self._closing)
        try:
            self.player.set_metadata(
                title=self.episode_title,
                artist=feed_title.strip(),
                media_uri=(episode.media_url or "").strip(),
            )
        except Exception:
            pass
        self.mpris = MprisService.try_start(
            self.player,
            raise_callback=self.present,
        )
        media_url = episode.media_url or ""
        try:
            self.player.open(media_url)
        except Exception as error:
            self.status.set_status(parent.t("load_error", detail=str(error)))

    def _toggle(self, _button: Gtk.Button) -> None:
        self.player.toggle()

    def _seek(self, _scale: Gtk.Range, _scroll: Gtk.ScrollType, value: float) -> bool:
        duration_ms = int(getattr(self, "_duration_ms", 0))
        if duration_ms > 0:
            self.player.seek_to(int(duration_ms * value / 1000.0))
        return False

    def _volume_changed(self, scale: Gtk.Scale) -> None:
        if self._updating_volume:
            return
        accepted = self.player.set_volume(scale.get_value() / 100.0)
        if not accepted:
            self._sync_volume(
                float(getattr(self.player.state, "volume", 1.0))
            )

    def _sync_volume(self, volume: float) -> None:
        target = min(100.0, max(0.0, float(volume) * 100.0))
        if abs(self.volume.get_value() - target) > 0.001:
            was_updating = self._updating_volume
            self._updating_volume = True
            try:
                self.volume.set_value(target)
            finally:
                self._updating_volume = was_updating
        text = self.parent_window.t("volume_percent", value=round(target))
        self.volume.update_property(
            [Gtk.AccessibleProperty.VALUE_TEXT],
            [text],
        )

    def _speed_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        if self._updating_speed:
            return
        selected = dropdown.get_selected()
        if selected < len(self.SPEEDS):
            self._updating_speed = True
            try:
                accepted = self.player.set_speed(self.SPEEDS[selected])
            finally:
                self._updating_speed = False
            if not accepted:
                self._sync_speed(float(getattr(self.player.state, "speed", 1.0)))

    def _sync_speed(self, speed: float) -> None:
        try:
            selected = self.SPEEDS.index(speed)
        except ValueError:
            selected = self.SPEEDS.index(1.0)
        if self.speed.get_selected() == selected:
            return
        was_updating = self._updating_speed
        self._updating_speed = True
        try:
            self.speed.set_selected(selected)
        finally:
            self._updating_speed = was_updating

    def _render(self, state: Any) -> None:
        phase = str(getattr(state, "phase", "ready")).lower().split(".")[-1]
        status_key = {
            "preparing": "preparing",
            "ready": "ready",
            "playing": "playing",
            "paused": "paused",
            "completed": "completed",
            "error": "playback_error",
        }.get(phase, "ready")
        error_message = str(getattr(state, "error_message", "") or "").strip()
        if phase == "error" and error_message:
            status_text = self.parent_window.t(
                "playback_error_detail", detail=error_message
            )
        elif bool(getattr(state, "speed_change_failed", False)):
            status_text = self.parent_window.t(
                "speed_change_error", detail=error_message
            )
        else:
            status_text = self.parent_window.t(status_key)
        self.status.set_status(status_text)
        playing = phase == "playing"
        self.play.set_label(self.parent_window.t("pause" if playing else "play"))
        interactive, timeline_seekable = playback_control_sensitivity(state)
        self.scale.set_sensitive(timeline_seekable)
        self.seek_back.set_sensitive(timeline_seekable)
        self.play.set_sensitive(interactive)
        self.seek_forward.set_sensitive(timeline_seekable)
        self.volume.set_sensitive(phase not in {"idle", "error"})
        self.speed.set_sensitive(interactive)
        self._sync_volume(float(getattr(state, "volume", 1.0)))
        self._sync_speed(float(getattr(state, "speed", 1.0) or 1.0))
        position_ms = int(getattr(state, "position_ms", 0) or 0)
        duration_ms = int(getattr(state, "duration_ms", 0) or 0)
        position = position_ms / 1000.0
        duration = duration_ms / 1000.0
        self._duration_ms = duration_ms
        progress = min(1000.0, position * 1000.0 / duration) if duration > 0 else 0.0
        self.scale.set_value(progress)
        text = self.parent_window.t("position", position=format_duration(position), duration=format_duration(duration))
        self.position.set_text(text)
        self.scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])

    def _closing(self, *_args: object) -> bool:
        self.player.close()
        self.parent_window.player_window_closed(self)
        return False


class GuidePage(Gtk.ScrolledWindow):
    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.set_focusable(False)
        self.window = window
        self.stations: list[Any] = []
        self._station_request = 0
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_focusable(False)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        self.set_child(body)
        body.append(heading(window.t("guide_heading")))
        body.append(description(window.t("guide_intro")))
        self.medium = Gtk.DropDown(model=Gtk.StringList.new([window.t("television"), window.t("radio")]))
        selected_medium = 1 if window.state.get("guide_medium", "television") == "radio" else 0
        self.medium.set_selected(selected_medium)
        self.medium.connect("notify::selected", self._medium_changed)
        body.append(labelled(f"_{window.t('medium')}", self.medium))
        station_expression = Gtk.PropertyExpression.new(
            Gtk.StringObject,
            None,
            "string",
        )
        self.station = Gtk.DropDown.new(None, station_expression)
        self.station.set_enable_search(True)
        self.station.set_search_match_mode(Gtk.StringFilterMatchMode.PREFIX)
        station_search_hint = window.t("station_search_hint")
        self.station.set_tooltip_text(station_search_hint)
        self.station.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [station_search_hint],
        )
        self.station.set_sensitive(False)
        body.append(labelled(f"_{window.t('station')}", self.station))
        self.date = Gtk.Entry(text=current_guide_date().isoformat(), activates_default=True)
        self.date.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        body.append(
            labelled(
                f"_{window.t('date')} ({window.t('date_format')})",
                self.date,
            )
        )
        self.show = wrapping_button(window.t("show_program"))
        self.show.add_css_class("suggested-action")
        self.show.set_receives_default(True)
        self.show.set_sensitive(False)
        self.show.connect("clicked", self._show)
        body.append(self.show)
        self.busy = BusyBlock()
        body.append(self.busy)
        self.status = LiveStatus()
        body.append(self.status)
        body.append(description(window.t("guide_source_note")))
        self.connect("map", self._mapped)
        self.connect("unmap", self._unmapped)

    def _mapped(self, *_args: object) -> None:
        self.window.set_default_widget(self.show)
        self._load_stations()

    def _unmapped(self, *_args: object) -> None:
        if self.window.get_default_widget() is self.show:
            self.window.set_default_widget(None)

    def _medium_value(self) -> str:
        return "radio" if self.medium.get_selected() == 1 else "television"

    def _medium_changed(self, *_args: object) -> None:
        value = self._medium_value()
        self.window.state.set("guide_medium", value)
        self._load_stations()

    def _load_stations(self) -> None:
        medium = self._medium_value()
        self._station_request += 1
        request = self._station_request
        self.station.set_sensitive(False)
        self.show.set_sensitive(False)
        set_invalid(self.station, False)
        self.status.set_status("", announce=False)
        self.busy.start(self.window.t("loading"))

        def loaded(stations: list[Any]) -> None:
            if request != self._station_request or medium != self._medium_value():
                return
            self.busy.stop()
            ordered_stations = order_guide_stations(
                stations,
                GuideMedium(medium),
            )
            self.stations = ordered_stations
            self.station.set_model(
                Gtk.StringList.new(
                    [station.name for station in ordered_stations]
                )
            )
            wanted_key = (
                "guide_radio_station_id"
                if medium == "radio"
                else "guide_television_station_id"
            )
            wanted = self.window.state.get(wanted_key)
            index = next(
                (
                    position
                    for position, station in enumerate(ordered_stations)
                    if station.id == wanted
                ),
                0,
            )
            self.station.set_selected(
                index if ordered_stations else Gtk.INVALID_LIST_POSITION
            )
            self.station.set_sensitive(True)
            self.show.set_sensitive(True)

        def failed(error: BaseException) -> None:
            if request != self._station_request or medium != self._medium_value():
                return
            self.busy.stop()
            self.stations = []
            self.station.set_model(Gtk.StringList.new([]))
            self.station.set_sensitive(True)
            self.show.set_sensitive(True)
            self.status.set_status(str(error))

        self.window.run_async(
            lambda: list(self.window.services.guide_stations(medium)),
            loaded,
            failed,
        )

    def _show(self, _button: Gtk.Button) -> None:
        try:
            selected_date = Date.fromisoformat(self.date.get_text().strip())
        except ValueError:
            set_invalid(self.date, True)
            set_invalid(self.station, False)
            self.status.set_status(self.window.t("invalid_date"))
            focus_later(self.date)
            return
        set_invalid(self.date, False)
        index = self.station.get_selected()
        if index >= len(self.stations):
            set_invalid(self.station, True)
            self.status.set_status(self.window.t("no_station"))
            focus_later(self.station)
            return
        set_invalid(self.station, False)
        self.status.set_status("", announce=False)
        selected_station = self.stations[index]
        medium = self._medium_value()
        key = "guide_radio_station_id" if medium == "radio" else "guide_television_station_id"
        self.window.state.set(key, selected_station.id)
        ProgramWindow(self.window, selected_station, selected_date).present()


CZECH_TELEVISION_STATION_IDS = frozenset(
    {"centrum:1", "centrum:2", "centrum:18", "centrum:24", "centrum:357", "centrum:358"}
)


def program_start_millis(entry: Any) -> int:
    return int(getattr(entry, "start_millis", 0) or 0)


def program_end_millis(entry: Any) -> int | None:
    value = getattr(entry, "end_millis", None)
    return int(value) if value is not None else None


def is_currently_airing(entry: Any, now_millis: int) -> bool:
    end = program_end_millis(entry)
    return end is not None and program_start_millis(entry) <= now_millis < end


def preferred_program_index(entries: Sequence[Any], now_millis: int) -> int:
    for index, entry in enumerate(entries):
        if is_currently_airing(entry, now_millis):
            return index
    for index, entry in enumerate(entries):
        if program_start_millis(entry) >= now_millis:
            return index
    return 0 if entries else -1


def next_program_boundary_delay_ms(
    entries: Sequence[Any],
    now_millis: int,
) -> int | None:
    boundaries = [
        value
        for entry in entries
        for value in (program_start_millis(entry), program_end_millis(entry))
        if value is not None and value > now_millis
    ]
    return min(boundaries) - now_millis + 250 if boundaries else None


def format_guide_date(value: Date, translator: Translator) -> str:
    return translator(
        "long_date",
        day=value.day,
        month=translator(f"month_{value.month}"),
        year=value.year,
    )


def has_unknown_audio_description(station: Any, entry: Any) -> bool:
    return (
        str(getattr(station, "id", "")) in CZECH_TELEVISION_STATION_IDS
        and not bool(getattr(entry, "audio_description", False))
        and not bool(getattr(entry, "audio_description_known", False))
    )


class ProgramWindow(FormWindow):
    def __init__(self, parent: MainWindow, station: Any, selected_date: Date) -> None:
        super().__init__(parent, parent.t("guide"), parent.t("back"), width=840, height=700)
        self.parent_window = parent
        self.station = station
        self.selected_date = selected_date
        self.entries: list[Any] = []
        self.program_rows: list[Gtk.Widget] = []
        self._load_request = 0
        self._refresh_source: int | None = None
        shown_date = format_guide_date(selected_date, parent.t)
        self.title_label = heading(
            parent.t("program_heading", station=station.name, date=shown_date)
        )
        self.content.append(self.title_label)
        self.busy = BusyBlock()
        self.content.append(self.busy)
        self.status = LiveStatus()
        self.content.append(self.status)
        self.list_box = navigable_list(parent.t("guide"))
        scroll = scrolled_content(self.list_box)
        scroll.set_vexpand(True)
        self.content.append(scroll)
        self.connect("map", self._start)
        self.connect("close-request", self._closing)
        self._started = False

    def _start(self, *_args: object) -> None:
        if self._started:
            return
        self._started = True
        self._load()

    def _load(self) -> None:
        self._load_request += 1
        request = self._load_request
        self._cancel_refresh()
        self.entries = []
        self.program_rows = []
        clear_list(self.list_box)
        self.status.set_status("", announce=False)
        self.busy.start(self.parent_window.t("guide_program_loading"))
        self.parent_window.run_async(
            lambda: list(self.parent_window.services.load_program(self.station, self.selected_date)),
            lambda entries: self._loaded(request, entries),
            lambda error: self._failed(request, error),
        )

    def _loaded(self, request: int, entries: list[Any]) -> None:
        if request != self._load_request:
            return
        self.busy.stop()
        self.entries = sorted(entries, key=program_start_millis)
        shown_date = format_guide_date(self.selected_date, self.parent_window.t)
        title = self.parent_window.t(
            "programs_count",
            station=self.station.name,
            date=shown_date,
            count=len(self.entries),
        )
        self.title_label.set_text(title)
        self.list_box.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.set_title(title)
        if not self.entries:
            self.status.set_status(self.parent_window.t("empty_program"))
            return
        self._render_programs(initial_focus=True)

    def _failed(self, request: int, _error: BaseException) -> None:
        if request != self._load_request:
            return
        self.busy.stop()
        message = self.parent_window.t("guide_program_error")
        self.status.set_status(message)
        dialog = Gtk.AlertDialog(
            message=self.parent_window.t("guide_error_title"),
            detail=message,
            modal=True,
        )
        dialog.set_buttons(
            [self.parent_window.t("retry"), self.parent_window.t("close")]
        )
        dialog.set_default_button(0)
        dialog.set_cancel_button(1)

        def finished(source: Gtk.AlertDialog, result: object) -> None:
            try:
                response = source.choose_finish(result)  # type: ignore[arg-type]
            except Exception:
                response = 1
            if response == 0:
                self._load()
            else:
                self.close()

        dialog.choose(self, None, finished)

    def _render_programs(self, *, initial_focus: bool) -> None:
        now_millis = int(time.time() * 1000)
        if len(self.program_rows) != len(self.entries):
            clear_list(self.list_box)
            self.program_rows = []
            for entry in self.entries:
                child = PresentationLabel(xalign=0, wrap=True)
                append_list_item(
                    self.list_box,
                    child,
                    label=program_title(entry, self.parent_window.t),
                    description=self.parent_window.t("show_program_details"),
                    callback=lambda entry=entry: self._open_program(entry),
                )
                self.program_rows.append(child)

        for row, entry in zip(
            self.program_rows,
            self.entries,
            strict=True,
        ):
            self._update_program_row(row, entry, now_millis)

        if initial_focus:
            index = preferred_program_index(self.entries, now_millis)
            self.title_label.announce(
                self.title_label.get_text(),
                Gtk.AccessibleAnnouncementPriority.MEDIUM,
            )
            if index >= 0:
                focus_list_item_later(self.list_box, index)
        self._schedule_refresh(now_millis)

    def _open_program(self, item: Any) -> None:
        ProgramDetailWindow(
            self.parent_window,
            item,
            audio_description_unknown=has_unknown_audio_description(
                self.station,
                item,
            ),
        ).present()

    def _update_program_row(
        self,
        child: Gtk.Widget,
        entry: Any,
        now_millis: int,
    ) -> None:
        title = program_title(entry, self.parent_window.t)
        start_text = _guide_time(entry, "start")
        end_text = _guide_time(entry, "end")
        time_range = start_text + (f"–{end_text}" if end_text else "")
        airing = is_currently_airing(entry, now_millis)
        audio_description = bool(getattr(entry, "audio_description", False))
        audio_unknown = has_unknown_audio_description(self.station, entry)

        visible_status = []
        if airing:
            visible_status.append(self.parent_window.t("now"))
        if audio_unknown:
            visible_status.append("AD?")
        main = (
            ("AD · " if audio_description else "")
            + f"{time_range} — {title}"
        )
        visible_label = (
            (" · ".join(visible_status) + "\n" if visible_status else "") + main
        )
        if isinstance(child, Gtk.Label):
            child.set_label(visible_label)

        spoken = [self.parent_window.t("program_item", time=time_range, title=title)]
        if airing:
            spoken.append(self.parent_window.t("now"))
        if audio_description:
            spoken.append(self.parent_window.t("audio_description"))
        if audio_unknown:
            spoken.append(self.parent_window.t("audio_unknown"))
        update_list_item(
            self.list_box,
            child,
            label=". ".join(spoken),
            description=self.parent_window.t("show_program_details"),
        )

    def _schedule_refresh(self, now_millis: int) -> None:
        self._cancel_refresh()
        delay = next_program_boundary_delay_ms(self.entries, now_millis)
        if delay is not None:
            self._refresh_source = GLib.timeout_add(max(1, delay), self._refresh)

    def _refresh(self) -> bool:
        self._refresh_source = None
        self._render_programs(initial_focus=False)
        return GLib.SOURCE_REMOVE

    def _cancel_refresh(self) -> None:
        if self._refresh_source is not None:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = None

    def _closing(self, *_args: object) -> bool:
        self._load_request += 1
        self._cancel_refresh()
        return False


def _guide_time(entry: Any, field: str) -> str:
    direct = getattr(entry, f"{field}_text", None)
    if direct:
        return str(direct)
    value = getattr(entry, f"{field}_at", None) or getattr(entry, f"{field}_datetime", None)
    if value is not None and hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    millis = getattr(entry, f"{field}_millis", None)
    if millis is not None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(float(millis) / 1000.0, ZoneInfo("Europe/Prague")).strftime("%H:%M")
    return ""


class ProgramDetailWindow(FormWindow):
    def __init__(
        self,
        parent: MainWindow,
        entry: Any,
        *,
        audio_description_unknown: bool = False,
    ) -> None:
        super().__init__(parent, parent.t("program_details"), parent.t("back"), height=560)
        self.content.append(heading(program_title(entry, parent.t)))
        times = "–".join(value for value in (_guide_time(entry, "start"), _guide_time(entry, "end")) if value)
        details: list[str] = []
        if times:
            details.append(times)
        ad = bool(getattr(entry, "audio_description", False))
        if ad:
            details.append(parent.t("audio_description"))
        elif audio_description_unknown:
            details.append(parent.t("audio_unknown"))
        details.append(
            getattr(entry, "description", "") or parent.t("description_missing")
        )
        self.details = readable_description("\n".join(details))
        self.content.append(self.details)
        program_url = getattr(entry, "program_url", None)
        archive_url = getattr(entry, "archive_url", None)
        if program_url:
            button = wrapping_button(parent.t("open_program"))
            button.connect("clicked", lambda _button: open_uri(self, program_url, parent.t))
            self.content.append(button)
        if archive_url and archive_url != program_url:
            button = wrapping_button(parent.t("open_archive"))
            button.connect("clicked", lambda _button: open_uri(self, archive_url, parent.t))
            self.content.append(button)


class SettingsPage(Gtk.ScrolledWindow):
    INTERVALS = (0, 1, 5, 10, 15, 30, 45, 60, 180, 360, 720)

    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.set_focusable(False)
        self.window = window
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_focusable(False)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        self.set_child(body)
        body.append(heading(window.t("settings_heading")))
        body.append(heading(window.t("common_settings"), level=2))
        languages = Gtk.StringList.new([window.t("language_system"), window.t("language_cs"), window.t("language_en")])
        self.language = Gtk.DropDown(model=languages)
        language_values = ("system", "cs", "en")
        try:
            self.language.set_selected(language_values.index(str(window.state.get("language", "system"))))
        except ValueError:
            self.language.set_selected(0)
        self.language.connect("notify::selected", self._language_changed)
        body.append(labelled(window.t("language_mnemonic"), self.language))
        filter_after = wrapping_check_button(window.t("filter_after"), active=bool(window.state.get("filter_after_list", False)))
        filter_after.connect("toggled", lambda button: self._set("filter_after_list", button.get_active()))
        body.append(filter_after)
        background_enabled = bool(
            window.state.get("background_checks_enabled", False)
        )
        self._background_updating = False
        self.background_switch = Gtk.Switch(
            active=background_enabled,
            halign=Gtk.Align.START,
        )
        self.background_switch.set_state(background_enabled)
        self.background_switch.connect("state-set", self._background_state_set)
        body.append(
            labelled(
                window.t("background_checks"),
                self.background_switch,
            )
        )
        self.background_description = description("")
        body.append(self.background_description)
        self.background_status = LiveStatus()
        body.append(self.background_status)
        self._update_background_description(background_enabled)
        body.append(heading(window.t("rss_settings"), level=2))
        show_articles = wrapping_check_button(window.t("show_article_dates"), active=bool(window.state.get("show_article_dates", False)))
        show_articles.connect("toggled", lambda button: self._set("show_article_dates", button.get_active()))
        body.append(show_articles)
        self._interval_control(
            body,
            "rss_check_interval_minutes",
            window.t("rss_interval_mnemonic"),
        )
        body.append(heading(window.t("podcast_settings"), level=2))
        show_episodes = wrapping_check_button(window.t("show_episode_dates"), active=bool(window.state.get("show_episode_dates", False)))
        show_episodes.connect("toggled", lambda button: self._set("show_episode_dates", button.get_active()))
        body.append(show_episodes)
        self._interval_control(
            body,
            "podcast_check_interval_minutes",
            window.t("podcast_interval_mnemonic"),
        )
        body.append(heading(window.t("about"), level=2))
        body.append(description(window.t("version", version=application_version(window.get_application()))))
        help_button = wrapping_button(window.t("help"))
        help_button.connect("clicked", lambda _button: alert(window, window.t("help"), window.t("help_text")))
        body.append(help_button)
        thank = wrapping_button(window.t("thank_author"))
        thank.connect("clicked", lambda _button: open_uri(window, THANK_AUTHOR_URL, window.t))
        body.append(thank)

    def _set(self, key: str, value: Any) -> None:
        self.window.state.set(key, value)
        self.window.preferences_changed(key)

    def _language_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        values = ("system", "cs", "en")
        selected = min(dropdown.get_selected(), len(values) - 1)
        chosen = values[selected]
        previous = str(self.window.state.get("language", "system"))
        if chosen == previous:
            return
        self.window.state.set("language", chosen)
        application = self.window.get_application()
        if application is None:
            return
        current_page = self.window.current_page()
        width, height = self.window.get_default_size()
        was_maximized = self.window.is_maximized()
        try:
            replacement = MainWindow(
                application,
                self.window.state,
                self.window.services,
            )
        except Exception as error:
            self.window.state.set("language", previous)
            alert(self.window, self.window.t("error"), str(error))
            return
        replacement.set_default_size(width, height)
        icon_name = self.window.get_icon_name()
        if icon_name:
            replacement.set_icon_name(icon_name)
        replacement.select_page(current_page)
        if was_maximized:
            replacement.maximize()
        if hasattr(application, "window"):
            application.window = replacement
        replacement.present()
        replacement.toast(replacement.t("language_changed"))
        focus_later(replacement.settings_page.language)
        self.window.close()

    def _background_state_set(
        self,
        switch: Gtk.Switch,
        requested: bool,
    ) -> bool:
        if self._background_updating:
            return False
        previous = bool(switch.get_state())
        if requested == previous:
            return True
        switch.set_sensitive(False)
        self.background_status.set_status(
            self.window.t(
                "background_checks_enabling"
                if requested
                else "background_checks_disabling"
            )
        )

        def work() -> bool:
            application = self.window.get_application()
            callback = getattr(application, "set_background_checks_enabled", None)
            if callback is None:
                raise RuntimeError(self.window.t("background_checks_api_missing"))
            callback(requested)
            return bool(
                self.window.state.get("background_checks_enabled", requested)
            )

        def succeeded(applied: bool) -> None:
            self._set_background_switch(applied)
            switch.set_sensitive(True)
            self._update_background_description(applied)
            self.background_status.set_status(
                self.window.t(
                    "background_checks_enabled_status"
                    if applied
                    else "background_checks_disabled_status"
                )
            )
            focus_later(switch)

        def failed(error: BaseException) -> None:
            restored = bool(
                self.window.state.get("background_checks_enabled", previous)
            )
            self._set_background_switch(restored)
            switch.set_sensitive(True)
            self._update_background_description(restored)
            message = self.window.t(
                "background_checks_error",
                detail=str(error),
            )
            self.background_status.set_status(message, announce=False)
            self.background_status.announce(
                message,
                Gtk.AccessibleAnnouncementPriority.HIGH,
            )
            focus_later(switch)

        self.window.run_async(work, succeeded, failed)
        return True

    def _set_background_switch(self, enabled: bool) -> None:
        self._background_updating = True
        try:
            self.background_switch.set_active(enabled)
            self.background_switch.set_state(enabled)
        finally:
            self._background_updating = False

    def refresh_background_checks_state(
        self,
        error: BaseException | None = None,
    ) -> None:
        """Reflect an asynchronous systemd sync without stealing keyboard focus."""

        enabled = bool(
            self.window.state.get("background_checks_enabled", False)
        )
        self._set_background_switch(enabled)
        self._update_background_description(enabled)
        if error is None:
            return
        message = self.window.t(
            "background_checks_error",
            detail=str(error),
        )
        self.background_status.set_status(message, announce=False)
        self.background_status.announce(
            message,
            Gtk.AccessibleAnnouncementPriority.HIGH,
        )

    def _update_background_description(self, enabled: bool) -> None:
        text = self.window.t(
            "background_checks_enabled_description"
            if enabled
            else "background_checks_disabled_description"
        )
        self.background_description.set_text(text)
        self.background_switch.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [self.window.t("background_checks"), text],
        )

    def _interval_label(self, minutes: int) -> str:
        if minutes == 0:
            return self.window.t("manual")
        if minutes == 60:
            return self.window.t("every_hour")
        if minutes > 60:
            return self.window.t("every_hours", hours=minutes // 60)
        return self.window.t("every_minutes", minutes=minutes)

    def _interval_control(self, body: Gtk.Box, key: str, title: str) -> None:
        dropdown = Gtk.DropDown(model=Gtk.StringList.new([self._interval_label(value) for value in self.INTERVALS]))
        current = int(self.window.state.get(key, 0))
        dropdown.set_selected(self.INTERVALS.index(current) if current in self.INTERVALS else 0)

        def changed(control: Gtk.DropDown, *_args: object) -> None:
            index = control.get_selected()
            if index < len(self.INTERVALS):
                self._set(key, self.INTERVALS[index])

        dropdown.connect("notify::selected", changed)
        body.append(labelled(title, dropdown))
