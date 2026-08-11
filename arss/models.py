"""Small immutable domain objects shared by every ARSS frontend."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeedSubscription:
    """A user-visible title and the address of an RSS, Atom, or podcast feed."""

    title: str
    url: str


@dataclass(frozen=True, slots=True)
class FeedArticle:
    """One RSS article or podcast episode.

    ``published_text`` preserves the publisher's original value for display,
    while ``published_at_millis`` is the parsed UTC instant used for sorting.
    Podcast-only fields remain ``None`` for ordinary articles.
    """

    title: str
    url: str
    source_id: str | None = None
    published_text: str | None = None
    published_at_millis: int | None = None
    media_url: str | None = None
    media_type: str | None = None
    duration_text: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    """A parsed feed title and its newest-first articles."""

    title: str
    articles: tuple[FeedArticle, ...]
