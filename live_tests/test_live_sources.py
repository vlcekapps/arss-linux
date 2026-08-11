from __future__ import annotations

import os
import unittest

from arss.directory import (
    PodcastIndexDirectoryClient,
    podcast_feed_deduplication_key,
)
from arss.feed import FeedClient
from arss.storage import CZECH_INITIAL_FEEDS, ENGLISH_INITIAL_FEEDS


LIVE_TESTS_ENABLED = os.environ.get("ARSS_RUN_LIVE_TESTS") == "1"


@unittest.skipUnless(
    LIVE_TESTS_ENABLED,
    "Set ARSS_RUN_LIVE_TESTS=1 to contact public endpoints",
)
class LiveSourceTest(unittest.TestCase):
    def test_all_localized_initial_feeds_are_accepted_end_to_end(self) -> None:
        subscriptions = CZECH_INITIAL_FEEDS + ENGLISH_INITIAL_FEEDS
        self.assertEqual(
            10,
            len({subscription.url for subscription in subscriptions}),
        )
        failures: list[str] = []

        with FeedClient() as client:
            for subscription in subscriptions:
                try:
                    feed = client.fetch(subscription.url)
                except Exception as error:
                    failures.append(
                        f"{subscription.title} ({subscription.url}): "
                        f"{type(error).__name__}: {error}"
                    )
                    continue
                if not feed.title.strip():
                    failures.append(f"{subscription.title}: feed title is blank")
                if not feed.articles:
                    failures.append(f"{subscription.title}: feed has no articles")
                elif any(
                    not article.url.lower().startswith(("http://", "https://"))
                    for article in feed.articles
                ):
                    failures.append(
                        f"{subscription.title}: an article has no absolute web URL"
                    )

        self.assertEqual([], failures, chr(10).join(failures))

    def test_tn_atom_regression_is_accepted_end_to_end(self) -> None:
        with FeedClient() as client:
            feed = client.fetch("https://tn.nova.cz/feed/atom/tnnova-2")

        self.assertTrue(feed.title.strip())
        self.assertTrue(feed.articles)
        self.assertTrue(
            all(
                article.url.startswith("https://tn.nova.cz/")
                for article in feed.articles
            )
        )
        self.assertFalse(
            any(article.url.lower().endswith(".jpg") for article in feed.articles)
        )
        self.assertTrue(
            any(article.published_at_millis is not None for article in feed.articles)
        )

    def test_podcast_directories_return_usable_ranked_results(self) -> None:
        client = PodcastIndexDirectoryClient(api_key="", api_secret="")
        try:
            results = client.search("news", "en-US")
            self.assertTrue(results)
            self.assertTrue(
                all(
                    item.url.lower().startswith(("http://", "https://"))
                    for item in results
                )
            )
            self.assertEqual(
                len(results),
                len(
                    {
                        podcast_feed_deduplication_key(item.url)
                        for item in results
                    }
                ),
            )

            regional = client.search("srdce nevi", "cs-CZ")
            self.assertTrue(regional)
            self.assertEqual("Srdce nevidomého dopraváka", regional[0].title)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
