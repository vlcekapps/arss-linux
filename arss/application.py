"""GApplication wiring for the Linux ARSS frontend."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from datetime import datetime
import os
from pathlib import Path
import threading
from typing import Any, Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

from . import __version__
from .background import (
    BACKGROUND_CHECKS_ENABLED,
    BackgroundMonitorManager,
    FileMonitorRunLock,
)
from .checkpoints import JsonCheckpointBackend
from .directory import PodcastDirectoryClient, RssDirectory
from .feed import FeedClient
from .guide import GuideDate, GuideMedium, GuideRepository
from .gtk_helpers import (
    LiveStatus,
    alert,
    heading,
    readable_description,
    scrolled_content,
    wrapping_button,
)
from .models import FeedArticle, FeedSubscription, ParsedFeed
from .monitor import (
    CheckpointStore,
    FeedMonitor,
    GLibMonitorScheduler,
    MonitorKind,
    NewFeedItem,
    article_identifiers,
)
from .notifications import GNotificationPublisher
from .storage import (
    DEFAULT_PREFERENCES,
    FeedStore,
    JsonPreferences,
    PodcastStore,
    StorageError,
    XdgPaths,
)
from .i18n import Translator
from .ui import MainWindow, open_uri, valid_web_url


APPLICATION_ID = "cz.pvlcek.arss"


def data_file(name: str) -> Path:
    return Path(__file__).resolve().parent / "data" / name


class DesktopState:
    """Small adapter exposing both stores and preferences to GTK."""

    def __init__(self, paths: XdgPaths | None = None) -> None:
        self.paths = paths or XdgPaths.from_environment()
        self.preferences = JsonPreferences(paths=self.paths)
        self.rss = FeedStore(paths=self.paths, preferences=self.preferences)
        self.podcasts = PodcastStore(paths=self.paths)

    def subscriptions(self, kind: str) -> list[FeedSubscription]:
        if kind == "rss":
            return list(self.rss.load())
        if kind == "podcast":
            return list(self.podcasts.load())
        raise ValueError(f"Unknown subscription kind: {kind}")

    def save_subscriptions(self, kind: str, items: Sequence[FeedSubscription]) -> None:
        if kind == "rss":
            self.rss.save(items)
        elif kind == "podcast":
            self.podcasts.save(items)
        else:
            raise ValueError(f"Unknown subscription kind: {kind}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.preferences.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.preferences.set(key, value)

    def back_up_and_reset_preferences(self) -> Path:
        """Preserve a broken settings file, then create validated defaults."""

        source = self.preferences.path
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = source.with_name(f"{source.name}.corrupt-{timestamp}")
        counter = 1
        while backup.exists():
            backup = source.with_name(f"{source.name}.corrupt-{timestamp}-{counter}")
            counter += 1
        if source.exists():
            os.replace(source, backup)
        self.preferences.save(DEFAULT_PREFERENCES)
        return backup


class DesktopServices:
    """Desktop implementations of network, guide, audio and directory ports."""

    def __init__(self, checkpoints: CheckpointStore | None = None) -> None:
        Gst.init(None)
        self.feed_client = FeedClient()
        self.rss_directory = RssDirectory(data_file("rss_directory.opml"))
        self.podcast_directory = PodcastDirectoryClient()
        self.guide = GuideRepository()
        self.monitor_feed_client = FeedClient()
        self.checkpoints = checkpoints or CheckpointStore(JsonCheckpointBackend())

    def fetch_feed(self, url: str) -> ParsedFeed:
        return self.feed_client.fetch(url)

    def record_feed_seen(
        self,
        kind: str,
        feed_url: str,
        articles: Sequence[FeedArticle],
    ) -> None:
        """Advance notification state after a successful user-requested load."""

        identities = tuple(article_identifiers(article) for article in articles)
        aliases = {
            identity.primary: identity.legacy_aliases
            for identity in identities
            if identity.legacy_aliases
        }
        self.checkpoints.record_successful_fetch(
            MonitorKind(kind),
            feed_url,
            (identity.primary for identity in identities),
            aliases,
        )

    def search_directory(self, kind: str, query: str) -> Sequence[Any]:
        if kind == "rss":
            return self.rss_directory.search(query)
        if kind == "podcast":
            return self.podcast_directory.search(query)
        raise ValueError(f"Unknown directory kind: {kind}")

    def guide_stations(self, medium: str) -> Sequence[Any]:
        return self.guide.refresh_stations(GuideMedium(medium))

    def load_program(self, station: Any, selected_date: Date) -> Sequence[Any]:
        guide_date = GuideDate(selected_date.year, selected_date.month, selected_date.day)
        return self.guide.load_program(station, guide_date)

    def create_player(self, callback: Callable[[Any], None]) -> Any:
        from .playback import PodcastPlayer

        return PodcastPlayer(callback)

    def close(self) -> None:
        self.feed_client.close()
        self.monitor_feed_client.close()
        self.podcast_directory.close()
        self.guide.close()


class ArssApplication(Adw.Application):
    version = __version__

    def __init__(self, *, smoke_test: bool = False) -> None:
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="arss")
        self.shutdown_event = threading.Event()
        self.state = DesktopState()
        self.background_monitor_manager = BackgroundMonitorManager(
            self.state.preferences
        )
        self.checkpoints = CheckpointStore(JsonCheckpointBackend())
        self.services = DesktopServices(self.checkpoints)
        self.window: MainWindow | None = None
        self.smoke_test = smoke_test
        self.smoke_succeeded = False
        self._preferences_validated = False
        self.recovery_window: PreferencesRecoveryWindow | None = None
        self.monitor_run_lock = FileMonitorRunLock()
        self.monitor = FeedMonitor(
            self.services.monitor_feed_client,
            self.state.rss,
            self.state.podcasts,
            checkpoints=self.checkpoints,
            notification_callback=self._monitor_notification,
            run_guard=self.monitor_run_lock,
        )
        self.monitor_scheduler = GLibMonitorScheduler(
            self.monitor,
            self._monitor_interval,
            executor=self.executor,
        )
        self._monitor_started = False
        self._background_sync_lock = threading.Lock()
        self._background_sync_in_flight = False
        self._background_sync_pending = False
        self._background_sync_error: BaseException | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        show_kind = Gio.SimpleAction.new("show-kind", GLib.VariantType.new("s"))
        show_kind.connect("activate", self._show_kind)
        self.add_action(show_kind)
        open_article = Gio.SimpleAction.new(
            "open-article", GLib.VariantType.new("s")
        )
        open_article.connect("activate", self._open_article)
        self.add_action(open_article)
        play_episode = Gio.SimpleAction.new(
            "play-episode", GLib.VariantType.new("(ssss)")
        )
        play_episode.connect("activate", self._play_episode)
        self.add_action(play_episode)
        provider = Gtk.CssProvider()
        css = data_file("style.css")
        if css.is_file():
            provider.load_from_path(str(css))
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )

    def do_activate(self) -> None:
        if not self._preferences_validated:
            try:
                self.state.preferences.load()
            except StorageError as error:
                if self.recovery_window is None:
                    self.recovery_window = PreferencesRecoveryWindow(self, error)
                self.recovery_window.present()
                return
            self._preferences_validated = True
        if self.window is None:
            self.window = MainWindow(self, self.state, self.services)
            self.window.set_icon_name(APPLICATION_ID)
        self.window.present()
        self.smoke_succeeded = True
        if not self._monitor_started:
            if self.state.get(BACKGROUND_CHECKS_ENABLED, False) is True:
                self._request_background_sync()
            self._monitor_started = True
            self.monitor_scheduler.start()
            self._run_enabled_monitors()
        if self.smoke_test:
            GLib.timeout_add(1200, self._finish_smoke_test)

    def _finish_smoke_test(self) -> bool:
        if self.window is not None:
            self.window.close()
        self.quit()
        return GLib.SOURCE_REMOVE

    def preferences_changed(self, key: str | None = None) -> None:
        """Reschedule only the monitor family whose interval changed."""

        kinds_by_key = {
            "rss_check_interval_minutes": MonitorKind.RSS,
            "podcast_check_interval_minutes": MonitorKind.PODCAST,
        }
        if key is not None and key not in kinds_by_key:
            return
        if self.state.get(BACKGROUND_CHECKS_ENABLED, False) is True:
            # systemctl has a 30-second timeout. Never run it from the GTK
            # callback which persists an interval change.
            self._request_background_sync()
        self.monitor_scheduler.refresh()
        changed_kind = kinds_by_key.get(key)
        if changed_kind is None:
            # Compatibility for callers which do not yet identify the key.
            self._run_enabled_monitors()
        elif self._monitor_interval(changed_kind) > 0:
            self.monitor_scheduler.run_now(changed_kind)

    def _request_background_sync(self) -> None:
        """Coalesce systemd refreshes in the worker pool without blocking GTK."""

        with self._background_sync_lock:
            if self.shutdown_event.is_set():
                return
            if self._background_sync_in_flight:
                self._background_sync_pending = True
                return
            self._background_sync_in_flight = True
        self._submit_background_sync()

    def _submit_background_sync(self) -> None:
        try:
            future = self.executor.submit(self.background_monitor_manager.sync)
        except RuntimeError as error:
            with self._background_sync_lock:
                final_error = self._background_sync_error or error
                self._background_sync_in_flight = False
                self._background_sync_pending = False
                self._background_sync_error = None
            if not self.shutdown_event.is_set():
                GLib.idle_add(self._finish_background_sync, final_error)
            return
        future.add_done_callback(self._background_sync_done)

    def _background_sync_done(self, future: object) -> None:
        # The manager resets the opt-in preference on failure. Refreshing on
        # the main context then immediately restores foreground scheduling.
        error: BaseException | None = None
        try:
            future.result()  # type: ignore[attr-defined]
        except BaseException as caught:
            error = caught
        with self._background_sync_lock:
            if error is not None and self._background_sync_error is None:
                self._background_sync_error = error
            repeat = (
                self._background_sync_pending
                and not self.shutdown_event.is_set()
            )
            self._background_sync_pending = False
            final_error = None
            if not repeat:
                self._background_sync_in_flight = False
                final_error = self._background_sync_error
                self._background_sync_error = None
        if repeat:
            self._submit_background_sync()
        elif not self.shutdown_event.is_set():
            GLib.idle_add(self._finish_background_sync, final_error)

    def _finish_background_sync(
        self,
        error: BaseException | None = None,
    ) -> bool:
        if not self.shutdown_event.is_set():
            self.monitor_scheduler.refresh()
            self._run_enabled_monitors()
            settings = getattr(self.window, "settings_page", None)
            refresh = getattr(settings, "refresh_background_checks_state", None)
            if refresh is not None:
                refresh(error)
        return GLib.SOURCE_REMOVE

    def set_background_checks_enabled(self, enabled: bool) -> None:
        """Apply the explicit opt-in, rolling back safely on systemd errors."""

        try:
            self.background_monitor_manager.set_enabled(enabled)
        finally:
            # On failure the manager has already reset the preference to false,
            # so refreshing here resumes checks while the GUI remains open.
            self.monitor_scheduler.refresh()
            self._run_enabled_monitors()

    def _monitor_interval(self, kind: MonitorKind) -> int:
        if self.state.get(BACKGROUND_CHECKS_ENABLED, False) is True:
            return 0
        key = (
            "rss_check_interval_minutes"
            if kind is MonitorKind.RSS
            else "podcast_check_interval_minutes"
        )
        value = self.state.get(key, 0)
        return value if type(value) is int and value > 0 else 0

    def _run_enabled_monitors(self) -> None:
        for kind in MonitorKind:
            if self._monitor_interval(kind) > 0:
                self.monitor_scheduler.run_now(kind)

    def _monitor_notification(
        self,
        kind: MonitorKind,
        items: tuple[NewFeedItem, ...],
    ) -> None:
        if self.shutdown_event.is_set():
            return
        GLib.idle_add(self._deliver_notification, kind, items)

    def _deliver_notification(
        self,
        kind: MonitorKind,
        items: tuple[NewFeedItem, ...],
    ) -> bool:
        if self.shutdown_event.is_set() or not items:
            return GLib.SOURCE_REMOVE
        translator = self.window.t if self.window is not None else None
        if translator is None:
            from .i18n import Translator

            translator = Translator(str(self.state.get("language", "system")))
        publisher = GNotificationPublisher(self)
        publisher.publish_feed_updates(kind, items, translator)
        return GLib.SOURCE_REMOVE

    def _show_kind(self, _action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        self.activate()
        if self.window is None:
            return
        page = parameter.get_string()
        self.window.select_page(page)
        self.window.present()

    def _open_article(
        self,
        _action: Gio.SimpleAction,
        parameter: GLib.Variant,
    ) -> None:
        self.activate()
        if self.window is not None:
            open_uri(self.window, parameter.get_string(), self.window.t)

    def _play_episode(
        self,
        _action: Gio.SimpleAction,
        parameter: GLib.Variant,
    ) -> None:
        self.activate()
        if self.window is None:
            return
        title, article_url, media_url, duration_text = parameter.unpack()
        if not valid_web_url(media_url):
            alert(
                self.window,
                self.window.t("error"),
                self.window.t("invalid_address"),
            )
            return
        episode = FeedArticle(
            title=title,
            url=article_url,
            media_url=media_url,
            duration_text=duration_text or None,
        )
        self.window.open_player(episode)

    def recover_preferences(self, status: LiveStatus) -> None:
        translator = Translator("system")
        try:
            self.state.back_up_and_reset_preferences()
        except Exception as error:
            detail = translator("recovery_failed", detail=str(error))
            status.set_status(detail)
            status.announce(detail, Gtk.AccessibleAnnouncementPriority.HIGH)
            return
        if self.recovery_window is not None:
            self.recovery_window.close()
            self.recovery_window = None
        self._preferences_validated = False
        self.activate()

    def do_shutdown(self) -> None:
        self.shutdown_event.set()
        self.monitor_scheduler.close()
        self.services.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        Adw.Application.do_shutdown(self)


class PreferencesRecoveryWindow(Adw.ApplicationWindow):
    """Explicit, recoverable response to corrupt preferences."""

    def __init__(self, application: ArssApplication, error: BaseException) -> None:
        super().__init__(application=application)
        translator = Translator("system")
        self.set_title(translator("preferences_broken"))
        self.set_default_size(620, 360)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(24)
        body.set_margin_bottom(24)
        body.set_margin_start(24)
        body.set_margin_end(24)
        body.append(heading(translator("preferences_broken")))
        detail = readable_description(
            f"{translator('preferences_broken_detail')}\n\n{error}"
        )
        body.append(detail)
        self.status = LiveStatus()
        body.append(self.status)
        buttons = Adw.WrapBox(
            child_spacing=8,
            line_spacing=8,
            wrap_policy=Adw.WrapPolicy.NATURAL,
        )
        buttons.set_focusable(False)
        buttons.set_halign(Gtk.Align.START)
        self.action_box = buttons
        close = wrapping_button(translator("close"))
        close.connect("clicked", lambda _button: self.close())
        reset = wrapping_button(translator("backup_reset"))
        reset.add_css_class("destructive-action")
        reset.connect(
            "clicked",
            lambda _button: application.recover_preferences(self.status),
        )
        buttons.append(close)
        buttons.append(reset)
        body.append(buttons)
        scroll = scrolled_content(body)
        scroll.set_vexpand(True)
        self.set_content(scroll)
