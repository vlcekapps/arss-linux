from __future__ import annotations

from io import BytesIO
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from arss.feed import (
    FeedClient,
    FeedNetworkError,
    FeedParseError,
    FeedRedirectError,
    FeedSecurityError,
    FeedTooLargeError,
    InvalidFeedUrlError,
    UnsupportedFeedFormatError,
    parse_feed,
    parse_feed_date_millis,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FeedParserTest(unittest.TestCase):
    def test_rss_sanitizes_resolves_media_and_sorts_newest_first(self) -> None:
        parsed = parse_feed(
            (FIXTURES / "rss2.xml").read_bytes(),
            "https://example.test/feeds/news.xml",
        )

        self.assertEqual("České & zprávy", parsed.title)
        self.assertEqual(3, len(parsed.articles))
        newest, older, undated = parsed.articles
        self.assertEqual("Novější & test", newest.title)
        self.assertEqual("https://example.test/clanky/novejsi", newest.url)
        self.assertEqual("article-new", newest.source_id)
        self.assertEqual("https://cdn.example.test/audio.mp3", newest.media_url)
        self.assertEqual("audio/mpeg", newest.media_type)
        self.assertGreater(newest.published_at_millis or 0, older.published_at_millis or 0)
        self.assertEqual("", undated.title)
        self.assertIsNone(undated.published_at_millis)

    def test_prefixed_atom_prefers_updated_and_ignores_image_enclosure(self) -> None:
        parsed = parse_feed(
            (FIXTURES / "atom1.xml").read_bytes(),
            "https://example.test/feeds/atom.xml",
        )

        self.assertEqual("Obecný Atom", parsed.title)
        self.assertEqual(2, len(parsed.articles))
        first, second = parsed.articles
        self.assertEqual("První & test", first.title)
        self.assertEqual("urn:uuid:article-one", first.source_id)
        self.assertEqual("https://example.test/article/one", first.url)
        self.assertEqual("2026-07-22T11:15:00.987Z", first.published_text)
        self.assertIsNone(first.media_url)
        self.assertEqual("https://example.test/article/two", second.url)

    def test_tn_atom_regression_uses_links_not_image_enclosures(self) -> None:
        parsed = parse_feed(
            (FIXTURES / "tn_nova_atom.xml").read_bytes(),
            "https://tn.nova.cz/feed/atom/tnnova-2",
        )

        self.assertEqual("Zpravodajství", parsed.title)
        self.assertEqual(2, len(parsed.articles))
        self.assertEqual("Testovací článek & zprávy", parsed.articles[0].title)
        self.assertTrue(
            all(item.url.startswith("https://tn.nova.cz/") for item in parsed.articles)
        )
        self.assertTrue(all(item.media_url is None for item in parsed.articles))

    def test_podcast_duration_and_media_rss_fallback_are_parsed(self) -> None:
        document = b"""<?xml version='1.0'?>
        <rss version='2.0'
          xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'
          xmlns:media='http://search.yahoo.com/mrss/'>
          <channel><title>Podcast</title><item>
            <guid isPermaLink='false'>episode-1</guid>
            <title>Episode</title>
            <link>../episodes/1</link>
            <itunes:duration>01:02:03</itunes:duration>
            <media:content url='../audio/episode.ogg' type='audio/ogg'/>
          </item></channel>
        </rss>"""

        article = parse_feed(
            document, "https://pod.example.test/shows/feed.xml"
        ).articles[0]

        self.assertEqual("https://pod.example.test/episodes/1", article.url)
        self.assertEqual("https://pod.example.test/audio/episode.ogg", article.media_url)
        self.assertEqual("audio/ogg", article.media_type)
        self.assertEqual("01:02:03", article.duration_text)

    def test_doctype_and_entities_are_rejected(self) -> None:
        document = b"""<!DOCTYPE rss [<!ENTITY title 'unsafe'>]>
        <rss version='2.0'><channel><title>&title;</title></channel></rss>"""

        with self.assertRaises(FeedSecurityError):
            parse_feed(document, "https://example.test/feed")

    def test_invalid_xml_format_and_size_have_typed_errors(self) -> None:
        with self.assertRaises(FeedParseError):
            parse_feed(b"<rss><channel>", "https://example.test/feed")
        with self.assertRaises(UnsupportedFeedFormatError):
            parse_feed(b"<html/>", "https://example.test/feed")
        with self.assertRaises(FeedTooLargeError):
            parse_feed(
                BytesIO(b"<rss/>"),
                "https://example.test/feed",
                maximum_bytes=4,
            )

    def test_complete_root_tolerates_only_trailing_parser_garbage(self) -> None:
        parsed = parse_feed(
            b"<rss><channel><title>Complete</title></channel></rss><!-- broken",
            "https://example.test/feed",
        )
        self.assertEqual("Complete", parsed.title)
        self.assertEqual((), parsed.articles)

    def test_dates_require_a_timezone_and_preserve_milliseconds(self) -> None:
        self.assertEqual(
            1_784_718_900_987,
            parse_feed_date_millis("2026-07-22T11:15:00.987Z"),
        )
        self.assertEqual(
            1_784_714_400_000,
            parse_feed_date_millis("Wed, 22 Jul 2026 10:00:00 GMT"),
        )
        self.assertIsNone(parse_feed_date_millis("2026-07-22T11:15:00"))
        self.assertIsNone(parse_feed_date_millis("not a date"))


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        reason: str = "Test response",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks
        self.reason = reason

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def iter_content(self, chunk_size: int) -> tuple[bytes, ...]:
        self.requested_chunk_size = chunk_size
        return self.chunks


class _FakeSession:
    fixture = (FIXTURES / "rss2.xml").read_bytes()

    def __init__(self) -> None:
        self.observed_user_agents: list[str] = []
        self.observed_requests: list[tuple[str, object, bool, bool]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: object,
        stream: bool,
        allow_redirects: bool,
    ) -> _FakeResponse:
        self.observed_user_agents.append(headers["User-Agent"])
        self.observed_requests.append((url, timeout, stream, allow_redirects))
        path = urlsplit(url).path
        if path == "/redirect":
            return _FakeResponse(302, headers={"Location": "/gzip"})
        if path == "/gzip":
            # requests transparently decompresses before iter_content yields.
            return _FakeResponse(
                200,
                headers={"Content-Encoding": "gzip"},
                chunks=(self.fixture,),
            )
        if path == "/ua":
            if headers["User-Agent"].startswith("ARSS-Test/"):
                return _FakeResponse(403)
            return self._feed()
        if path == "/loop-a":
            return _FakeResponse(302, headers={"Location": "/loop-b"})
        if path == "/loop-b":
            return _FakeResponse(302, headers={"Location": "/loop-a"})
        if path == "/chain-a":
            return _FakeResponse(302, headers={"Location": "/chain-b"})
        if path == "/chain-b":
            return _FakeResponse(302, headers={"Location": "/feed"})
        if path == "/large-declared":
            return _FakeResponse(200, headers={"Content-Length": "257"})
        if path == "/large-streamed":
            return _FakeResponse(200, chunks=(b"x" * 257,))
        if path == "/feed":
            return self._feed()
        return _FakeResponse(404)

    def close(self) -> None:
        pass

    def _feed(self) -> _FakeResponse:
        return _FakeResponse(
            200,
            headers={"Content-Length": str(len(self.fixture))},
            chunks=(self.fixture,),
        )


class _RedirectResponse:
    status_code = 302
    reason = "Found"
    headers = {"Location": "http://insecure.example.test/feed"}

    def __enter__(self) -> _RedirectResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _RedirectSession:
    def get(self, *args: object, **kwargs: object) -> _RedirectResponse:
        del args, kwargs
        return _RedirectResponse()

    def close(self) -> None:
        pass


class FeedClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _FakeSession()
        self.base_url = "http://unit.test"

    def test_follows_relative_redirect_and_decodes_gzip(self) -> None:
        with FeedClient(
            user_agent="ARSS-Test/1",
            session=self.session,  # type: ignore[arg-type]
        ) as client:
            parsed = client.fetch(f"{self.base_url}/redirect")

        self.assertEqual("České & zprávy", parsed.title)
        self.assertEqual(3, len(parsed.articles))
        self.assertTrue(
            all(value == "ARSS-Test/1" for value in self.session.observed_user_agents)
        )
        self.assertTrue(all(request[2] for request in self.session.observed_requests))
        self.assertTrue(all(not request[3] for request in self.session.observed_requests))

    def test_retries_only_a_user_agent_sensitive_status(self) -> None:
        with FeedClient(
            user_agent="ARSS-Test/1",
            fallback_user_agent="Fallback-Test/1",
            session=self.session,  # type: ignore[arg-type]
        ) as client:
            parsed = client.fetch(f"{self.base_url}/ua")

        self.assertEqual("České & zprávy", parsed.title)
        self.assertEqual(
            ["ARSS-Test/1", "Fallback-Test/1"],
            self.session.observed_user_agents,
        )

    def test_rejects_loops_and_redirect_chains_over_limit(self) -> None:
        with FeedClient(session=self.session) as client:  # type: ignore[arg-type]
            with self.assertRaises(FeedRedirectError):
                client.fetch(f"{self.base_url}/loop-a")
        with FeedClient(
            maximum_redirects=1,
            session=_FakeSession(),  # type: ignore[arg-type]
        ) as client:
            with self.assertRaises(FeedRedirectError):
                client.fetch(f"{self.base_url}/chain-a")

    def test_enforces_declared_and_decoded_stream_limits(self) -> None:
        with FeedClient(
            maximum_response_bytes=256,
            session=self.session,  # type: ignore[arg-type]
        ) as client:
            with self.assertRaises(FeedTooLargeError):
                client.fetch(f"{self.base_url}/large-declared")
            with self.assertRaises(FeedTooLargeError):
                client.fetch(f"{self.base_url}/large-streamed")

    def test_rejects_invalid_schemes_before_network_access(self) -> None:
        with FeedClient(session=self.session) as client:  # type: ignore[arg-type]
            with self.assertRaises(InvalidFeedUrlError):
                client.fetch("file:///etc/passwd")
        self.assertEqual([], self.session.observed_requests)

    def test_rejects_https_to_http_downgrade(self) -> None:
        client = FeedClient(session=_RedirectSession())  # type: ignore[arg-type]
        with self.assertRaises(FeedRedirectError):
            client.fetch("https://secure.example.test/feed")

    def test_cancel_blocks_requests_until_resumed(self) -> None:
        client = FeedClient(session=self.session)  # type: ignore[arg-type]
        client.cancel()
        with self.assertRaises(FeedNetworkError):
            client.fetch(f"{self.base_url}/feed")
        self.assertEqual([], self.session.observed_requests)
        client.resume()
        self.assertEqual("České & zprávy", client.fetch(f"{self.base_url}/feed").title)


if __name__ == "__main__":
    unittest.main()
