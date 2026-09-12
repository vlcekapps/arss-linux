"""Virtual All views, independent of GTK, subscription stores and monitoring."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import unicodedata

from .models import FeedArticle, FeedSubscription, ParsedFeed


SORT_DATE = "date"
SORT_SOURCE = "source"
SORT_VALUES = (SORT_DATE, SORT_SOURCE)


def offers_all(subscription_count: int) -> bool:
    return subscription_count >= 2


def sort_preference_key(kind: str) -> str:
    if kind not in {"rss", "podcast"}:
        raise ValueError(f"Unknown feed kind: {kind}")
    return f"{kind}_all_sort"


@dataclass(frozen=True, slots=True)
class SourcedArticle:
    source: FeedSubscription
    article: FeedArticle

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        # A publisher's entry ID is scoped to its feed, never globally unique.
        return (
            self.source.url,
            self.article.source_id or "",
            self.article.url,
            self.article.media_url or "",
            self.article.title,
        )


@dataclass(frozen=True, slots=True)
class AggregateResult:
    items: tuple[SourcedArticle, ...]
    loaded: tuple[tuple[FeedSubscription, ParsedFeed], ...]
    failed: tuple[FeedSubscription, ...]


def load_all(
    subscriptions: Sequence[FeedSubscription],
    fetch: Callable[[str], ParsedFeed],
    kind: str,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> AggregateResult:
    """Load on a worker, keeping successful sources after individual failures.

    Requests are sequential so the injected client need not share an HTTP
    session across threads. Closing the view prevents any further fetches.
    The parser and HTTP client retain their existing per-feed security limits.
    """

    sort_preference_key(kind)
    loaded: list[tuple[FeedSubscription, ParsedFeed]] = []
    failed: list[FeedSubscription] = []
    items: list[SourcedArticle] = []
    for source in tuple(subscriptions):
        if cancelled():
            break
        try:
            feed = fetch(source.url)
        except Exception:
            failed.append(source)
            continue
        loaded.append((source, feed))
        items.extend(
            SourcedArticle(source, article)
            for article in feed.articles
            if kind != "podcast" or bool((article.media_url or "").strip())
        )
    return AggregateResult(tuple(items), tuple(loaded), tuple(failed))


def source_name_key(name: str, language: str) -> tuple[tuple[int, ...], str]:
    """Stable case-insensitive alphabetic keys for ARSS's Czech/English UI.

    Do not call locale.setlocale: it is process-global and would race network
    workers or alter publisher date parsing. Czech has distinct č/ř/š/ž and ch
    after h; other accents are secondary. English treats accents secondarily.
    """

    normalized = unicodedata.normalize("NFC", name.strip().casefold())
    czech = language.lower().replace("_", "-").startswith("cs")
    alphabet = "a b c č d e f g h ch i j k l m n o p q r ř s š t u v w x y z ž".split()
    weights = {letter: 100 + index for index, letter in enumerate(alphabet)}
    primary: list[int] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if czech and normalized[index:index + 2] == "ch":
            character = "ch"
            index += 1
        elif not (czech and character in "čřšž"):
            character = "".join(
                part for part in unicodedata.normalize("NFD", character)
                if not unicodedata.combining(part)
            )
        if character == "ch":
            primary.append(weights["ch"])
        else:
            primary.extend(weights.get(part, 1000 + ord(part)) for part in character)
        index += 1
    return tuple(primary), normalized


def order_all(
    items: Iterable[SourcedArticle], sort: str = SORT_DATE, language: str = "en"
) -> tuple[SourcedArticle, ...]:
    """Newest first, or alphabetic source groups and newest within each group."""

    def key(entry: SourcedArticle) -> tuple:
        timestamp = entry.article.published_at_millis
        date = (timestamp is None, -(timestamp or 0))
        source = (source_name_key(entry.source.title, language), entry.source.url)
        tie = (source_name_key(entry.article.title, language), entry.identity)
        return (source, date, tie) if sort == SORT_SOURCE else (date, source, tie)

    return tuple(sorted(items, key=key))
