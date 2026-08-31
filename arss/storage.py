"""XDG-aware, atomic persistence for subscriptions and preferences."""

from __future__ import annotations

from dataclasses import dataclass
import json
import locale
from collections.abc import Iterable, Mapping
import os
from pathlib import Path
import tempfile
import threading
from typing import Final

from .contract import load_embedded_contract
from .feed import is_supported_web_url
from .models import FeedSubscription
from .opml import OpmlError, read_opml, write_opml


APPLICATION_DIRECTORY_NAME: Final = "arss"
RSS_FILE_NAME: Final = "readFeeds.opml"
PODCAST_FILE_NAME: Final = "podcasts.opml"
PREFERENCES_FILE_NAME: Final = "preferences.json"
MAXIMUM_PREFERENCES_BYTES: Final = 1024 * 1024

LANGUAGE_SYSTEM: Final = "system"
LANGUAGE_CZECH: Final = "cs"
LANGUAGE_ENGLISH: Final = "en"
SUPPORTED_LANGUAGES: Final = frozenset(
    {LANGUAGE_SYSTEM, LANGUAGE_CZECH, LANGUAGE_ENGLISH}
)

GUIDE_MEDIUM_TELEVISION: Final = "television"
GUIDE_MEDIUM_RADIO: Final = "radio"
SUPPORTED_GUIDE_MEDIA: Final = frozenset(
    {GUIDE_MEDIUM_TELEVISION, GUIDE_MEDIUM_RADIO}
)
DEFAULT_TELEVISION_STATION_ID: Final = "tv.ct1"
DEFAULT_RADIO_STATION_ID: Final = "radio.radiozurnal"

CHECK_MANUALLY: Final = 0
SUPPORTED_CHECK_INTERVAL_MINUTES: Final = (
    CHECK_MANUALLY,
    1,
    5,
    10,
    15,
    30,
    45,
    60,
    180,
    360,
    720,
)

OBSOLETE_PREFERENCE_KEYS: Final = frozenset(
    {"rss_notification_sound", "podcast_notification_sound"}
)

DEFAULT_PREFERENCES: Final[dict[str, object]] = {
    "rss_store_initialized": False,
    "default_feed_url": None,
    "show_article_dates": False,
    "filter_after_list": False,
    "show_episode_dates": False,
    "guide_medium": GUIDE_MEDIUM_TELEVISION,
    "guide_television_station_id": DEFAULT_TELEVISION_STATION_ID,
    "guide_radio_station_id": DEFAULT_RADIO_STATION_ID,
    "language": LANGUAGE_SYSTEM,
    "background_checks_enabled": False,
    "rss_check_interval_minutes": CHECK_MANUALLY,
    "podcast_check_interval_minutes": CHECK_MANUALLY,
}

def _contract_initial_feeds(locale_code: str) -> tuple[FeedSubscription, ...]:
    return tuple(
        FeedSubscription(feed.title, feed.url)
        for feed in load_embedded_contract().default_feeds_by_locale[locale_code]
    )


CZECH_INITIAL_FEEDS: Final = _contract_initial_feeds(LANGUAGE_CZECH)
ENGLISH_INITIAL_FEEDS: Final = _contract_initial_feeds(LANGUAGE_ENGLISH)

_STORAGE_LOCK = threading.RLock()


class StorageError(Exception):
    """Base class for durable storage failures."""


class StorageReadError(StorageError):
    """Persistent data exists but could not be read safely."""


class StorageWriteError(StorageError):
    """Persistent data could not be atomically replaced."""


class PreferencesError(StorageError):
    """Preferences are corrupt or violate their schema."""


@dataclass(frozen=True, slots=True)
class XdgPaths:
    """Resolved application directories below XDG data and config homes."""

    data_dir: Path
    config_dir: Path

    @property
    def rss_opml(self) -> Path:
        return self.data_dir / RSS_FILE_NAME

    @property
    def podcasts_opml(self) -> Path:
        return self.data_dir / PODCAST_FILE_NAME

    @property
    def preferences_json(self) -> Path:
        return self.config_dir / PREFERENCES_FILE_NAME

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        application_directory_name: str = APPLICATION_DIRECTORY_NAME,
    ) -> XdgPaths:
        """Resolve XDG paths, ignoring relative XDG overrides per the spec."""

        if not application_directory_name or "/" in application_directory_name:
            raise ValueError("application_directory_name must be one path component")
        values = os.environ if environment is None else environment
        resolved_home = (Path.home() if home is None else Path(home)).expanduser()
        data_home = _absolute_xdg_path(
            values.get("XDG_DATA_HOME"), resolved_home / ".local" / "share"
        )
        config_home = _absolute_xdg_path(
            values.get("XDG_CONFIG_HOME"), resolved_home / ".config"
        )
        return cls(
            data_dir=data_home / application_directory_name,
            config_dir=config_home / application_directory_name,
        )


