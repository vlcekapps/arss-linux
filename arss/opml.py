"""Secure, bounded OPML import and export."""

from __future__ import annotations

from html import escape
from typing import BinaryIO, Final, Iterable
from urllib.parse import urlsplit
from xml.parsers import expat

from .feed import is_supported_web_url
from .models import FeedSubscription, ParsedFeed


MAXIMUM_OPML_BYTES: Final = 8 * 1024 * 1024
MAXIMUM_OUTLINES: Final = 1_000
MAXIMUM_TITLE_CODE_POINTS: Final = 512
MAXIMUM_URL_CODE_POINTS: Final = 4_096
MAXIMUM_XML_DEPTH: Final = 256


class OpmlError(Exception):
    """Base class for OPML errors safe for a frontend to classify."""


class OpmlParseError(OpmlError):
    """The input is not a well-formed OPML document."""


class OpmlSecurityError(OpmlParseError):
    """The input attempted to declare a DTD or XML entity."""


class OpmlLimitError(OpmlError):
    """A byte, item, depth, title, or URL limit was exceeded."""


class OpmlValidationError(OpmlError):
    """An exported subscription violates the persistent model contract."""


def read_opml(
    source: bytes | bytearray | memoryview | BinaryIO,
    *,
    maximum_bytes: int = MAXIMUM_OPML_BYTES,
) -> tuple[FeedSubscription, ...]:
    """Read every feed outline, including outlines nested in folders.

    Folder names are intentionally not part of the ARSS data model. Importing a
    nested document therefore produces a flat subscription list, matching the
    Android application.
    """

    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    payload = _read_limited(source, maximum_bytes)
    handler = _OpmlHandler()
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartElementHandler = handler.start_element
    parser.EndElementHandler = handler.end_element
    parser.StartDoctypeDeclHandler = handler.reject_doctype
    parser.EntityDeclHandler = handler.reject_entity_declaration
    parser.ExternalEntityRefHandler = handler.reject_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(payload, True)
    except OpmlError:
        raise
    except expat.ExpatError as error:
        raise OpmlParseError(
            f"OPML is not valid XML at line {error.lineno}, column {error.offset}"
        ) from error
    if not handler.root_seen:
        raise OpmlParseError("OPML document is empty")
    return tuple(handler.subscriptions)


def write_opml(
    subscriptions: Iterable[FeedSubscription],
    output: BinaryIO | None = None,
    *,
    maximum_bytes: int = MAXIMUM_OPML_BYTES,
) -> bytes:
    """Serialize subscriptions as UTF-8 OPML and optionally write them."""

    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    snapshot = tuple(subscriptions)
    if len(snapshot) > MAXIMUM_OUTLINES:
        raise OpmlLimitError("OPML contains too many subscriptions")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head><title>ARSS</title></head>",
        "  <body>",
    ]
    for subscription in snapshot:
        title = _validated_export_value(
            subscription.title,
            MAXIMUM_TITLE_CODE_POINTS,
            "Subscription title is too long",
        )
        url = _validated_export_value(
            subscription.url,
            MAXIMUM_URL_CODE_POINTS,
            "Subscription URL is too long",
        )
        if not is_supported_web_url(url):
            raise OpmlValidationError("Subscription URL must use HTTP or HTTPS")
        escaped_title = _escape_attribute(title)
        escaped_url = _escape_attribute(url)
        lines.append(
            f'    <outline type="rss" text="{escaped_title}" '
            f'title="{escaped_title}" xmlUrl="{escaped_url}"/>'
        )
    lines.extend(("  </body>", "</opml>", ""))
    payload = "\n".join(lines).encode("utf-8")
    if len(payload) > maximum_bytes:
        raise OpmlLimitError("OPML exceeds the configured byte limit")
    if output is not None:
        output.write(payload)
    return payload


def merge_subscriptions(
    existing: Iterable[FeedSubscription],
    imported: Iterable[FeedSubscription],
) -> tuple[tuple[FeedSubscription, ...], tuple[FeedSubscription, ...]]:
    """Return ``(added, merged)`` while retaining the first exact URL match."""

    existing_snapshot = tuple(existing)
    known_urls = {subscription.url for subscription in existing_snapshot}
    added: list[FeedSubscription] = []
    for subscription in imported:
        if subscription.url in known_urls:
            continue
        known_urls.add(subscription.url)
        added.append(subscription)
    return tuple(added), existing_snapshot + tuple(added)


