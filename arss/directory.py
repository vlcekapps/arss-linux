"""Platform-independent RSS and podcast directory services.

The module deliberately has no GUI dependencies.  It mirrors the bounded,
privacy-conscious directory behaviour of the Android application and can be
used from GTK, tests, or command-line tools.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import locale as system_locale
import os
from pathlib import Path
import posixpath
import re
import threading
import time
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urldefrag, urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests

from .contract import load_embedded_contract
from .search import normalize_search_text, search_text_matches


MAXIMUM_RESULTS = 60
MAXIMUM_PROVIDER_RESULTS = 200
MAXIMUM_APPLE_QUERY_VARIANTS = 4
ALL_QUERY_TERMS_MATCH_SCORE = 10_000
DEFAULT_DIRECTORY_PATH = load_embedded_contract().rss_directory_path

_MULTIPLE_SPACES = re.compile(r"\s+")
_TWO_LETTER_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One feed offered by a local or remote directory."""

    title: str
    url: str
    detail: str = ""


class RssDirectory:
    """Search the bundled OPML catalog without network access."""

    def __init__(
        self,
        path: str | Path = DEFAULT_DIRECTORY_PATH,
        *,
        maximum_results: int = MAXIMUM_RESULTS,
    ) -> None:
        if maximum_results <= 0:
            raise ValueError("maximum_results must be positive")
        self.path = Path(path)
        self.maximum_results = maximum_results
        self._entries: tuple[DirectoryEntry, ...] | None = None
        self._lock = threading.Lock()

    def entries(self) -> tuple[DirectoryEntry, ...]:
        with self._lock:
            if self._entries is None:
                self._entries = tuple(_read_rss_directory(self.path))
            return self._entries

    def search(self, query: str) -> list[DirectoryEntry]:
        if not normalize_search_text(query):
            return []
        found: list[DirectoryEntry] = []
        known_urls: set[str] = set()
        for entry in self.entries():
            if not search_text_matches(query, entry.title, entry.url):
                continue
            if entry.url in known_urls:
                continue
            known_urls.add(entry.url)
            found.append(entry)
            if len(found) >= self.maximum_results:
                break
        return found


def search_rss_directory(
    query: str,
    path: str | Path = DEFAULT_DIRECTORY_PATH,
    *,
    maximum_results: int = MAXIMUM_RESULTS,
) -> list[DirectoryEntry]:
    return RssDirectory(path, maximum_results=maximum_results).search(query)


def _read_rss_directory(path: Path) -> Iterable[DirectoryEntry]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exception:
        raise DirectoryInvalidResponseError(
            f"The RSS directory could not be read: {path}"
        ) from exception
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if not url:
            continue
        title = (
            outline.get("title")
            or outline.get("text")
            or urlsplit(url).hostname
            or url
        ).strip()
        yield DirectoryEntry(title=title, url=url)


class PodcastDirectoryError(OSError):
    """Base class for recoverable remote-directory failures."""


class DirectoryInvalidUrlError(PodcastDirectoryError):
    pass


class DirectoryNetworkError(PodcastDirectoryError):
    pass


class DirectoryHttpError(PodcastDirectoryError):
    def __init__(self, status_code: int, reason: str = "") -> None:
        self.status_code = status_code
        suffix = f": {reason}" if reason.strip() else ""
        super().__init__(f"The podcast directory returned HTTP {status_code}{suffix}.")


class DirectoryRedirectError(PodcastDirectoryError):
    pass


class DirectoryTooLargeError(PodcastDirectoryError):
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        super().__init__(
            f"The podcast directory response exceeded {maximum_bytes} bytes."
        )


class DirectoryInvalidResponseError(PodcastDirectoryError):
    pass