class JsonPreferences:
    """Typed preferences persisted as one atomically replaced UTF-8 JSON file."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        paths: XdgPaths | None = None,
    ) -> None:
        if path is not None and paths is not None:
            raise ValueError("Supply path or paths, not both")
        resolved_paths = paths or XdgPaths.from_environment()
        self.path = Path(path) if path is not None else resolved_paths.preferences_json

    def load(self) -> dict[str, object]:
        """Return defaults merged with stored keys without mutating the file."""

        with _STORAGE_LOCK:
            try:
                payload = self.path.read_bytes()
            except FileNotFoundError:
                return dict(DEFAULT_PREFERENCES)
            except OSError as error:
                raise StorageReadError(f"Could not read {self.path}") from error
            if len(payload) > MAXIMUM_PREFERENCES_BYTES:
                raise PreferencesError("Preferences file exceeds its byte limit")
            try:
                decoded = json.loads(
                    payload.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
                raise PreferencesError("Preferences file is not valid UTF-8 JSON") from error
            if not isinstance(decoded, dict) or not all(
                isinstance(key, str) for key in decoded
            ):
                raise PreferencesError("Preferences root must be a JSON object")
            merged = dict(DEFAULT_PREFERENCES)
            merged.update(decoded)
            # Match Android's defensive getters: a syntactically valid but
            # unsupported persisted value falls back instead of preventing all
            # future preference updates.
            for key, fallback in DEFAULT_PREFERENCES.items():
                try:
                    _validate_preference(key, merged[key])
                except PreferencesError:
                    merged[key] = fallback
            for key in OBSOLETE_PREFERENCE_KEYS:
                merged.pop(key, None)
            return merged

    def save(self, values: Mapping[str, object]) -> None:
        """Validate and atomically replace the complete preference object."""

        snapshot = dict(values)
        for key, value in snapshot.items():
            _validate_preference(key, value)
        try:
            payload = (
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PreferencesError("Preferences contain a non-JSON value") from error
        if len(payload) > MAXIMUM_PREFERENCES_BYTES:
            raise PreferencesError("Preferences file exceeds its byte limit")
        with _STORAGE_LOCK:
            _atomic_write(self.path, payload)

    def get(self, key: str, default: object | None = None) -> object | None:
        return self.load().get(key, default)

    def set(self, key: str, value: object) -> None:
        self.update({key: value})

    def update(self, values: Mapping[str, object] | None = None, **changes: object) -> None:
        """Apply several settings in one atomic commit."""

        requested = dict(values or {})
        requested.update(changes)
        for key, value in requested.items():
            _validate_preference(key, value)
        with _STORAGE_LOCK:
            current = self.load()
            current.update(requested)
            self.save(current)

    @property
    def rss_store_initialized(self) -> bool:
        return _preference_bool(self.load(), "rss_store_initialized", False)

    @rss_store_initialized.setter
    def rss_store_initialized(self, value: bool) -> None:
        self.set("rss_store_initialized", value)

    @property
    def default_feed_url(self) -> str | None:
        value = self.load().get("default_feed_url")
        return value if isinstance(value, str) and is_supported_web_url(value) else None

    @default_feed_url.setter
    def default_feed_url(self, value: str | None) -> None:
        self.set("default_feed_url", value)

    @property
    def show_article_dates(self) -> bool:
        return _preference_bool(self.load(), "show_article_dates", False)

    @show_article_dates.setter
    def show_article_dates(self, value: bool) -> None:
        self.set("show_article_dates", value)

    @property
    def filter_after_list(self) -> bool:
        return _preference_bool(self.load(), "filter_after_list", False)

    @filter_after_list.setter
    def filter_after_list(self, value: bool) -> None:
        self.set("filter_after_list", value)

    @property
    def show_episode_dates(self) -> bool:
        return _preference_bool(self.load(), "show_episode_dates", False)

    @show_episode_dates.setter
    def show_episode_dates(self, value: bool) -> None:
        self.set("show_episode_dates", value)

    @property
    def guide_medium(self) -> str:
        return _supported_string(
            self.load().get("guide_medium"),
            SUPPORTED_GUIDE_MEDIA,
            GUIDE_MEDIUM_TELEVISION,
        )

    @guide_medium.setter
    def guide_medium(self, value: str) -> None:
        self.set("guide_medium", value)

    @property
    def guide_television_station_id(self) -> str:
        return _non_blank_string(
            self.load().get("guide_television_station_id"),
            DEFAULT_TELEVISION_STATION_ID,
        )

    @guide_television_station_id.setter
    def guide_television_station_id(self, value: str) -> None:
        self.set("guide_television_station_id", value)

    @property
    def guide_radio_station_id(self) -> str:
        return _non_blank_string(
            self.load().get("guide_radio_station_id"),
            DEFAULT_RADIO_STATION_ID,
        )

    @guide_radio_station_id.setter
    def guide_radio_station_id(self, value: str) -> None:
        self.set("guide_radio_station_id", value)

    @property
    def language(self) -> str:
        return _supported_string(
            self.load().get("language"), SUPPORTED_LANGUAGES, LANGUAGE_SYSTEM
        )

    @language.setter
    def language(self, value: str) -> None:
        self.set("language", value)

    @property
    def rss_check_interval_minutes(self) -> int:
        return _supported_int(
            self.load().get("rss_check_interval_minutes"),
            SUPPORTED_CHECK_INTERVAL_MINUTES,
            CHECK_MANUALLY,
        )

    @rss_check_interval_minutes.setter
    def rss_check_interval_minutes(self, value: int) -> None:
        self.set("rss_check_interval_minutes", value)

    @property
    def background_checks_enabled(self) -> bool:
        return _preference_bool(self.load(), "background_checks_enabled", False)

    @background_checks_enabled.setter
    def background_checks_enabled(self, value: bool) -> None:
        self.set("background_checks_enabled", value)

    @property
    def podcast_check_interval_minutes(self) -> int:
        return _supported_int(
            self.load().get("podcast_check_interval_minutes"),
            SUPPORTED_CHECK_INTERVAL_MINUTES,
            CHECK_MANUALLY,
        )

    @podcast_check_interval_minutes.setter
    def podcast_check_interval_minutes(self, value: int) -> None:
        self.set("podcast_check_interval_minutes", value)


class SubscriptionStore:
    """An atomically replaced flat OPML subscription store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[FeedSubscription, ...]:
        with _STORAGE_LOCK:
            try:
                payload = self.path.read_bytes()
            except FileNotFoundError:
                return ()
            except OSError as error:
                raise StorageReadError(f"Could not read {self.path}") from error
            try:
                return read_opml(payload)
            except OpmlError as error:
                raise StorageReadError(f"Could not parse {self.path}") from error

    def save(self, subscriptions: Iterable[FeedSubscription]) -> None:
        snapshot = tuple(subscriptions)
        try:
            payload = write_opml(snapshot)
        except OpmlError as error:
            raise StorageWriteError(f"Could not serialize {self.path}") from error
        with _STORAGE_LOCK:
            _atomic_write(self.path, payload)


