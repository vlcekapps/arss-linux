from __future__ import annotations

import unittest

from arss.directory import (
    DirectoryHttpError,
    DirectoryInvalidResponseError,
    DirectoryInvalidUrlError,
    DirectoryNetworkError,
    DirectoryRedirectError,
    DirectoryTooLargeError,
)
from arss.feed import (
    FeedHttpError,
    FeedNetworkError,
    FeedParseError,
    FeedRedirectError,
    FeedSecurityError,
    FeedTooLargeError,
    InvalidFeedUrlError,
    UnsupportedFeedFormatError,
)
from arss.i18n import Translator
from arss.opml import (
    OpmlLimitError,
    OpmlParseError,
    OpmlSecurityError,
    OpmlValidationError,
)
from arss.user_errors import (
    directory_error_message,
    feed_error_message,
    opml_error_message,
)


class UserErrorTest(unittest.TestCase):
    def test_feed_failures_are_fully_localized_by_type(self) -> None:
        translator = Translator("cs")
        cases = (
            (InvalidFeedUrlError("bad"), "není platná"),
            (FeedNetworkError("raw network detail"), "stáhnout"),
            (FeedHttpError(503, "raw reason"), "503"),
            (FeedRedirectError("raw redirect detail"), "přesměrování"),
            (FeedTooLargeError(4096), "4096"),
            (UnsupportedFeedFormatError("raw root"), "RSS 2.0 ani Atom 1.0"),
            (FeedParseError("raw parser detail"), "platný XML"),
            (FeedSecurityError("raw security detail"), "platný XML"),
            (RuntimeError("raw unknown detail"), "neznámé chybě"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                message = feed_error_message(translator, error)
                self.assertIn(expected, message)
                self.assertNotIn("raw", message)

    def test_directory_failures_are_fully_localized_by_type(self) -> None:
        translator = Translator("en")
        cases = (
            (DirectoryNetworkError("raw network"), "could not be downloaded"),
            (DirectoryHttpError(429, "raw reason"), "429"),
            (DirectoryRedirectError("raw redirect"), "redirect"),
            (DirectoryInvalidUrlError("raw URL"), "redirect"),
            (DirectoryTooLargeError(8192), "8192"),
            (DirectoryInvalidResponseError("raw response"), "invalid response"),
            (RuntimeError("raw unknown"), "unknown directory error"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                message = directory_error_message(translator, error)
                self.assertIn(expected, message)
                self.assertNotIn("raw", message)

    def test_opml_parser_details_do_not_escape_the_selected_language(self) -> None:
        translator = Translator("cs")
        for error in (
            OpmlParseError("raw parse"),
            OpmlSecurityError("raw security"),
            OpmlLimitError("raw limit"),
            OpmlValidationError("raw validation"),
        ):
            with self.subTest(error=type(error).__name__):
                message = opml_error_message(translator, error)
                self.assertIn("platný a bezpečný dokument OPML", message)
                self.assertNotIn("raw", message)
        self.assertEqual(
            "K vybranému souboru se nepodařilo přistoupit.",
            opml_error_message(translator, OSError("raw file path")),
        )


if __name__ == "__main__":
    unittest.main()