class PodcastDirectorySource(Protocol):
    def search(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> list[DirectoryEntry]: ...


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    created_at: float
    entries: tuple[DirectoryEntry, ...]


class HttpPodcastDirectorySource:
    """Bounded JSON client with explicit redirects and a small process cache."""

    _cache: OrderedDict[str, _CachedResponse] = OrderedDict()
    _cache_lock = threading.Lock()

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 15.0,
        maximum_response_bytes: int = 2 * 1024 * 1024,
        maximum_redirects: int = 5,
        cache_responses: bool = True,
        cache_lifetime: float = 120.0,
        cache_maximum_entries: int = 32,
        response_parser: Callable[[str], list[DirectoryEntry]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum_response_bytes must be positive")
        if maximum_redirects < 0:
            raise ValueError("maximum_redirects must not be negative")
        if cache_lifetime <= 0 or cache_maximum_entries <= 0:
            raise ValueError("cache limits must be positive")
        self.session = session or requests.Session()
        self._owns_session = session is None
        self._cancelled = threading.Event()
        self._response_lock = threading.RLock()
        self._active_responses: list[object] = []
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_redirects = maximum_redirects
        self.cache_responses = cache_responses
        self.cache_lifetime = cache_lifetime
        self.cache_maximum_entries = cache_maximum_entries
        self.response_parser = response_parser or parse_podcast_directory_response
        self.clock = clock

    @classmethod
    def clear_process_cache_for_tests(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()

    def search(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> list[DirectoryEntry]:
        self._require_not_cancelled()
        initial = _require_web_url(url)
        if self.cache_responses:
            cached = self._cached(initial)
            if cached is not None:
                return list(cached)

        current = initial
        visited: set[str] = set()
        redirects = 0
        while True:
            self._require_not_cancelled()
            identity = urldefrag(current)[0]
            if identity in visited:
                raise DirectoryRedirectError(
                    "The podcast directory created a redirect loop."
                )
            visited.add(identity)
            try:
                request_headers = {
                    "Accept": "application/json",
                    "User-Agent": "ARSS/Linux (podcast directory)",
                }
                if headers:
                    request_headers.update(headers)
                response = self.session.get(
                    current,
                    headers=request_headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exception:
                raise DirectoryNetworkError(
                    "The podcast directory could not be downloaded."
                ) from exception

            self._register_response(response)
            try:
                self._require_not_cancelled()
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location", "").strip()
                    if not location or redirects >= self.maximum_redirects:
                        raise DirectoryRedirectError(
                            "The podcast directory returned an unsafe redirect."
                        )
                    target = _require_web_url(urljoin(current, location))
                    if urlsplit(current).scheme.casefold() == "https" and (
                        urlsplit(target).scheme.casefold() == "http"
                    ):
                        raise DirectoryRedirectError(
                            "The podcast directory attempted an HTTPS downgrade."
                        )
                    if headers and _url_origin(target) != _url_origin(initial):
                        raise DirectoryRedirectError(
                            "The authenticated podcast directory attempted a cross-origin redirect."
                        )
                    current = target
                    redirects += 1
                    continue
                if not 200 <= response.status_code <= 299:
                    raise DirectoryHttpError(response.status_code, response.reason or "")
                declared = response.headers.get("Content-Length", "").strip()
                if declared.isdigit() and int(declared) > self.maximum_response_bytes:
                    raise DirectoryTooLargeError(self.maximum_response_bytes)
                body = _read_limited_response(
                    response,
                    self.maximum_response_bytes,
                    self._cancelled,
                )
                try:
                    entries = self.response_parser(body.decode("utf-8-sig"))
                except PodcastDirectoryError:
                    raise
                except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exception:
                    raise DirectoryInvalidResponseError(
                        "The podcast directory response is invalid."
                    ) from exception
            finally:
                response.close()
                self._unregister_response(response)

            if self.cache_responses:
                self._store(initial, entries)
            return list(entries)

    def cancel(self) -> None:
        """Interrupt streamed responses as far as the requests backend permits."""

        self._cancelled.set()
        with self._response_lock:
            responses = tuple(self._active_responses)
        for response in responses:
            try:
                response.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def close(self) -> None:
        self.cancel()
        if self._owns_session:
            self.session.close()

    def _require_not_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise DirectoryNetworkError("The podcast directory request was cancelled.")

    def _register_response(self, response: object) -> None:
        with self._response_lock:
            self._active_responses.append(response)

    def _unregister_response(self, response: object) -> None:
        with self._response_lock:
            try:
                self._active_responses.remove(response)
            except ValueError:
                pass

    def _cached(self, key: str) -> tuple[DirectoryEntry, ...] | None:
        now = self.clock()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            if now - cached.created_at > self.cache_lifetime:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return cached.entries

    def _store(self, key: str, entries: Iterable[DirectoryEntry]) -> None:
        cached = _CachedResponse(self.clock(), tuple(entries))
        with self._cache_lock:
            self._cache[key] = cached
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_maximum_entries:
                self._cache.popitem(last=False)


def _read_limited_response(
    response: requests.Response,
    maximum_bytes: int,
    cancelled: threading.Event | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if cancelled is not None and cancelled.is_set():
                raise DirectoryNetworkError("The podcast directory request was cancelled.")
            if not chunk:
                continue
            total += len(chunk)
            if total > maximum_bytes:
                raise DirectoryTooLargeError(maximum_bytes)
            chunks.append(chunk)
    except requests.RequestException as exception:
        raise DirectoryNetworkError(
            "The podcast directory could not be downloaded."
        ) from exception
    return b"".join(chunks)


class PodcastIndexDirectoryClient:
    """Search Podcast Index and Apple Podcasts, matching the Android sources.

    Podcast Index keeps an Apple-compatible public search endpoint which does
    not require credentials. When the user supplies a key and secret, prefer
    the richer signed v1 endpoint instead; Apple remains an independent
    regional fallback in both cases.
    """

    PODCAST_INDEX_PUBLIC_SEARCH_URL = "https://api.podcastindex.org/search"
    PODCAST_INDEX_SEARCH_URL = "https://api.podcastindex.org/api/1.0/search/byterm"
    APPLE_SEARCH_URL = "https://itunes.apple.com/search"

    def __init__(
        self,
        source: PodcastDirectorySource | Callable[[str], list[DirectoryEntry]] | None = None,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source = source or HttpPodcastDirectorySource()
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("PODCAST_INDEX_KEY", "")
        ).strip()
        self.api_secret = (
            api_secret
            if api_secret is not None
            else os.environ.get("PODCAST_INDEX_SECRET", "")
        ).strip()
        self.clock = clock

    @property
    def podcast_index_enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def close(self) -> None:
        close = getattr(self.source, "close", None)
        if close is not None:
            close()

    def search(self, query: str, locale_name: str | None = None) -> list[DirectoryEntry]:
        trimmed_query = _MULTIPLE_SPACES.sub(" ", query.strip())
        if not trimmed_query:
            raise ValueError("query must not be blank")

        collected: OrderedDict[str, DirectoryEntry] = OrderedDict()
        failures: list[Exception] = []

        def collect(url: str, headers: Mapping[str, str] | None = None) -> bool:
            try:
                source = self.source
                found = (
                    source(url)
                    if callable(source)
                    else source.search(url, headers=headers)
                )
            except Exception as exception:
                failures.append(exception)
                return False
            for entry in found:
                key = podcast_feed_deduplication_key(entry.url)
                existing = collected.get(key)
                score = podcast_relevance_score(entry, trimmed_query)
                existing_score = (
                    podcast_relevance_score(existing, trimmed_query)
                    if existing is not None
                    else None
                )
                if (
                    existing is None
                    or score > existing_score  # type: ignore[operator]
                    or (
                        score == existing_score
                        and entry.url.casefold().startswith("https://")
                        and not existing.url.casefold().startswith("https://")
                    )
                ):
                    collected[key] = entry
            return True

        if self.podcast_index_enabled:
            collect(
                self._podcast_index_url(trimmed_query),
                self._podcast_index_headers(),
            )
        else:
            collect(self._podcast_index_public_url(trimmed_query))
        country = podcast_storefront_country(locale_name)
        apple_failure: Exception | None = None
        for variant in podcast_search_variants(trimmed_query):
            if not collect(self._apple_url(variant, country)):
                apple_failure = failures[-1]
                break

        if apple_failure is not None and not any(
            podcast_relevance_score(entry, trimmed_query)
            >= ALL_QUERY_TERMS_MATCH_SCORE
            for entry in collected.values()
        ):
            raise apple_failure
        if not collected and failures:
            raise failures[0]
        return rank_podcast_entries(collected.values(), trimmed_query)[:MAXIMUM_RESULTS]

    def _podcast_index_url(self, query: str) -> str:
        arguments = {"q": query, "max": str(MAXIMUM_PROVIDER_RESULTS)}
        return f"{self.PODCAST_INDEX_SEARCH_URL}?{urlencode(arguments, quote_via=quote)}"

    def _podcast_index_public_url(self, query: str) -> str:
        arguments = {"term": query}
        return (
            f"{self.PODCAST_INDEX_PUBLIC_SEARCH_URL}?"
            f"{urlencode(arguments, quote_via=quote)}"
        )

    def _podcast_index_headers(self) -> dict[str, str]:
        timestamp = str(int(self.clock()))
        digest = hashlib.sha1(  # noqa: S324 - mandated by the provider protocol
            f"{self.api_key}{self.api_secret}{timestamp}".encode("utf-8")
        ).hexdigest()
        return {
            "X-Auth-Date": timestamp,
            "X-Auth-Key": self.api_key,
            "Authorization": digest,
        }

    def _apple_url(self, query: str, country: str) -> str:
        arguments = {
            "media": "podcast",
            "entity": "podcast",
            "country": country,
            "limit": str(MAXIMUM_PROVIDER_RESULTS),
            "term": query,
        }
        return f"{self.APPLE_SEARCH_URL}?{urlencode(arguments, quote_via=quote)}"


# Shorter name for new callers while retaining parity with the Android class.
PodcastDirectoryClient = PodcastIndexDirectoryClient


def parse_podcast_directory_response(payload: str | bytes) -> list[DirectoryEntry]:
    try:
        root = json.loads(payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload)
    except (UnicodeError, json.JSONDecodeError) as exception:
        raise DirectoryInvalidResponseError(
            "The podcast directory response is invalid."
        ) from exception
    if not isinstance(root, Mapping):
        raise DirectoryInvalidResponseError("The podcast directory response is invalid.")
    results = root.get("results")
    podcast_index_response = False
    if not isinstance(results, list):
        results = root.get("feeds")
        podcast_index_response = isinstance(results, list)
    if not isinstance(results, list):
        return []
    entries: list[DirectoryEntry] = []
    known_urls: set[str] = set()
    for item in results[:MAXIMUM_PROVIDER_RESULTS]:
        if not isinstance(item, Mapping):
            continue
        feed_url = _json_text(
            item.get("url") if podcast_index_response else item.get("feedUrl")
        )
        key = podcast_feed_deduplication_key(feed_url)
        if not _is_supported_web_url(feed_url) or key in known_urls:
            continue
        known_urls.add(key)
        if podcast_index_response:
            title = _json_text(item.get("title"))
            detail = _json_text(item.get("author")) or _json_text(
                item.get("ownerName")
            )
        else:
            title = _json_text(item.get("collectionName")) or _json_text(
                item.get("trackName")
            )
            detail = _json_text(item.get("artistName"))
        title = title or urlsplit(feed_url).hostname or feed_url
        entries.append(
            DirectoryEntry(
                title=title,
                url=feed_url,
                detail=detail,
            )
        )
    return entries


def _json_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def podcast_feed_deduplication_key(value: str) -> str:
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.casefold()
        host = (parts.hostname or "").casefold()
        if not host:
            return raw
        try:
            port = parts.port
        except ValueError:
            return raw
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        host_text = f"[{host}]" if ":" in host else host
        user_info = ""
        if "@" in parts.netloc:
            user_info = parts.netloc.rsplit("@", 1)[0] + "@"
        path = _normalize_url_path(parts.path)
        suffix = f"?{parts.query}" if parts.query else ""
        return f"{user_info}{host_text}{f':{port}' if port is not None else ''}{path}{suffix}"
    except (TypeError, ValueError):
        return raw


def _normalize_url_path(path: str) -> str:
    if not path or path == "/":
        return ""
    normalized = posixpath.normpath(path)
    if path.startswith("/") and not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/")


def _is_supported_web_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return parts.scheme.casefold() in {"http", "https"} and bool(parts.netloc)
    except (TypeError, ValueError):
        return False


def _require_web_url(value: str) -> str:
    raw = value.strip()
    if not _is_supported_web_url(raw):
        raise DirectoryInvalidUrlError("The podcast directory URL is invalid.")
    return raw


def _url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").casefold(), port


def podcast_storefront_country(locale_name: str | None = None) -> str:
    language, country = _split_locale(locale_name or _default_locale_name())
    if _TWO_LETTER_COUNTRY.fullmatch(country):
        return country.upper()
    return {"cs": "CZ", "sk": "SK", "en": "US"}.get(language.casefold(), "US")


def podcast_search_locale(app_locale: str, device_locale: str) -> str:
    app_language, app_country = _split_locale(app_locale)
    device_language, device_country = _split_locale(device_locale)
    if _TWO_LETTER_COUNTRY.fullmatch(app_country):
        return f"{app_language}-{app_country.upper()}"
    language = app_language or device_language or "en"
    if language.casefold() == "cs":
        return "cs-CZ"
    if _TWO_LETTER_COUNTRY.fullmatch(device_country):
        return f"{language}-{device_country.upper()}"
    return app_locale


def _default_locale_name() -> str:
    locale_name = system_locale.getlocale()[0]
    return locale_name or "en-US"


def _split_locale(value: str) -> tuple[str, str]:
    normalized = value.split(".", 1)[0].replace("_", "-")
    parts = [part for part in normalized.split("-") if part]
    language = parts[0] if parts else ""
    country = next(
        (part for part in parts[1:] if _TWO_LETTER_COUNTRY.fullmatch(part)),
        "",
    )
    return language, country


def podcast_search_variants(query: str) -> list[str]:
    words = query.strip().split()
    canonical = " ".join(words)
    if len(words) <= 1:
        return [canonical]
    variants: list[str] = []

    def add(value: str) -> None:
        if value and value not in variants and len(variants) < MAXIMUM_APPLE_QUERY_VARIANTS:
            variants.append(value)

    add(canonical)
    add(" ".join(words[:-1]))
    add(words[0])
    for word in sorted(words, key=lambda item: len(normalize_search_text(item)), reverse=True):
        add(word)
    return variants


def podcast_relevance_score(entry: DirectoryEntry, query: str) -> int:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return 0
    title = normalize_search_text(entry.title)
    searchable = normalize_search_text(f"{entry.title} {entry.detail}")
    terms = normalized_query.split()
    score = sum(100 for term in terms if term in searchable)
    if all(term in searchable for term in terms):
        score += 10_000
    if all(term in title for term in terms):
        score += 20_000
    if normalized_query in title:
        score += 30_000
    if title.startswith(normalized_query):
        score += 40_000
    if title == normalized_query:
        score += 50_000
    return score


def rank_podcast_entries(
    entries: Collection[DirectoryEntry] | Iterable[DirectoryEntry], query: str
) -> list[DirectoryEntry]:
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda item: (-podcast_relevance_score(item[1], query), item[0]))
    return [entry for _, entry in indexed]


__all__ = [
    "DirectoryEntry",
    "DirectoryHttpError",
    "DirectoryInvalidResponseError",
    "DirectoryInvalidUrlError",
    "DirectoryNetworkError",
    "DirectoryRedirectError",
    "DirectoryTooLargeError",
    "HttpPodcastDirectorySource",
    "PodcastDirectoryClient",
    "PodcastDirectoryError",
    "PodcastDirectorySource",
    "PodcastIndexDirectoryClient",
    "RssDirectory",
    "normalize_search_text",
    "parse_podcast_directory_response",
    "podcast_feed_deduplication_key",
    "podcast_relevance_score",
    "podcast_search_locale",
    "podcast_search_variants",
    "podcast_storefront_country",
    "rank_podcast_entries",
    "search_rss_directory",
    "search_text_matches",
]
