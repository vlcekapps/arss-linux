"""In-process RSS and podcast update monitoring.

This module contains no GTK code and performs no work merely by being
imported.  :class:`FeedMonitor` owns the synchronous, testable feed pass;
:class:`GLibMonitorScheduler` optionally runs those passes in worker threads
only while the desktop application is alive.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import threading
from typing import Final, Protocol

from .models import FeedArticle, FeedSubscription, ParsedFeed


MAX_IDS_PER_FEED: Final = 256
MAXIMUM_INLINE_IDENTITY_CHARS: Final = 512
_UNIT_SEPARATOR: Final = "\x1f"


class MonitorKind(StrEnum):
    """Independently stored and scheduled families of subscriptions."""

    RSS = "rss"
    PODCAST = "podcast"


FeedCheckKind = MonitorKind


@dataclass(frozen=True, slots=True)
class ArticleIdentifiers:
    """Current stable identity plus aliases used by older ARSS releases."""

    primary: str
    legacy_aliases: frozenset[str] = frozenset()


def article_identifiers(article: FeedArticle) -> ArticleIdentifiers:
    """Build a stable, bounded notification identity for one article."""

    source_id = _non_blank(article.source_id)
    article_url = _non_blank(article.url)
    media_url = _non_blank(article.media_url)
    text_identity = "text:" + _sha256(
        article.title + _UNIT_SEPARATOR + (article.published_text or "")
    )

    if source_id is not None:
        primary = _bounded_identity("id", source_id)
    elif article_url is not None:
        primary = _bounded_identity("url", article_url)
    elif media_url is not None:
        primary = _bounded_identity("media", media_url)
    else:
        primary = text_identity

    # ARSS 1.6.3 preferred an enclosure URL to the article URL.  Retaining
    # exactly that old value as an alias prevents an upgrade notification storm.
    if source_id is not None:
        legacy = f"id:{source_id}"
    elif media_url is not None:
        legacy = f"media:{media_url}"
    elif article_url is not None:
        legacy = f"url:{article_url}"
    else:
        legacy = text_identity
    aliases = frozenset() if legacy == primary else frozenset({legacy})
    return ArticleIdentifiers(primary=primary, legacy_aliases=aliases)


def article_identity(article: FeedArticle) -> str:
    return article_identifiers(article).primary


class CheckpointEdge(StrEnum):
    """Learned edge at which a long feed inserts genuinely new entries."""

    UNKNOWN = "unknown"
    START = "start"
    END = "end"


@dataclass(frozen=True, slots=True)
class CheckpointState:
    """One bounded per-feed baseline stored by a checkpoint backend."""

    ids: tuple[str, ...]
    edge: CheckpointEdge = CheckpointEdge.UNKNOWN
    complete: bool = True

    def __post_init__(self) -> None:
        if len(self.ids) > MAX_IDS_PER_FEED:
            raise ValueError(f"A checkpoint may contain at most {MAX_IDS_PER_FEED} IDs")


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    """Pure result of comparing a successful fetch with its prior baseline."""

    new_ids: tuple[str, ...]
    retained_ids: tuple[str, ...]
    edge: CheckpointEdge
    complete: bool

    @property
    def state(self) -> CheckpointState:
        return CheckpointState(self.retained_ids, self.edge, self.complete)


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Committed decision returned to the monitor."""

    is_baseline: bool
    new_ids: tuple[str, ...]


LegacyAliases = Mapping[str, Collection[str]]


