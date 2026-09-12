from __future__ import annotations

from pathlib import Path
import unittest

from arss.aggregate import (
    AggregateResult, SourcedArticle, load_all, offers_all, order_all,
    sort_preference_key, source_name_key,
)
from arss.models import FeedArticle, FeedSubscription, ParsedFeed
from arss.storage import DEFAULT_PREFERENCES, PreferencesError, _validate_preference


def entry(source: str, date: int | None, title: str = "Headline", *,
          source_url: str | None = None, media: str | None = None) -> SourcedArticle:
    return SourcedArticle(
        FeedSubscription(source, source_url or f"https://example.test/{source}"),
        FeedArticle(title, "https://example.test/article", source_id="same-id",
                    published_at_millis=date, media_url=media),
    )


class AggregateTest(unittest.TestCase):
    def test_all_threshold_is_two_for_both_kinds(self) -> None:
        for count in (0, 1, 2, 3):
            self.assertEqual(count >= 2, offers_all(count))
        self.assertEqual("rss_all_sort", sort_preference_key("rss"))
        self.assertEqual("podcast_all_sort", sort_preference_key("podcast"))
        with self.assertRaises(ValueError):
            sort_preference_key("unknown")

    def test_date_default_is_newest_first_and_unknown_last(self) -> None:
        entries = [entry("tn", None), entry("idnes", 10), entry("tn", 20)]
        self.assertEqual([20, 10, None], [e.article.published_at_millis for e in order_all(entries)])
        self.assertEqual(order_all(entries), order_all(entries, "invalid"))

    def test_source_sort_groups_actual_feed_then_newest_first(self) -> None:
        entries = [entry("tn", 30), entry("idnes", 10), entry("idnes", None), entry("idnes", 20)]
        ordered = order_all(entries, "source")
        self.assertEqual(["idnes", "idnes", "idnes", "tn"], [e.source.title for e in ordered])
        self.assertEqual([20, 10, None, 30], [e.article.published_at_millis for e in ordered])

    def test_identical_titles_still_form_separate_source_groups(self) -> None:
        a, b = "https://example.test/a", "https://example.test/b"
        entries = [entry("Same", 40, source_url=b), entry("Same", 10, source_url=a),
                   entry("Same", 30, source_url=b), entry("Same", 20, source_url=a)]
        self.assertEqual([a, a, b, b], [e.source.url for e in order_all(entries, "source")])

    def test_czech_and_english_alphabetic_order_are_language_aware(self) -> None:
        names = ["Chrudim", "Idnes", "Český", "Hudba", "CNN", "čtení"]
        self.assertEqual(["CNN", "Český", "čtení", "Hudba", "Chrudim", "Idnes"],
                         sorted(names, key=lambda name: source_name_key(name, "cs")))
        self.assertEqual(["Český", "Chrudim", "CNN", "čtení", "Hudba", "Idnes"],
                         sorted(names, key=lambda name: source_name_key(name, "en")))

    def test_ties_are_deterministic_independent_of_fetch_order(self) -> None:
        entries = [entry("tn", 10, "Z"), entry("idnes", 10, "B"), entry("idnes", 10, "A")]
        for sort in ("date", "source"):
            self.assertEqual(order_all(entries, sort), order_all(reversed(entries), sort))

    def test_partial_failure_keeps_actual_source_and_same_ids_from_different_feeds(self) -> None:
        sources = [FeedSubscription(name, f"https://example.test/{name}") for name in ("a", "bad", "b")]
        article = entry("source", 10).article
        def fetch(url: str) -> ParsedFeed:
            if url.endswith("bad"):
                raise OSError("offline")
            return ParsedFeed("Publisher name", (article,))
        result = load_all(sources, fetch, "rss")
        self.assertEqual((sources[1],), result.failed)
        self.assertEqual(2, len(result.items))
        self.assertEqual(["a", "b"], [item.source.title for item in result.items])
        self.assertNotEqual(result.items[0].identity, result.items[1].identity)
        self.assertIs(article, result.items[0].article)
        self.assertEqual(3, len(sources))

    def test_podcast_filter_keeps_playable_original_media(self) -> None:
        source = FeedSubscription("Podcast", "https://example.test/feed")
        playable = entry("a", 10, media="https://example.test/episode.mp3").article
        result = load_all([source], lambda _: ParsedFeed("Podcast", (
            playable, entry("a", 5).article, entry("a", 1, media="  ").article,
        )), "podcast")
        self.assertEqual(1, len(result.items))
        self.assertIs(playable, result.items[0].article)
        self.assertEqual(3, len(result.loaded[0][1].articles))

    def test_empty_sources_and_every_source_failing_are_distinct(self) -> None:
        self.assertEqual(AggregateResult((), (), ()), load_all([], lambda _: None, "rss"))
        source = FeedSubscription("Broken", "https://example.test/feed")
        def broken(_: str) -> ParsedFeed:
            raise ValueError("invalid XML")
        result = load_all([source], broken, "rss")
        self.assertEqual((source,), result.failed)
        self.assertEqual((), result.loaded)

    def test_closing_stops_fetching_remaining_sources(self) -> None:
        calls: list[str] = []
        sources = [FeedSubscription(str(i), f"https://example.test/{i}") for i in range(3)]
        def fetch(url: str) -> ParsedFeed:
            calls.append(url)
            return ParsedFeed("Feed", ())
        result = load_all(sources, fetch, "rss", cancelled=lambda: len(calls) == 1)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(result.loaded))

    def test_independent_preferences_default_to_date_and_reject_invalid_values(self) -> None:
        for kind in ("rss", "podcast"):
            key = sort_preference_key(kind)
            self.assertEqual("date", DEFAULT_PREFERENCES[key])
            for value in ("date", "source"):
                _validate_preference(key, value)
            for value in (None, True, "alphabetical", 2, ["date"]):
                with self.assertRaises(PreferencesError):
                    _validate_preference(key, value)

    def test_virtual_row_is_not_a_subscription_and_is_packaged(self) -> None:
        root = Path(__file__).resolve().parent.parent
        ui = (root / "arss" / "ui.py").read_text(encoding="utf-8")
        all_body = ui.split("    def _open_all(self)", 1)[1].split("    def _open_default", 1)[0]
        self.assertIn("ItemsWindow(self.window, self.kind, None)", all_body)
        self.assertNotIn("FeedSubscription(", all_body)
        self.assertIn("'arss/aggregate.py'", (root / "meson.build").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