def accept_podcast_import(
    subscription: FeedSubscription,
    parsed_feed: ParsedFeed,
) -> FeedSubscription | None:
    """Return the original OPML item when its feed contains playable audio.

    Fetching the feed validates that this really is a podcast, but must not
    replace a title the user deliberately stored in OPML.
    """

    if any(article.media_url for article in parsed_feed.articles):
        return subscription
    return None


class _OpmlHandler:
    def __init__(self) -> None:
        self.depth = 0
        self.outline_count = 0
        self.root_seen = False
        self.subscriptions: list[FeedSubscription] = []

    def start_element(self, expanded_name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        if self.depth > MAXIMUM_XML_DEPTH:
            raise OpmlLimitError("OPML nesting is too deep")
        name = _local_name(expanded_name)
        if self.depth == 1:
            self.root_seen = True
            if name.lower() != "opml":
                raise OpmlParseError("Document root is not OPML")
        if name.lower() != "outline":
            return

        self.outline_count += 1
        if self.outline_count > MAXIMUM_OUTLINES:
            raise OpmlLimitError("OPML contains too many outline elements")
        raw_url = _attribute(attributes, "xmlUrl")
        if raw_url is None:
            raw_url = _attribute(attributes, "url")
        if raw_url is None:
            return
        _require_length(
            raw_url,
            MAXIMUM_URL_CODE_POINTS,
            "Subscription URL is too long",
        )
        url = raw_url.strip()
        if not is_supported_web_url(url):
            return

        title_value = _attribute(attributes, "title")
        if not title_value or not title_value.strip():
            title_value = _attribute(attributes, "text")
        title = (title_value or _fallback_title(url)).strip()
        if not title:
            title = _fallback_title(url)
        _require_length(
            title,
            MAXIMUM_TITLE_CODE_POINTS,
            "Subscription title is too long",
        )
        self.subscriptions.append(FeedSubscription(title, url))

    def end_element(self, expanded_name: str) -> None:
        del expanded_name
        self.depth -= 1

    def reject_doctype(
        self,
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: bool,
    ) -> None:
        del name, system_id, public_id, has_internal_subset
        raise OpmlSecurityError("DTD declarations are prohibited in OPML")

    def reject_entity_declaration(self, *declaration: object) -> None:
        del declaration
        raise OpmlSecurityError("Entity declarations are prohibited in OPML")

    def reject_external_entity(self, *entity: object) -> int:
        del entity
        raise OpmlSecurityError("External XML entities are prohibited in OPML")


def _read_limited(
    source: bytes | bytearray | memoryview | BinaryIO,
    maximum_bytes: int,
) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
        if len(payload) > maximum_bytes:
            raise OpmlLimitError("OPML exceeds the configured byte limit")
        return payload

    output = bytearray()
    while True:
        chunk = source.read(min(64 * 1024, maximum_bytes - len(output) + 1))
        if not chunk:
            return bytes(output)
        if not isinstance(chunk, bytes):
            raise TypeError("OPML input stream must return bytes")
        if len(output) > maximum_bytes - len(chunk):
            raise OpmlLimitError("OPML exceeds the configured byte limit")
        output.extend(chunk)


def _local_name(expanded_name: str) -> str:
    return expanded_name.split(" ", 1)[-1]


def _attribute(attributes: dict[str, str], wanted_name: str) -> str | None:
    for expanded_name, value in attributes.items():
        if _local_name(expanded_name).lower() == wanted_name.lower():
            return value
    return None


def _fallback_title(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _validated_export_value(value: str, maximum: int, message: str) -> str:
    if not isinstance(value, str):
        raise OpmlValidationError("Subscription fields must be strings")
    _require_length(value, maximum, message)
    return value


def _require_length(value: str, maximum: int, message: str) -> None:
    if len(value) > maximum:
        raise OpmlLimitError(message)


def _escape_attribute(value: str) -> str:
    sanitized = "".join(
        character if _is_xml_10_character(ord(character)) else "\ufffd"
        for character in value
    )
    return escape(sanitized, quote=True).replace("&#x27;", "&apos;")


def _is_xml_10_character(code_point: int) -> bool:
    return (
        code_point in {0x9, 0xA, 0xD}
        or 0x20 <= code_point <= 0xD7FF
        or 0xE000 <= code_point <= 0xFFFD
        or 0x10000 <= code_point <= 0x10FFFF
    )
