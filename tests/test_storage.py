from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from arss.models import FeedSubscription
from arss.storage import (
    CZECH_INITIAL_FEEDS,
    ENGLISH_INITIAL_FEEDS,
    FeedStore,
    JsonPreferences,
    PodcastStore,
    PreferencesError,
    StorageReadError,
    StorageWriteError,
    XdgPaths,
    initial_rss_feeds,
)


class XdgPathsTest(unittest.TestCase):
    def test_explicit_absolute_xdg_homes_are_used(self) -> None:
        paths = XdgPaths.from_environment(
            {
                "XDG_DATA_HOME": "/var/tmp/test-data",
                "XDG_CONFIG_HOME": "/var/tmp/test-config",
            },
            home=Path("/unused"),
        )

        self.assertEqual(Path("/var/tmp/test-data/arss"), paths.data_dir)
        self.assertEqual(Path("/var/tmp/test-config/arss"), paths.config_dir)
        self.assertEqual("readFeeds.opml", paths.rss_opml.name)
        self.assertEqual("podcasts.opml", paths.podcasts_opml.name)
        self.assertEqual("preferences.json", paths.preferences_json.name)

    def test_relative_xdg_homes_fall_back_to_home(self) -> None:
        paths = XdgPaths.from_environment(
            {"XDG_DATA_HOME": "relative", "XDG_CONFIG_HOME": ""},
            home=Path("/home/tester"),
        )

        self.assertEqual(Path("/home/tester/.local/share/arss"), paths.data_dir)
        self.assertEqual(Path("/home/tester/.config/arss"), paths.config_dir)


class JsonPreferencesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "config" / "preferences.json"
        self.preferences = JsonPreferences(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_defaults_and_typed_properties_round_trip_atomically(self) -> None:
        self.assertEqual("system", self.preferences.language)
        self.assertFalse(self.preferences.background_checks_enabled)
        self.assertEqual(0, self.preferences.rss_check_interval_minutes)

        self.preferences.update(
            language="cs",
            show_article_dates=True,
            rss_check_interval_minutes=15,
            default_feed_url="https://example.test/feed",
            background_checks_enabled=True,
        )

        reloaded = JsonPreferences(self.path)
        self.assertEqual("cs", reloaded.language)
        self.assertTrue(reloaded.show_article_dates)
        self.assertEqual(15, reloaded.rss_check_interval_minutes)
        self.assertEqual("https://example.test/feed", reloaded.default_feed_url)
        self.assertTrue(reloaded.background_checks_enabled)
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))
        self.assertEqual("cs", json.loads(self.path.read_text())["language"])

    def test_invalid_values_and_corrupt_json_are_typed_errors(self) -> None:
        with self.assertRaises(PreferencesError):
            self.preferences.language = "de"
        with self.assertRaises(PreferencesError):
            self.preferences.rss_check_interval_minutes = 2
        with self.assertRaises(PreferencesError):
            self.preferences.default_feed_url = "file:///tmp/feed"
        with self.assertRaises(PreferencesError):
            self.preferences.background_checks_enabled = 1  # type: ignore[assignment]

        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            '{"language":"de","rss_check_interval_minutes":2}',
            encoding="utf-8",
        )
        self.assertEqual("system", self.preferences.language)
        self.assertEqual(0, self.preferences.rss_check_interval_minutes)
        self.preferences.show_article_dates = True
        self.assertTrue(self.preferences.show_article_dates)

        self.path.write_text('{"unknown":NaN}', encoding="utf-8")
        with self.assertRaises(PreferencesError):
            self.preferences.load()

        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PreferencesError):
            self.preferences.load()


    def test_legacy_sound_preferences_are_removed_during_migration(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "language": "cs",
                    "rss_notification_sound": "rss",
                    "podcast_notification_sound": "alert1",
                }
            ),
            encoding="utf-8",
        )

        loaded = self.preferences.load()

        self.assertEqual("cs", loaded["language"])
        self.assertNotIn("rss_notification_sound", loaded)
        self.assertNotIn("podcast_notification_sound", loaded)
        self.preferences.show_article_dates = True
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("rss_notification_sound", persisted)
        self.assertNotIn("podcast_notification_sound", persisted)
        with self.assertRaises(PreferencesError):
            self.preferences.set("rss_notification_sound", "system")


class SubscriptionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = XdgPaths(root / "data", root / "config")
        self.preferences = JsonPreferences(paths=self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_czech_load_seeds_once_and_sets_default(self) -> None:
        self.preferences.language = "cs"
        store = FeedStore(paths=self.paths, preferences=self.preferences)

        first = store.load()
        second = store.load()

        self.assertEqual(CZECH_INITIAL_FEEDS, first)
        self.assertEqual(first, second)
        self.assertTrue(self.preferences.rss_store_initialized)
        self.assertEqual(first[0].url, store.default_feed_url)
        self.assertTrue(self.paths.rss_opml.is_file())
        self.assertEqual(0o600, stat.S_IMODE(self.paths.rss_opml.stat().st_mode))

    def test_system_language_selects_english_or_czech(self) -> None:
        self.assertEqual(CZECH_INITIAL_FEEDS, initial_rss_feeds("system", "cs_CZ"))
        self.assertEqual(ENGLISH_INITIAL_FEEDS, initial_rss_feeds("system", "en_US"))
        self.assertEqual(ENGLISH_INITIAL_FEEDS, initial_rss_feeds("en", "cs_CZ"))

    def test_deleted_or_missing_initialized_store_is_never_reseeded(self) -> None:
        store = FeedStore(
            paths=self.paths,
            preferences=self.preferences,
            system_language="cs_CZ",
        )
        seeded = store.load()
        self.assertEqual(5, len(seeded))
        store.save(())
        self.assertEqual((), store.load())

        store.default_feed_url = seeded[0].url
        self.paths.rss_opml.unlink()
        self.assertEqual((), store.load())
        self.assertIsNone(store.default_feed_url)
        self.assertTrue(self.paths.rss_opml.is_file())

    def test_existing_store_is_loaded_without_seeding(self) -> None:
        existing = (FeedSubscription("Mine", "https://example.test/feed"),)
        podcasts = PodcastStore(self.paths.rss_opml)
        podcasts.save(existing)
        store = FeedStore(paths=self.paths, preferences=self.preferences)

        self.assertEqual(existing, store.load())
        self.assertTrue(self.preferences.rss_store_initialized)
        self.assertIsNone(store.default_feed_url)

    def test_podcast_store_starts_empty_and_round_trips(self) -> None:
        store = PodcastStore(paths=self.paths)
        expected = (FeedSubscription("Podcast", "https://pod.example/feed"),)

        self.assertEqual((), store.load())
        store.save(expected)
        self.assertEqual(expected, store.load())

    def test_corrupt_opml_is_not_silently_replaced(self) -> None:
        self.paths.podcasts_opml.parent.mkdir(parents=True)
        self.paths.podcasts_opml.write_bytes(b"<opml><broken>")

        with self.assertRaises(StorageReadError):
            PodcastStore(paths=self.paths).load()
        self.assertEqual(b"<opml><broken>", self.paths.podcasts_opml.read_bytes())

    def test_failed_atomic_replace_preserves_old_store_and_removes_temp(self) -> None:
        store = PodcastStore(paths=self.paths)
        old = (FeedSubscription("Old", "https://old.example/feed"),)
        store.save(old)

        with patch("arss.storage.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(StorageWriteError):
                store.save((FeedSubscription("New", "https://new.example/feed"),))

        self.assertEqual(old, store.load())
        self.assertEqual([], list(self.paths.data_dir.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
