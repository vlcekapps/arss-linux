"""Bounded HTTP fetching and secure RSS 2.0 / Atom 1.0 parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum, auto
from html import unescape
import re
import threading
from types import TracebackType
from typing import BinaryIO, Final
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.parsers import expat

import requests

from . import __version__
from .models import FeedArticle, ParsedFeed


DEFAULT_CONNECT_TIMEOUT_SECONDS: Final = 15.0
DEFAULT_READ_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_MAXIMUM_RESPONSE_BYTES: Final = 4 * 1024 * 1024
DEFAULT_MAXIMUM_REDIRECTS: Final = 5
DEFAULT_USER_AGENT: Final = f"ARSS-Linux/{__version__}"
FALLBACK_USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0 Safari/537.36"
)

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_USER_AGENT_RETRY_STATUSES: Final = frozenset({403, 406})
_AUDIO_FILE_EXTENSIONS: Final = (
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
)
_RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})"
    r"(?:\.(\d+))?([zZ]|[+-]\d{2}:\d{2})$"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


class FeedError(Exception):
    """Base class for failures safe for a frontend to classify."""


class InvalidFeedUrlError(FeedError):
    """The supplied feed address is not an absolute HTTP(S) URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Invalid feed URL: {url}")


class FeedNetworkError(FeedError):
    """The connection failed before a usable HTTP response was received."""


class FeedHttpError(FeedError):
    """The server returned a non-success HTTP status."""

    def __init__(self, status_code: int, reason: str | None = None) -> None:
        self.status_code = status_code
        self.reason = reason.strip() if reason else None
        detail = f": {self.reason}" if self.reason else ""
        super().__init__(f"Feed server returned HTTP {status_code}{detail}")


class FeedRedirectError(FeedError):
    """A redirect was missing, unsafe, cyclic, or over the configured limit."""


class FeedTooLargeError(FeedError):
    """The decoded response exceeded the configured byte limit."""

    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        super().__init__(f"Feed exceeds the {maximum_bytes}-byte limit")


class FeedParseError(FeedError):
    """The response is not well-formed supported feed XML."""


class FeedSecurityError(FeedParseError):
    """The XML contains a prohibited DTD or entity declaration."""


class UnsupportedFeedFormatError(FeedParseError):
    """The XML root is neither an RSS 2.0 nor an Atom feed."""


