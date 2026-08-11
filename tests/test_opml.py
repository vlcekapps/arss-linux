from __future__ import annotations

from io import BytesIO
import unittest

from arss.models import FeedArticle, FeedSubscription, ParsedFeed
from arss.opml import (
    MAXIMUM_OUTLINES,
    MAXIMUM_TITLE_CODE_POINTS,
    OpmlLimitError,
    OpmlSecurityError,
    accept_podcast_import,
    merge_subscriptions,
    read_opml,
    write_opml,
)


class OpmlTest(unittest.TestCase):
    def test_round_trip_preserves_unicode_titles_and_addresses(self) -> None:
        subscriptions = (
            FeedSubscription("České & <zprávy>", "https://example.test/rss?a=1&b=2"),
            FeedSubscription("Podcast 'One'", "http://pod.example.test/feed.xml"),
        )
        output = BytesIO()

        payload = write_opml(subscriptions, output)

        self.assertEqual(payload, output.getvalue())
        self.assertEqual(subscriptions, read_opml(payload))

    def test_nested_outlines_are_flattened_and_non_web_urls_skipped(self) -> None:
        payload = b"""<?xml version='1.0'?>
        <opml version='2.0'><body>
          <outline text='Folder'>
            <outline TEXT='Fallback text' XMLURL='https://one.example/rss'/>
            <outline title='Second' url='http://two.example/feed'/>
            <outline text='Local file' xmlUrl='file:///tmp/feed'/>
          </outline>
          <outline xmlUrl='https://fallback.example/path'/>
        </body></opml>"""

        self.assertEqual(
            (
                FeedSubscription("Fallback text", "https://one.example/rss"),
                FeedSubscription("Second", "http://two.example/feed"),
                FeedSubscription("fallback.example", "https://fallback.example/path"),
            ),
            read_opml(payload),
        )

    def test_doctype_and_entities_are_rejected(self) -> None:
        payload = b"""<!DOCTYPE opml [<!ENTITY secret SYSTEM 'file:///etc/passwd'>]>
        <opml><body><outline text='x' xmlUrl='https://x.test/&secret;'/></body></opml>"""

        with self.assertRaises(OpmlSecurityError):
            read_opml(payload)

    def test_input_byte_outline_and_title_limits_are_enforced(self) -> None:
        with self.assertRaises(OpmlLimitError):
            read_opml(BytesIO(b"<opml/>") , maximum_bytes=4)

        outlines = "".join(
            f"<outline text='Folder {index}'/>"
            for index in range(MAXIMUM_OUTLINES + 1)
        )
        with self.assertRaises(OpmlLimitError):
            read_opml(f"<opml><body>{outlines}</body></opml>".encode())

        title = "x" * (MAXIMUM_TITLE_CODE_POINTS + 1)
        with self.assertRaises(OpmlLimitError):
            write_opml((FeedSubscription(title, "https://example.test/feed"),))

    def test_export_replaces_xml_1_0_forbidden_characters(self) -> None:
        payload = write_opml(
            (FeedSubscription("Before\x00after 😀", "https://example.test/feed"),)
        )

        self.assertNotIn(b"\x00", payload)
        self.assertIn("Before�after 😀", payload.decode("utf-8"))
        self.assertEqual("Before�after 😀", read_opml(payload)[0].title)

    def test_merge_is_exact_stable_and_detached_from_inputs(self) -> None:
        existing = [FeedSubscription("Existing", "https://example.test/feed")]
        imported = [
            FeedSubscription("Duplicate", "https://example.test/feed"),
            FeedSubscription("New", "https://example.test/new"),
            FeedSubscription("Repeated", "https://example.test/new"),
        ]

        added, merged = merge_subscriptions(existing, imported)
        existing.clear()
        imported.clear()

        self.assertEqual(
            (FeedSubscription("New", "https://example.test/new"),), added
        )
        self.assertEqual(
            (
                FeedSubscription("Existing", "https://example.test/feed"),
                FeedSubscription("New", "https://example.test/new"),
            ),
            merged,
        )

    def test_podcast_validation_preserves_the_custom_opml_title(self) -> None:
        imported = FeedSubscription(
            "Můj vlastní název",
            "https://example.test/podcast.xml",
        )
        parsed = ParsedFeed(
            "Název vydavatele",
            (
                FeedArticle(
                    "Epizoda",
                    "https://example.test/episode",
                    media_url="https://example.test/episode.mp3",
                ),
            ),
        )

        accepted = accept_podcast_import(imported, parsed)

        self.assertIs(imported, accepted)
        self.assertEqual("Můj vlastní název", accepted.title)
        self.assertIsNone(
            accept_podcast_import(
                imported,
                ParsedFeed(
                    "Název vydavatele",
                    (FeedArticle("Článek", "https://example.test/article"),),
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