def evaluate_checkpoint(
    *,
    has_baseline: bool,
    previous_ids: Iterable[str],
    current_ids: Iterable[str],
    legacy_aliases_by_id: LegacyAliases | None = None,
    previous_edge: CheckpointEdge = CheckpointEdge.UNKNOWN,
    previous_complete: bool | None = None,
) -> CheckpointDecision:
    """Compare identities without producing false discoveries for long feeds.

    A feed with more than 256 entries retains an even first/last sample.  Until
    the insertion edge can be inferred unambiguously, unknown middle entries
    are learned silently instead of being announced as new.
    """

    aliases = legacy_aliases_by_id or {}
    previous = _unique_non_blank(previous_ids)
    previous_set = set(previous)
    if previous_complete is None:
        previous_complete = len(previous) < MAX_IDS_PER_FEED

    current = _unique_non_blank(current_ids)
    current_complete = len(current) <= MAX_IDS_PER_FEED
    if current_complete:
        tracked = current
    else:
        first_half = MAX_IDS_PER_FEED // 2
        tracked = current[:first_half] + current[-(MAX_IDS_PER_FEED - first_half) :]

    def was_seen(identity: str) -> bool:
        return identity in previous_set or any(
            alias in previous_set for alias in aliases.get(identity, ())
        )

    normalized_previous_edge = CheckpointEdge(previous_edge)
    next_edge = normalized_previous_edge
    new_ids: tuple[str, ...]
    if not has_baseline:
        next_edge = CheckpointEdge.UNKNOWN
        new_ids = ()
    elif previous_complete and current_complete:
        new_ids = tuple(identity for identity in current if not was_seen(identity))
    else:
        known_indices = [index for index, identity in enumerate(current) if was_seen(identity)]
        if not known_indices:
            next_edge = CheckpointEdge.UNKNOWN
            new_ids = ()
        else:
            first_known = known_indices[0]
            last_known = known_indices[-1]
            prefix = tuple(
                identity for identity in current[:first_known] if not was_seen(identity)
            )
            suffix = tuple(
                identity for identity in current[last_known + 1 :] if not was_seen(identity)
            )
            if normalized_previous_edge is CheckpointEdge.START:
                new_ids = prefix
            elif normalized_previous_edge is CheckpointEdge.END:
                new_ids = suffix
            elif prefix and not suffix:
                next_edge = CheckpointEdge.START
                new_ids = prefix
            elif suffix and not prefix:
                next_edge = CheckpointEdge.END
                new_ids = suffix
            else:
                new_ids = ()

    migrated_aliases = {
        alias
        for identity in tracked
        for alias in aliases.get(identity, ())
    }
    retained: list[str] = []
    retained_set: set[str] = set()
    for identity in tracked:
        if identity not in retained_set and len(retained) < MAX_IDS_PER_FEED:
            retained.append(identity)
            retained_set.add(identity)
    for identity in previous:
        if len(retained) >= MAX_IDS_PER_FEED:
            break
        if identity not in retained_set and identity not in migrated_aliases:
            retained.append(identity)
            retained_set.add(identity)

    return CheckpointDecision(
        new_ids=new_ids,
        retained_ids=tuple(retained),
        edge=next_edge,
        complete=current_complete,
    )


class CheckpointBackend(Protocol):
    """Persistence boundary for bounded checkpoint state."""

    def load(
        self,
        kind: MonitorKind,
        feed_url: str,
    ) -> CheckpointState | None: ...

    def save(
        self,
        kind: MonitorKind,
        feed_url: str,
        state: CheckpointState,
    ) -> None: ...


class MemoryCheckpointBackend:
    """Thread-safe backend useful for one application session and unit tests."""

    def __init__(self) -> None:
        self._states: dict[tuple[MonitorKind, str], CheckpointState] = {}
        self._lock = threading.RLock()

    def load(self, kind: MonitorKind, feed_url: str) -> CheckpointState | None:
        with self._lock:
            return self._states.get((MonitorKind(kind), feed_url.strip()))

    def save(
        self,
        kind: MonitorKind,
        feed_url: str,
        state: CheckpointState,
    ) -> None:
        with self._lock:
            self._states[(MonitorKind(kind), feed_url.strip())] = state

    def snapshot(self) -> Mapping[tuple[MonitorKind, str], CheckpointState]:
        with self._lock:
            return dict(self._states)