class FeedClient:
    """A synchronous, bounded HTTP client.

    Frontends should call :meth:`fetch` away from their UI thread. Redirects are
    handled here instead of by ``requests`` so loops, limits, and HTTPS
    downgrades have identical behavior on every desktop.
    """

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        maximum_response_bytes: int = DEFAULT_MAXIMUM_RESPONSE_BYTES,
        maximum_redirects: int = DEFAULT_MAXIMUM_REDIRECTS,
        user_agent: str = DEFAULT_USER_AGENT,
        fallback_user_agent: str = FALLBACK_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        if read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum_response_bytes must be positive")
        if maximum_redirects < 0:
            raise ValueError("maximum_redirects must not be negative")
        if not user_agent.strip() or not fallback_user_agent.strip():
            raise ValueError("user agents must not be blank")

        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_redirects = maximum_redirects
        self.user_agents = (user_agent.strip(), fallback_user_agent.strip())
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._cancelled = threading.Event()
        self._response_lock = threading.RLock()
        self._active_responses: list[object] = []

    def fetch(self, url: str) -> ParsedFeed:
        """Download and parse one feed, retrying only UA-sensitive HTTP errors."""

        self._require_not_cancelled()
        last_error: FeedHttpError | None = None
        for index, user_agent in enumerate(self.user_agents):
            try:
                payload, final_url = self._download_once(url, user_agent)
                return parse_feed(
                    payload,
                    final_url,
                    maximum_bytes=self.maximum_response_bytes,
                )
            except FeedHttpError as error:
                last_error = error
                has_fallback = index < len(self.user_agents) - 1
                if not has_fallback or error.status_code not in _USER_AGENT_RETRY_STATUSES:
                    raise
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        """Close the internally owned connection pool."""

        self.cancel()
        if self._owns_session:
            self._session.close()

    def cancel(self) -> None:
        """Best-effort interruption of streamed responses during app shutdown."""

        self._cancelled.set()
        with self._response_lock:
            responses = tuple(self._active_responses)
        for response in responses:
            close = getattr(response, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def resume(self) -> None:
        """Allow new requests after a scheduler stop that did not close the client."""

        self._cancelled.clear()

    def __enter__(self) -> FeedClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _download_once(self, url: str, user_agent: str) -> tuple[bytes, str]:
        current_url = _require_http_url(url)
        visited: set[str] = set()
        redirect_count = 0

        while True:
            self._require_not_cancelled()
            identity = _redirect_identity(current_url)
            if identity in visited:
                raise FeedRedirectError("Feed server created a redirect loop")
            visited.add(identity)

            try:
                response = self._session.get(
                    current_url,
                    headers={
                        "User-Agent": user_agent,
                        "Accept": (
                            "application/atom+xml, application/rss+xml, "
                            "application/xml, text/xml, */*"
                        ),
                        "Accept-Encoding": "gzip",
                    },
                    timeout=(
                        self.connect_timeout_seconds,
                        self.read_timeout_seconds,
                    ),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                raise FeedNetworkError("Feed could not be downloaded") from error

            self._register_response(response)
            try:
                with response:
                    self._require_not_cancelled()
                    status = response.status_code
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise FeedRedirectError("Feed redirect has no destination")
                        if redirect_count >= self.maximum_redirects:
                            raise FeedRedirectError("Feed exceeded the redirect limit")
                        target = _resolve_redirect(current_url, location)
                        if (
                            urlsplit(current_url).scheme.lower() == "https"
                            and urlsplit(target).scheme.lower() == "http"
                        ):
                            raise FeedRedirectError(
                                "An HTTPS feed attempted to redirect to insecure HTTP"
                            )
                        current_url = target
                        redirect_count += 1
                        continue

                    if status < 200 or status > 299:
                        raise FeedHttpError(status, response.reason)

                    declared_length = _non_negative_int(
                        response.headers.get("Content-Length")
                    )
                    if (
                        declared_length is not None
                        and declared_length > self.maximum_response_bytes
                    ):
                        raise FeedTooLargeError(self.maximum_response_bytes)

                    output = bytearray()
                    try:
                        # requests decodes gzip while streaming through iter_content.
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            self._require_not_cancelled()
                            if not chunk:
                                continue
                            if len(output) > self.maximum_response_bytes - len(chunk):
                                raise FeedTooLargeError(self.maximum_response_bytes)
                            output.extend(chunk)
                    except FeedError:
                        raise
                    except requests.RequestException as error:
                        raise FeedNetworkError("Feed response could not be read") from error
                    return bytes(output), current_url
            finally:
                self._unregister_response(response)

    def _require_not_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise FeedNetworkError("Feed request was cancelled")

    def _register_response(self, response: object) -> None:
        with self._response_lock:
            self._active_responses.append(response)

    def _unregister_response(self, response: object) -> None:
        with self._response_lock:
            try:
                self._active_responses.remove(response)
            except ValueError:
                pass


def parse_feed(
    source: bytes | bytearray | memoryview | BinaryIO,
    source_url: str,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_RESPONSE_BYTES,
) -> ParsedFeed:
    """Parse bounded RSS/Atom XML without resolving DTDs or external entities."""

    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    canonical_source_url = _require_http_url(source_url)
    payload = _read_limited(source, maximum_bytes)
    handler = _FeedHandler(canonical_source_url)
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.StartElementHandler = handler.start_element
    parser.EndElementHandler = handler.end_element
    parser.CharacterDataHandler = handler.character_data
    parser.StartDoctypeDeclHandler = handler.reject_doctype
    parser.EntityDeclHandler = handler.reject_entity_declaration
    parser.ExternalEntityRefHandler = handler.reject_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    try:
        parser.Parse(payload, True)
    except FeedError:
        raise
    except expat.ExpatError as error:
        # A few real feeds append a malformed cache comment after an otherwise
        # complete root. Stay strict inside the root and tolerate only that tail.
        if not handler.root_completed:
            raise FeedParseError(
                f"Feed is not valid XML at line {error.lineno}, column {error.offset}"
            ) from error
    return handler.to_parsed_feed()


def parse_feed_date_millis(value: str | None) -> int | None:
    """Parse common RSS/Atom timestamps to UTC epoch milliseconds."""

    candidate = value.strip() if value else ""
    if not candidate:
        return None

    match = _RFC3339.fullmatch(candidate)
    if match:
        fraction = match.group(3)
        normalized = f"{match.group(1)}T{match.group(2)}"
        if fraction:
            normalized += f".{fraction}"
        offset = match.group(4)
        normalized += "+00:00" if offset.lower() == "z" else offset
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return _datetime_to_epoch_millis(parsed)

    try:
        parsed_mail_date = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed_mail_date.tzinfo is None:
        return None
    return _datetime_to_epoch_millis(parsed_mail_date)


def is_supported_web_url(value: str) -> bool:
    """Return whether ``value`` is an absolute HTTP(S) URL with a host."""

    try:
        _require_http_url(value)
    except InvalidFeedUrlError:
        return False
    return True


class _FeedKind(Enum):
    RSS = auto()
    ATOM = auto()


class _CaptureTarget(Enum):
    FEED_TITLE = auto()
    ARTICLE_TITLE = auto()
    RSS_LINK = auto()
    ARTICLE_ID = auto()
    RSS_GUID = auto()
    RSS_DATE = auto()
    ATOM_PUBLISHED = auto()
    ATOM_UPDATED = auto()
    PODCAST_DURATION = auto()


@dataclass(slots=True)
class _ArticleBuilder:
    title: str | None = None
    rss_link: str | None = None
    atom_link: str | None = None
    atom_link_score: int = -(2**31)
    source_id: str | None = None
    guid: str | None = None
    guid_is_permalink: bool = True
    rss_date: str | None = None
    published: str | None = None
    updated: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    media_score: int = -(2**31)
    duration: str | None = None

    def consider_media(self, url: str | None, media_type: str | None) -> None:
        if not url or not url.strip():
            return
        normalized_url = url.strip()
        normalized_type = (
            media_type.split(";", 1)[0].strip().lower() if media_type else None
        )
        path = normalized_url.split("?", 1)[0].split("#", 1)[0].lower()
        has_audio_extension = path.endswith(_AUDIO_FILE_EXTENSIONS)
        if normalized_type and normalized_type.startswith("audio/"):
            score = 30
        elif not normalized_type and has_audio_extension:
            score = 20
        elif normalized_type == "application/octet-stream" and has_audio_extension:
            score = 15
        else:
            return
        if score > self.media_score:
            self.media_url = normalized_url
            self.media_type = normalized_type
            self.media_score = score

    def consider_atom_link(
        self,
        href: str | None,
        relation: str | None,
        media_type: str | None,
    ) -> None:
        if not href or not href.strip():
            return
        normalized_relation = (relation or "alternate").strip().lower() or "alternate"
        if normalized_relation != "alternate":
            return
        normalized_type = (
            media_type.split(";", 1)[0].strip().lower() if media_type else ""
        )
        if normalized_type in {"", "text/html"}:
            score = 30
        elif normalized_type == "application/xhtml+xml":
            score = 20
        else:
            score = 10
        if score > self.atom_link_score:
            self.atom_link = href.strip()
            self.atom_link_score = score

    def to_article(self, kind: _FeedKind, source_url: str) -> FeedArticle | None:
        if kind is _FeedKind.RSS:
            raw_url = _non_blank(self.rss_link)
            if raw_url is None and self.guid_is_permalink:
                raw_url = _non_blank(self.guid)
            published_text = _non_blank(self.rss_date)
            source_id = _non_blank(self.guid)
        else:
            raw_url = _non_blank(self.atom_link) or _non_blank(self.source_id)
            published_text = _non_blank(self.updated) or _non_blank(self.published)
            source_id = _non_blank(self.source_id)

        resolved_media_url = _resolve_web_url(self.media_url, source_url)
        resolved_url = _resolve_web_url(raw_url, source_url) or resolved_media_url
        if resolved_url is None:
            return None
        return FeedArticle(
            title=_sanitize_title(self.title),
            url=resolved_url,
            source_id=source_id,
            published_text=published_text,
            published_at_millis=parse_feed_date_millis(published_text),
            media_url=resolved_media_url,
            media_type=self.media_type,
            duration_text=_non_blank(self.duration),
        )


class _FeedHandler:
    def __init__(self, source_url: str) -> None:
        self.source_url = source_url
        self.depth = 0
        self.kind: _FeedKind | None = None
        self.rss_namespace = ""
        self.atom_namespace = ""
        self.channel_depth = -1
        self.article_depth = -1
        self.feed_title: str | None = None
        self.current_article: _ArticleBuilder | None = None
        self.articles: list[_ArticleBuilder] = []
        self.root_completed = False
        self.capture_target: _CaptureTarget | None = None
        self.capture_element = ""
        self.capture_depth = -1
        self.captured_text: list[str] = []

    def start_element(self, expanded_name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        namespace, name = _expanded_name(expanded_name)
        name = name.lower()

        if self.depth == 1:
            if name == "rss":
                self.kind = _FeedKind.RSS
                self.rss_namespace = namespace
            elif name == "feed":
                self.kind = _FeedKind.ATOM
                self.atom_namespace = namespace

        if self.capture_target is not None:
            if self.depth > self.capture_depth:
                self.captured_text.append(" ")
            return

        if self.kind is _FeedKind.RSS:
            self._handle_rss_start(namespace, name, attributes)
        elif self.kind is _FeedKind.ATOM:
            self._handle_atom_start(namespace, name, attributes)

    def end_element(self, expanded_name: str) -> None:
        namespace, name = _expanded_name(expanded_name)
        name = name.lower()
        if (
            self.capture_target is not None
            and self.depth == self.capture_depth
            and name == self.capture_element
        ):
            self._finish_capture()
        elif self.capture_target is not None and self.depth > self.capture_depth:
            self.captured_text.append(" ")

        if self.current_article is not None and self.depth == self.article_depth:
            closes_article = (
                self.kind is _FeedKind.RSS
                and name == "item"
                and self._is_rss_element(namespace)
            ) or (
                self.kind is _FeedKind.ATOM
                and name == "entry"
                and self._is_atom_element(namespace)
            )
            if closes_article:
                self.articles.append(self.current_article)
                self.current_article = None
                self.article_depth = -1

        if (
            self.kind is _FeedKind.RSS
            and name == "channel"
            and self.depth == self.channel_depth
            and self._is_rss_element(namespace)
        ):
            self.channel_depth = -1

        if self.depth == 1:
            self.root_completed = (
                self.kind is _FeedKind.RSS
                and name == "rss"
                and self._is_rss_element(namespace)
            ) or (
                self.kind is _FeedKind.ATOM
                and name == "feed"
                and self._is_atom_element(namespace)
            )
        self.depth -= 1

    def character_data(self, value: str) -> None:
        if self.capture_target is not None:
            self.captured_text.append(value)

    def reject_doctype(
        self,
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: bool,
    ) -> None:
        del name, system_id, public_id, has_internal_subset
        raise FeedSecurityError("DTD declarations are prohibited in feeds")

    def reject_entity_declaration(self, *declaration: object) -> None:
        del declaration
        raise FeedSecurityError("Entity declarations are prohibited in feeds")

    def reject_external_entity(self, *entity: object) -> int:
        del entity
        raise FeedSecurityError("External XML entities are prohibited in feeds")

    def to_parsed_feed(self) -> ParsedFeed:
        if self.kind is None:
            raise UnsupportedFeedFormatError(
                "Document is neither an RSS 2.0 nor an Atom 1.0 feed"
            )
        indexed_articles = [
            (index, article)
            for index, builder in enumerate(self.articles)
            if (article := builder.to_article(self.kind, self.source_url)) is not None
        ]
        indexed_articles.sort(
            key=lambda pair: (
                pair[1].published_at_millis is None,
                -(pair[1].published_at_millis or 0),
                pair[0],
            )
        )
        title = _sanitize_title(self.feed_title)
        if not title:
            title = urlsplit(self.source_url).hostname or self.source_url
        return ParsedFeed(title, tuple(article for _, article in indexed_articles))

    def _handle_rss_start(
        self,
        namespace: str,
        name: str,
        attributes: dict[str, str],
    ) -> None:
        article = self.current_article
        if article is not None and self.depth == self.article_depth + 1:
            if name == "duration":
                self._start_capture(_CaptureTarget.PODCAST_DURATION, name)
                return
            if name == "enclosure" and self._is_rss_element(namespace):
                article.consider_media(
                    _attribute(attributes, "url"),
                    _attribute(attributes, "type"),
                )
                return
            if name == "content" and not self._is_rss_element(namespace):
                article.consider_media(
                    _attribute(attributes, "url"),
                    _attribute(attributes, "type"),
                )
                return

        if not self._is_rss_element(namespace):
            return
        if name == "channel" and self.depth == 2:
            self.channel_depth = self.depth
            return
        if name == "item" and self.channel_depth > 0 and self.current_article is None:
            self.current_article = _ArticleBuilder()
            self.article_depth = self.depth
            return

        article = self.current_article
        if article is not None and self.depth == self.article_depth + 1:
            if name == "title":
                self._start_capture(_CaptureTarget.ARTICLE_TITLE, name)
            elif name == "link":
                self._start_capture(_CaptureTarget.RSS_LINK, name)
            elif name == "guid":
                article.guid_is_permalink = (
                    _attribute(attributes, "isPermaLink") or ""
                ).lower() != "false"
                self._start_capture(_CaptureTarget.RSS_GUID, name)
            elif name == "pubdate":
                self._start_capture(_CaptureTarget.RSS_DATE, name)
        elif (
            article is None
            and self.channel_depth > 0
            and self.depth == self.channel_depth + 1
            and name == "title"
        ):
            self._start_capture(_CaptureTarget.FEED_TITLE, name)

    def _handle_atom_start(
        self,
        namespace: str,
        name: str,
        attributes: dict[str, str],
    ) -> None:
        current = self.current_article
        if (
            current is not None
            and self.depth == self.article_depth + 1
            and not self._is_atom_element(namespace)
        ):
            if name == "duration":
                self._start_capture(_CaptureTarget.PODCAST_DURATION, name)
                return
            if name == "content":
                current.consider_media(
                    _attribute(attributes, "url"),
                    _attribute(attributes, "type"),
                )
                return

        if not self._is_atom_element(namespace):
            return
        if name == "entry" and self.depth == 2 and self.current_article is None:
            self.current_article = _ArticleBuilder()
            self.article_depth = self.depth
            return

        article = self.current_article
        if article is not None and self.depth == self.article_depth + 1:
            if name == "title":
                self._start_capture(_CaptureTarget.ARTICLE_TITLE, name)
            elif name == "id":
                self._start_capture(_CaptureTarget.ARTICLE_ID, name)
            elif name == "published":
                self._start_capture(_CaptureTarget.ATOM_PUBLISHED, name)
            elif name == "updated":
                self._start_capture(_CaptureTarget.ATOM_UPDATED, name)
            elif name == "link":
                relation = _attribute(attributes, "rel")
                if relation and relation.lower() == "enclosure":
                    article.consider_media(
                        _attribute(attributes, "href"),
                        _attribute(attributes, "type"),
                    )
                else:
                    article.consider_atom_link(
                        _attribute(attributes, "href"),
                        relation,
                        _attribute(attributes, "type"),
                    )
        elif article is None and self.depth == 2 and name == "title":
            self._start_capture(_CaptureTarget.FEED_TITLE, name)

    def _start_capture(self, target: _CaptureTarget, element: str) -> None:
        self.capture_target = target
        self.capture_element = element
        self.capture_depth = self.depth
        self.captured_text.clear()

    def _finish_capture(self) -> None:
        target = self.capture_target
        value = "".join(self.captured_text).strip()
        article = self.current_article
        if target is _CaptureTarget.FEED_TITLE and not _non_blank(self.feed_title):
            self.feed_title = value
        elif article is not None:
            if target is _CaptureTarget.ARTICLE_TITLE and not _non_blank(article.title):
                article.title = value
            elif target is _CaptureTarget.RSS_LINK and not _non_blank(article.rss_link):
                article.rss_link = value
            elif target is _CaptureTarget.ARTICLE_ID and not _non_blank(article.source_id):
                article.source_id = value
            elif target is _CaptureTarget.RSS_GUID and not _non_blank(article.guid):
                article.guid = value
            elif target is _CaptureTarget.RSS_DATE and not _non_blank(article.rss_date):
                article.rss_date = value
            elif target is _CaptureTarget.ATOM_PUBLISHED and not _non_blank(article.published):
                article.published = value
            elif target is _CaptureTarget.ATOM_UPDATED and not _non_blank(article.updated):
                article.updated = value
            elif target is _CaptureTarget.PODCAST_DURATION and not _non_blank(article.duration):
                article.duration = value
        self.capture_target = None
        self.capture_element = ""
        self.capture_depth = -1
        self.captured_text.clear()

    def _is_atom_element(self, namespace: str) -> bool:
        return namespace == self.atom_namespace

    def _is_rss_element(self, namespace: str) -> bool:
        return namespace == self.rss_namespace


def _read_limited(
    source: bytes | bytearray | memoryview | BinaryIO,
    maximum_bytes: int,
) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
        if len(payload) > maximum_bytes:
            raise FeedTooLargeError(maximum_bytes)
        return payload

    output = bytearray()
    while True:
        chunk = source.read(min(64 * 1024, maximum_bytes - len(output) + 1))
        if not chunk:
            return bytes(output)
        if not isinstance(chunk, bytes):
            raise TypeError("feed input stream must return bytes")
        if len(output) > maximum_bytes - len(chunk):
            raise FeedTooLargeError(maximum_bytes)
        output.extend(chunk)


def _require_http_url(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        # Accessing these properties validates brackets and the numeric port.
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError):
        raise InvalidFeedUrlError(value) from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or any(ord(character) < 0x20 for character in candidate)
    ):
        raise InvalidFeedUrlError(value)
    return candidate


def _resolve_redirect(current_url: str, location: str) -> str:
    try:
        target = urljoin(current_url, location)
    except ValueError as error:
        raise FeedRedirectError("Feed redirect URL is invalid") from error
    try:
        return _require_http_url(target)
    except InvalidFeedUrlError as error:
        raise FeedRedirectError("Feed redirect URL is not HTTP(S)") from error


def _redirect_identity(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit(
        (parsed.scheme.lower(), authority, path, parsed.query, "")
    )


def _resolve_web_url(value: str | None, source_url: str) -> str | None:
    raw = _non_blank(value)
    if raw is None:
        return None
    try:
        resolved = urljoin(source_url, raw.replace(" ", "%20"))
        return _require_http_url(resolved)
    except (InvalidFeedUrlError, ValueError):
        return None


def _expanded_name(value: str) -> tuple[str, str]:
    if " " not in value:
        return "", value
    namespace, local_name = value.split(" ", 1)
    return namespace, local_name


def _attribute(attributes: dict[str, str], wanted_name: str) -> str | None:
    for expanded_name, value in attributes.items():
        _, name = _expanded_name(expanded_name)
        if name.lower() == wanted_name.lower():
            return value
    return None


def _sanitize_title(value: str | None) -> str:
    if not value or not value.strip():
        return ""
    without_comments = _HTML_COMMENT.sub(" ", value)
    without_tags = _HTML_TAG.sub(" ", without_comments)
    return _WHITESPACE.sub(" ", unescape(without_tags).replace("\u00a0", " ")).strip()


def _non_blank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _non_negative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.strip(), 10)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _datetime_to_epoch_millis(value: datetime) -> int:
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