class FeedStore(SubscriptionStore):
    """RSS store with locale-sensitive, exactly-once initial seeding."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        paths: XdgPaths | None = None,
        preferences: JsonPreferences | None = None,
        system_language: str | None = None,
    ) -> None:
        if path is not None and paths is not None:
            raise ValueError("Supply path or paths, not both")
        resolved_paths = paths or XdgPaths.from_environment()
        super().__init__(Path(path) if path is not None else resolved_paths.rss_opml)
        self.preferences = preferences or (
            JsonPreferences(self.path.parent / PREFERENCES_FILE_NAME)
            if path is not None
            else JsonPreferences(paths=resolved_paths)
        )
        self.system_language = system_language

    def load(self) -> tuple[FeedSubscription, ...]:
        with _STORAGE_LOCK:
            if self.path.exists():
                subscriptions = super().load()
                if not self.preferences.rss_store_initialized:
                    self.preferences.rss_store_initialized = True
                return subscriptions

            if self.preferences.rss_store_initialized:
                super().save(())
                self.preferences.default_feed_url = None
                return ()

            initial = initial_rss_feeds(
                self.preferences.language,
                self.system_language,
            )
            super().save(initial)
            self.preferences.update(
                rss_store_initialized=True,
                default_feed_url=initial[0].url,
            )
            return initial

    def save(self, subscriptions: Iterable[FeedSubscription]) -> None:
        snapshot = tuple(subscriptions)
        with _STORAGE_LOCK:
            super().save(snapshot)
            if not self.preferences.rss_store_initialized:
                self.preferences.rss_store_initialized = True

    @property
    def default_feed_url(self) -> str | None:
        return self.preferences.default_feed_url

    @default_feed_url.setter
    def default_feed_url(self, value: str | None) -> None:
        self.preferences.default_feed_url = value


class PodcastStore(SubscriptionStore):
    """Unseeded podcast subscription store."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        paths: XdgPaths | None = None,
    ) -> None:
        if path is not None and paths is not None:
            raise ValueError("Supply path or paths, not both")
        resolved_paths = paths or XdgPaths.from_environment()
        super().__init__(
            Path(path) if path is not None else resolved_paths.podcasts_opml
        )