class CheckpointStore:
    """Atomic policy facade over a pluggable persistence backend."""

    def __init__(self, backend: CheckpointBackend | None = None) -> None:
        self.backend = backend or MemoryCheckpointBackend()
        self._lock = threading.RLock()

    def record_success(
        self,
        kind: MonitorKind,
        feed_url: str,
        article_ids: Iterable[str],
        legacy_aliases_by_id: LegacyAliases | None = None,
    ) -> CheckpointResult:
        normalized_kind = MonitorKind(kind)
        normalized_url = feed_url.strip()
        current_ids = tuple(article_ids)
        with self._lock:
            atomic_record = getattr(self.backend, "record_success_atomic", None)
            if callable(atomic_record):
                return atomic_record(
                    normalized_kind,
                    normalized_url,
                    current_ids,
                    legacy_aliases_by_id,
                )
            previous = self.backend.load(normalized_kind, normalized_url)
            decision = evaluate_checkpoint(
                has_baseline=previous is not None,
                previous_ids=previous.ids if previous is not None else (),
                current_ids=current_ids,
                legacy_aliases_by_id=legacy_aliases_by_id,
                previous_edge=previous.edge
                if previous is not None
                else CheckpointEdge.UNKNOWN,
                previous_complete=previous.complete if previous is not None else True,
            )
            self.backend.save(normalized_kind, normalized_url, decision.state)
        return CheckpointResult(
            is_baseline=previous is None,
            new_ids=decision.new_ids,
        )

    # Longer name documents the important semantic: failed fetches never advance.
    record_successful_fetch = record_success


class FeedClientProtocol(Protocol):
    def fetch(self, url: str) -> ParsedFeed: ...


class SubscriptionStoreProtocol(Protocol):
    def load(self) -> tuple[FeedSubscription, ...]: ...


class MonitorRunGuardProtocol(Protocol):
    """Optional cross-process lease used by GUI and headless schedulers."""

    def try_acquire(self, kind: MonitorKind) -> bool: ...

    def release(self, kind: MonitorKind) -> None: ...


@dataclass(frozen=True, slots=True)
class NewFeedItem:
    kind: MonitorKind
    subscription: FeedSubscription
    article: FeedArticle
    stable_id: str


NotificationCallback = Callable[[MonitorKind, tuple[NewFeedItem, ...]], None]
MonitorErrorCallback = Callable[
    [MonitorKind, FeedSubscription | None, BaseException],
    None,
]


