"""Localized, user-safe descriptions of recoverable domain failures.

Exception messages remain useful diagnostics for tests and logs, but they are
English implementation details. GTK surfaces these classifications instead
so changing the application's language changes the complete error message.
"""

from __future__ import annotations

from typing import Protocol

from .directory import (
    DirectoryHttpError,
    DirectoryInvalidResponseError,
    DirectoryInvalidUrlError,
    DirectoryNetworkError,
    DirectoryRedirectError,
    DirectoryTooLargeError,
)
from .feed import (
    FeedHttpError,
    FeedNetworkError,
    FeedParseError,
    FeedRedirectError,
    FeedTooLargeError,
    InvalidFeedUrlError,
    UnsupportedFeedFormatError,
)
from .opml import (
    OpmlLimitError,
    OpmlParseError,
    OpmlSecurityError,
    OpmlValidationError,
)


class MessageTranslator(Protocol):
    def __call__(self, key: str, **values: object) -> str: ...


def feed_error_message(
    translator: MessageTranslator,
    error: BaseException,
) -> str:
    """Describe a feed transport/parser failure without leaking raw text."""

    if isinstance(error, InvalidFeedUrlError):
        return translator("feed_error_invalid_url")
    if isinstance(error, FeedNetworkError):
        return translator("feed_error_network")
    if isinstance(error, FeedHttpError):
        return translator("feed_error_http", status=error.status_code)
    if isinstance(error, FeedRedirectError):
        return translator("feed_error_redirect")
    if isinstance(error, FeedTooLargeError):
        return translator(
            "feed_error_too_large",
            maximum=error.maximum_bytes,
        )
    if isinstance(error, UnsupportedFeedFormatError):
        return translator("feed_error_unsupported_format")
    if isinstance(error, FeedParseError):
        return translator("feed_error_invalid_xml")
    return translator("feed_error_unknown")


def directory_error_message(
    translator: MessageTranslator,
    error: BaseException,
) -> str:
    """Describe a local or remote directory failure in the selected locale."""

    if isinstance(error, DirectoryNetworkError):
        return translator("directory_error_network")
    if isinstance(error, DirectoryHttpError):
        return translator("directory_error_http", status=error.status_code)
    if isinstance(error, (DirectoryRedirectError, DirectoryInvalidUrlError)):
        return translator("directory_error_redirect")
    if isinstance(error, DirectoryTooLargeError):
        return translator(
            "directory_error_too_large",
            maximum=error.maximum_bytes,
        )
    if isinstance(error, DirectoryInvalidResponseError):
        return translator("directory_error_invalid_response")
    return translator("directory_error_unknown")


def opml_error_message(
    translator: MessageTranslator,
    error: BaseException,
) -> str:
    """Describe unsafe/invalid OPML separately from file-access failures."""

    if isinstance(
        error,
        (
            OpmlParseError,
            OpmlSecurityError,
            OpmlLimitError,
            OpmlValidationError,
        ),
    ):
        return translator("opml_error_invalid_document")
    return translator("opml_error_file_access")


__all__ = (
    "MessageTranslator",
    "directory_error_message",
    "feed_error_message",
    "opml_error_message",
)
