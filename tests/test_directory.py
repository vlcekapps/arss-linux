from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from arss.directory import (
    DirectoryEntry,
    DirectoryNetworkError,
    DirectoryRedirectError,
    DirectoryTooLargeError,
    HttpPodcastDirectorySource,
    PodcastIndexDirectoryClient,
    RssDirectory,
    normalize_search_text,
    parse_podcast_directory_response,
    podcast_feed_deduplication_key,
    podcast_search_locale,
    podcast_search_variants,
    podcast_storefront_country,
    rank_podcast_entries,
)


class DirectoryTests(unittest.TestCase):
    def test_local_catalog_is_offline_bounded_and_diacritic_independent(self) -> None:
        directory = RssDirectory()
        self.assertEqual(118, len(directory.entries()))
        self.assertEqual("prilis zlutoucky kun", normalize_search_text("Příliš žluťoučký kůň"))
        android = directory.search("android authority")
        self.assertTrue(android)
        self.assertTrue(all("android" in normalize_search_text(item.title + item.url) for item in android))
        self.assertEqual([], directory.search("   "))
        self.assertLessEqual(len(directory.search("rss")), 60)

    def test_search_variants_ranking_and_storefront_match_android_policy(self) -> None:
        self.assertEqual(
            ["srdce nevi", "srdce", "nevi"],
            podcast_search_variants("  srdce   nevi  "),
        )
        self.assertEqual(
            ["jeden dva tři čtyři pět", "jeden dva tři čtyři", "jeden", "čtyři"],
            podcast_search_variants("jeden dva tři čtyři pět"),
        )
        target = DirectoryEntry(
            "Srdce nevidomého dopraváka",
            "https://feeds.castos.com/79gk3",
            "Pavel Vlček",
        )
        candidates = [
            DirectoryEntry("Otcovo srdce pre Slovensko", "https://example.test/other"),
            DirectoryEntry("Nesouvisející výsledek", "https://example.test/noise", "Srdce"),
            target,
        ]
        for query in ("srdce", "srdce nevi", "srdce nevidomeho"):
            self.assertEqual(target, rank_podcast_entries(candidates, query)[0])
        self.assertEqual("GB", podcast_storefront_country("en-GB"))
        self.assertEqual("CZ", podcast_storefront_country("cs"))
        self.assertEqual("US", podcast_storefront_country("en"))
        self.assertEqual("en-CZ", podcast_search_locale("en", "cs-CZ"))
        self.assertEqual("cs-CZ", podcast_search_locale("cs", "en-GB"))

    def test_feed_url_canonicalization_merges_default_ports_and_schemes(self) -> None:
        self.assertEqual(
            podcast_feed_deduplication_key("HTTP://Example.com:80/feed/"),
            podcast_feed_deduplication_key("https://example.com/feed"),
        )
        self.assertEqual(
            "example.com/feed",
            podcast_feed_deduplication_key("https://EXAMPLE.com:443/feed/#ignored"),
        )
        self.assertNotEqual(
            podcast_feed_deduplication_key("http://example.com/feed"),
            podcast_feed_deduplication_key("http://example.com:443/feed"),
        )

    def test_remote_response_filters_bad_urls_deduplicates_and_limits(self) -> None:
        results = [
            {"feedUrl": "https://example.test/feed", "collectionName": "One"},
            {"feedUrl": "HTTPS://EXAMPLE.TEST:443/feed/", "collectionName": "Duplicate"},
            {"feedUrl": "file:///secret", "collectionName": "Unsafe"},
            {"feedUrl": "https://two.test/feed", "trackName": "Two", "artistName": "Author"},
        ]
        entries = parse_podcast_directory_response(json.dumps({"results": results}))
        self.assertEqual(["One", "Two"], [entry.title for entry in entries])
        self.assertEqual("Author", entries[1].detail)
        many = parse_podcast_directory_response(
            json.dumps(
                {
                    "results": [
                        {"feedUrl": f"https://example{i}.test/feed", "collectionName": str(i)}
                        for i in range(250)
                    ]
                }
            )
        )
        self.assertEqual(200, len(many))

    def test_combined_client_expands_queries_deduplicates_and_prefers_metadata(self) -> None:
        requested: list[str] = []
        generic = DirectoryEntry("Obecný záznam", "http://example.com:80/feed/")
        target = DirectoryEntry(
            "Srdce nevidomého dopraváka",
            "https://example.com/feed",
            "Pavel Vlček",
        )

        def source(url: str) -> list[DirectoryEntry]:
            requested.append(url)
            if urlsplit(url).hostname == "api.podcastindex.org":
                return [generic]
            query = parse_qs(urlsplit(url).query)["term"][0]
            return [target] if query == "srdce" else []

        entries = PodcastIndexDirectoryClient(
            source,
            api_key="key",
            api_secret="secret",
            clock=lambda: 1_000,
        ).search("srdce nevi", "cs-CZ")
        self.assertEqual([target], entries)
        self.assertEqual(
            ["srdce nevi", "srdce", "nevi"],
            [
                parse_qs(urlsplit(url).query)["term"][0]
                for url in requested
                if urlsplit(url).hostname == "itunes.apple.com"
            ],
        )

    def test_combined_client_surfaces_failed_fallback_without_useful_result(self) -> None:
        expected = OSError("Apple is unavailable")

        def source(url: str) -> list[DirectoryEntry]:
            if urlsplit(url).hostname == "api.podcastindex.org":
                return []
            raise expected

        with self.assertRaises(OSError) as caught:
            PodcastIndexDirectoryClient(source).search("srdce nevi", "cs-CZ")
        self.assertIs(expected, caught.exception)

    def test_podcast_index_uses_current_endpoint_and_signed_headers(self) -> None:
        source = _RecordingDirectorySource()
        client = PodcastIndexDirectoryClient(
            source,
            api_key="public",
            api_secret="private",
            clock=lambda: 1_234,
        )
        entries = client.search("accessible news", "en-US")
        index_url, headers = source.calls[0]
        self.assertEqual("/api/1.0/search/byterm", urlsplit(index_url).path)
        self.assertEqual(["accessible news"], parse_qs(urlsplit(index_url).query)["q"])
        self.assertEqual("1234", headers["X-Auth-Date"])
        self.assertEqual("public", headers["X-Auth-Key"])
        self.assertEqual(
            hashlib.sha1(b"publicprivate1234").hexdigest(),
            headers["Authorization"],
        )
        self.assertEqual("Podcast Index result", entries[0].title)

    def test_public_podcast_index_is_used_without_credentials(self) -> None:
        requested: list[str] = []

        def source(url: str) -> list[DirectoryEntry]:
            requested.append(url)
            return []

        PodcastIndexDirectoryClient(source, api_key="", api_secret="").search(
            "example", "en-US"
        )
        self.assertTrue(requested)
        public = requested[0]
        self.assertEqual("api.podcastindex.org", urlsplit(public).hostname)
        self.assertEqual("/search", urlsplit(public).path)
        self.assertEqual(["example"], parse_qs(urlsplit(public).query)["term"])
        self.assertTrue(
            any(urlsplit(url).hostname == "itunes.apple.com" for url in requested)
        )

    def test_parses_podcast_index_feed_shape(self) -> None:
        entries = parse_podcast_directory_response(
            json.dumps(
                {
                    "feeds": [
                        {
                            "url": "https://example.test/podcast.xml",
                            "title": "Accessible podcast",
                            "author": "Example author",
                        }
                    ]
                }
            )
        )
        self.assertEqual(
            [
                DirectoryEntry(
                    "Accessible podcast",
                    "https://example.test/podcast.xml",
                    "Example author",
                )
            ],
            entries,
        )

    def test_http_source_caches_success_and_enforces_stream_limit(self) -> None:
        response = _FakeResponse(
            200,
            json.dumps(
                {"results": [{"feedUrl": "https://feed.test/rss", "collectionName": "Feed"}]}
            ).encode(),
        )
        session = _FakeSession([response])
        source = HttpPodcastDirectorySource(session, cache_responses=True)
        source.clear_process_cache_for_tests()
        first = source.search("https://directory.test/search")
        second = source.search("https://directory.test/search")
        self.assertEqual(first, second)
        self.assertEqual(1, len(session.urls))
        self.assertTrue(response.closed)

        oversized = HttpPodcastDirectorySource(
            _FakeSession([_FakeResponse(200, b"123456")]),
            maximum_response_bytes=5,
            cache_responses=False,
        )
        with self.assertRaises(DirectoryTooLargeError):
            oversized.search("https://directory.test/search")

    def test_http_source_rejects_https_downgrade(self) -> None:
        response = _FakeResponse(302, b"", {"Location": "http://directory.test/plain"})
        source = HttpPodcastDirectorySource(
            _FakeSession([response]), cache_responses=False
        )
        with self.assertRaises(DirectoryRedirectError):
            source.search("https://directory.test/start")

    def test_cancelled_http_source_rejects_new_requests(self) -> None:
        session = _FakeSession([])
        source = HttpPodcastDirectorySource(session, cache_responses=False)
        source.cancel()
        with self.assertRaises(DirectoryNetworkError):
            source.search("https://directory.test/search")
        self.assertEqual([], session.urls)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.reason = "Test response"
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


class _RecordingDirectorySource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def search(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[DirectoryEntry]:
        self.calls.append((url, dict(headers or {})))
        if urlsplit(url).hostname == "api.podcastindex.org":
            return [
                DirectoryEntry(
                    "Podcast Index result",
                    "https://example.test/index.xml",
                )
            ]
        return []


if __name__ == "__main__":
    unittest.main()