def initial_rss_feeds(
    language: str = LANGUAGE_SYSTEM,
    system_language: str | None = None,
) -> tuple[FeedSubscription, ...]:
    """Select the same five one-time defaults as Android ARSS."""

    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported application language: {language}")
    effective = _language_code(
        _detected_system_language() if language == LANGUAGE_SYSTEM and system_language is None
        else system_language if language == LANGUAGE_SYSTEM
        else language
    )
    return CZECH_INITIAL_FEEDS if effective == LANGUAGE_CZECH else ENGLISH_INITIAL_FEEDS


def _atomic_write(path: Path, payload: bytes) -> None:
    """Durably replace ``path`` using a same-directory temporary file."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
    except OSError as error:
        raise StorageWriteError(f"Could not prepare atomic write for {path}") from error

    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageWriteError(f"Could not atomically replace {path}") from error


def _absolute_xdg_path(value: str | None, fallback: Path) -> Path:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return fallback


def _validate_preference(key: str, value: object) -> None:
    if key in OBSOLETE_PREFERENCE_KEYS:
        raise PreferencesError(f"{key} is obsolete")
    boolean_keys = {
        "rss_store_initialized",
        "show_article_dates",
        "filter_after_list",
        "show_episode_dates",
        "background_checks_enabled",
    }
    if key in boolean_keys:
        if type(value) is not bool:
            raise PreferencesError(f"{key} must be a boolean")
        return
    if key == "default_feed_url":
        if value is not None and (
            not isinstance(value, str) or not is_supported_web_url(value)
        ):
            raise PreferencesError("default_feed_url must be an HTTP(S) URL or null")
        return
    if key == "language":
        if not isinstance(value, str) or value not in SUPPORTED_LANGUAGES:
            raise PreferencesError("language is unsupported")
        return
    if key == "guide_medium":
        if not isinstance(value, str) or value not in SUPPORTED_GUIDE_MEDIA:
            raise PreferencesError("guide_medium is unsupported")
        return
    if key in {"guide_television_station_id", "guide_radio_station_id"}:
        if not isinstance(value, str) or not value.strip():
            raise PreferencesError(f"{key} must not be blank")
        return
    if key in {"rss_check_interval_minutes", "podcast_check_interval_minutes"}:
        if type(value) is not int or value not in SUPPORTED_CHECK_INTERVAL_MINUTES:
            raise PreferencesError(f"{key} is unsupported")
        return
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PreferencesError(f"{key} is not a JSON value") from error


def _preference_bool(values: Mapping[str, object], key: str, fallback: bool) -> bool:
    value = values.get(key)
    return value if type(value) is bool else fallback


def _supported_string(
    value: object,
    supported: frozenset[str],
    fallback: str,
) -> str:
    return value if isinstance(value, str) and value in supported else fallback


def _supported_int(value: object, supported: tuple[int, ...], fallback: int) -> int:
    return value if type(value) is int and value in supported else fallback


def _non_blank_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _detected_system_language() -> str:
    detected = locale.getlocale()[0]
    if detected:
        return detected
    return os.environ.get("LANG", "")


def _language_code(value: str | None) -> str:
    if not value:
        return ""
    return value.split(".", 1)[0].replace("-", "_").split("_", 1)[0].lower()


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Non-standard JSON constant is prohibited: {value}")