class FeedMonitor:
    """Perform independent, best-effort RSS and podcast passes.

    ``run_once`` is synchronous and intended for a worker thread.  RSS and
    podcast runs have separate non-blocking guards, so one slow family cannot
    prevent the other from running.  Callbacks execute on that same worker and
    intentionally make no assumptions about GTK.
    """

    def __init__(
        self,
        client: FeedClientProtocol,
        rss_store: SubscriptionStoreProtocol,
        podcast_store: SubscriptionStoreProtocol,
        *,
        checkpoints: CheckpointStore | None = None,
        notification_callback: NotificationCallback | None = None,
        error_callback: MonitorErrorCallback | None = None,
        run_guard: MonitorRunGuardProtocol | None = None,
    ) -> None:
        self.client = client
        self.rss_store = rss_store
        self.podcast_store = podcast_store
        self.checkpoints = checkpoints or CheckpointStore()
        self.notification_callback = notification_callback
        self.error_callback = error_callback
        self.run_guard = run_guard
        self._run_locks = {
            MonitorKind.RSS: threading.Lock(),
            MonitorKind.PODCAST: threading.Lock(),
        }
        self._cancelled = threading.Event()

    def run_once(self, kind: MonitorKind) -> tuple[NewFeedItem, ...]:
        normalized_kind = MonitorKind(kind)
        if self._cancelled.is_set():
            return ()
        run_lock = self._run_locks[normalized_kind]
        if not run_lock.acquire(blocking=False):
            return ()
        guard_acquired = False
        try:
            if self.run_guard is not None:
                try:
                    guard_acquired = self.run_guard.try_acquire(normalized_kind)
                except Exception as error:
                    self._report_error(normalized_kind, None, error)
                    return ()
                if not guard_acquired:
                    return ()
            try:
                subscriptions = self._store_for(normalized_kind).load()
            except Exception as error:
                self._report_error(normalized_kind, None, error)
                return ()

            discoveries: list[NewFeedItem] = []
            for subscription in subscriptions:
                if self._cancelled.is_set():
                    break
                try:
                    parsed = self.client.fetch(subscription.url)
                    if self._cancelled.is_set():
                        break
                    eligible = (
                        parsed.articles
                        if normalized_kind is MonitorKind.RSS
                        else tuple(
                            article
                            for article in parsed.articles
                            if _non_blank(article.media_url) is not None
                        )
                    )
                    articles_by_id: dict[str, FeedArticle] = {}
                    aliases_by_id: dict[str, set[str]] = {}
                    for article in eligible:
                        identifiers = article_identifiers(article)
                        articles_by_id.setdefault(identifiers.primary, article)
                        if identifiers.legacy_aliases:
                            aliases_by_id.setdefault(identifiers.primary, set()).update(
                                identifiers.legacy_aliases
                            )
                    checkpoint = self.checkpoints.record_success(
                        normalized_kind,
                        subscription.url,
                        articles_by_id,
                        aliases_by_id,
                    )
                    if checkpoint.is_baseline:
                        continue
                    for stable_id in checkpoint.new_ids:
                        article = articles_by_id.get(stable_id)
                        if article is not None:
                            discoveries.append(
                                NewFeedItem(
                                    normalized_kind,
                                    subscription,
                                    article,
                                    stable_id,
                                )
                            )
                except Exception as error:
                    if not self._cancelled.is_set():
                        self._report_error(normalized_kind, subscription, error)

            result = tuple(discoveries)
            if (
                result
                and not self._cancelled.is_set()
                and self.notification_callback is not None
            ):
                try:
                    self.notification_callback(normalized_kind, result)
                except Exception as error:
                    self._report_error(normalized_kind, None, error)
            return result
        finally:
            if guard_acquired and self.run_guard is not None:
                try:
                    self.run_guard.release(normalized_kind)
                except Exception as error:
                    self._report_error(normalized_kind, None, error)
            run_lock.release()

    def run_rss(self) -> tuple[NewFeedItem, ...]:
        return self.run_once(MonitorKind.RSS)

    def run_podcasts(self) -> tuple[NewFeedItem, ...]:
        return self.run_once(MonitorKind.PODCAST)

    def is_running(self, kind: MonitorKind) -> bool:
        run_lock = self._run_locks[MonitorKind(kind)]
        acquired = run_lock.acquire(blocking=False)
        if acquired:
            run_lock.release()
        return not acquired

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Stop after any currently blocking fetch returns."""

        self._cancelled.set()
        cancel_client = getattr(self.client, "cancel", None)
        if cancel_client is not None:
            try:
                cancel_client()
            except Exception:
                pass

    def resume(self) -> None:
        self._cancelled.clear()
        resume_client = getattr(self.client, "resume", None)
        if resume_client is not None:
            try:
                resume_client()
            except Exception:
                pass

    def _store_for(self, kind: MonitorKind) -> SubscriptionStoreProtocol:
        return self.rss_store if kind is MonitorKind.RSS else self.podcast_store

    def _report_error(
        self,
        kind: MonitorKind,
        subscription: FeedSubscription | None,
        error: BaseException,
    ) -> None:
        if self.error_callback is None:
            return
        try:
            self.error_callback(kind, subscription, error)
        except Exception:
            pass


IntervalProvider = Callable[[MonitorKind], int] | Mapping[MonitorKind | str, int]


class GLibMonitorScheduler:
    """Schedule monitor passes while, and only while, the process is running.

    GLib timers live in the application's main loop.  Feed work is submitted to
    a two-thread executor so RSS and podcasts stay independent and never block
    GTK.  Stopping removes every GLib source; no OS daemon, alarm, or autostart
    job is installed.
    """

    def __init__(
        self,
        monitor: FeedMonitor,
        interval_minutes: IntervalProvider,
        *,
        glib: object | None = None,
        executor: Executor | None = None,
        run_immediately: bool = False,
    ) -> None:
        if glib is None:
            try:
                import gi

                gi.require_version("GLib", "2.0")
                from gi.repository import GLib
            except (ImportError, ValueError) as error:
                raise RuntimeError("GLib Python bindings are unavailable") from error
            glib = GLib
        self.monitor = monitor
        self._interval_minutes = interval_minutes
        self._glib = glib
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="arss-feed-monitor",
        )
        self._owns_executor = executor is None
        self._run_immediately = run_immediately
        self._sources: dict[MonitorKind, object] = {}
        self._futures: dict[MonitorKind, Future[tuple[NewFeedItem, ...]] | object] = {}
        self._lock = threading.RLock()
        self._running = False
        self._closed = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def scheduled_kinds(self) -> frozenset[MonitorKind]:
        with self._lock:
            return frozenset(self._sources)

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Monitor scheduler is closed")
            if self._running:
                return
            resume = getattr(self.monitor, "resume", None)
            if resume is not None:
                resume()
            self._running = True
            self._reschedule_locked()
            immediate_kinds = tuple(self._sources)
        if self._run_immediately:
            for kind in immediate_kinds:
                self.run_now(kind)

    def refresh(self) -> None:
        """Apply changed intervals without restarting the application."""

        with self._lock:
            if self._closed or not self._running:
                return
            self._reschedule_locked()

    # Name used by settings-oriented callers.
    reschedule = refresh

    def run_now(self, kind: MonitorKind) -> bool:
        """Submit one family unless it is stopped or already in flight."""

        normalized_kind = MonitorKind(kind)
        with self._lock:
            if self._closed or not self._running:
                return False
            previous = self._futures.get(normalized_kind)
            if previous is not None and not _future_done(previous):
                return False
            try:
                future = self._executor.submit(self.monitor.run_once, normalized_kind)
            except Exception:
                return False
            self._futures[normalized_kind] = future
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            cancel = getattr(self.monitor, "cancel", None)
            if cancel is not None:
                cancel()
            self._remove_sources_locked()
            futures = tuple(self._futures.values())
            self._futures.clear()
        for future in futures:
            try:
                future.cancel()  # type: ignore[attr-defined]
            except (AttributeError, RuntimeError):
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self.stop()
        with self._lock:
            self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _reschedule_locked(self) -> None:
        self._remove_sources_locked()
        for kind in MonitorKind:
            minutes = self._interval_for(kind)
            if minutes <= 0:
                continue
            seconds = minutes * 60
            source = self._glib.timeout_add_seconds(  # type: ignore[attr-defined]
                seconds,
                self._on_timeout,
                kind,
            )
            self._sources[kind] = source

    def _remove_sources_locked(self) -> None:
        sources = tuple(self._sources.values())
        self._sources.clear()
        for source in sources:
            try:
                self._glib.source_remove(source)  # type: ignore[attr-defined]
            except (AttributeError, TypeError, ValueError):
                pass

    def _on_timeout(self, kind: MonitorKind) -> bool:
        with self._lock:
            if not self._running or self._closed:
                return False
        self.run_now(kind)
        return True

    def _interval_for(self, kind: MonitorKind) -> int:
        try:
            if callable(self._interval_minutes):
                value = self._interval_minutes(kind)
            else:
                value = self._interval_minutes.get(
                    kind,
                    self._interval_minutes.get(kind.value, 0),
                )
        except Exception:
            return 0
        return value if type(value) is int and value > 0 else 0


def _future_done(future: object) -> bool:
    try:
        return bool(future.done())  # type: ignore[attr-defined]
    except Exception:
        return True


def _bounded_identity(kind: str, value: str) -> str:
    if len(value) <= MAXIMUM_INLINE_IDENTITY_CHARS:
        return f"{kind}:{value}"
    return f"{kind}-hash:{_sha256(value)}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _non_blank(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _unique_non_blank(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
