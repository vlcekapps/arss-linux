from __future__ import annotations

import unittest

from arss.i18n import Translator
from arss.models import FeedArticle, FeedSubscription
from arss.monitor import MonitorKind, NewFeedItem
from arss.notifications import (
    GNotificationPublisher,
    MAX_ITEM_NOTIFICATIONS,
    NotificationPriority,
    build_notification_batch,
)


def feed_item(
    index: int,
    *,
    kind: MonitorKind = MonitorKind.RSS,
    media_url: str | None = None,
) -> NewFeedItem:
    return NewFeedItem(
        kind=kind,
        subscription=FeedSubscription(
            title="Example source",
            url="https://example.test/feed.xml",
        ),
        article=FeedArticle(
            title=f"Item {index}",
            url=f"https://example.test/items/{index}",
            media_url=media_url,
            duration_text="12:34" if media_url else None,
        ),
        stable_id=f"id:item-{index}",
    )


class FakeVariant:
    def __init__(self, signature: str, value: object) -> None:
        self.signature = signature
        self.value = value


class FakeGLib:
    Variant = FakeVariant


class FakeNotification:
    def __init__(self, title: str) -> None:
        self.title = title
        self.body = ""
        self.priority: object = None
        self.action = ""
        self.target: FakeVariant | None = None

    @classmethod
    def new(cls, title: str) -> FakeNotification:
        return cls(title)

    def set_body(self, body: str) -> None:
        self.body = body

    def set_priority(self, priority: object) -> None:
        self.priority = priority

    def set_default_action_and_target(
        self,
        action: str,
        target: FakeVariant,
    ) -> None:
        self.action = action
        self.target = target


class FakeGio:
    Notification = FakeNotification

    class NotificationPriority:
        LOW = "low"
        NORMAL = "normal"


class FakeApplication:
    def __init__(self) -> None:
        self.sent: list[tuple[str, FakeNotification]] = []
        self.withdrawn: list[str] = []

    def send_notification(
        self,
        identifier: str,
        notification: FakeNotification,
    ) -> None:
        self.sent.append((identifier, notification))

    def withdraw_notification(self, identifier: str) -> None:
        self.withdrawn.append(identifier)


class NotificationBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Translator("en")

    def test_eight_stable_item_notifications_and_low_priority_summary(self) -> None:
        items = tuple(feed_item(index) for index in range(10))

        first = build_notification_batch(MonitorKind.RSS, items, self.t)
        second = build_notification_batch(MonitorKind.RSS, items, self.t)

        self.assertEqual(MAX_ITEM_NOTIFICATIONS, len(first.items))
        self.assertEqual(
            [item.identifier for item in first.items],
            [item.identifier for item in second.items],
        )
        self.assertIsNotNone(first.summary)
        summary = first.summary
        assert summary is not None
        self.assertEqual("New articles: 10", summary.title)
        self.assertEqual(NotificationPriority.LOW, summary.priority)
        self.assertEqual("app.show-kind", summary.action.name)
        self.assertEqual("rss", summary.action.value)
        self.assertEqual(NotificationPriority.NORMAL, first.items[0].priority)
        self.assertTrue(
            all(
                item.priority is NotificationPriority.LOW
                for item in first.items[1:]
            )
        )
        self.assertEqual("app.open-article", first.items[0].action.name)

    def test_single_item_has_no_duplicate_summary_but_multiple_items_do(self) -> None:
        single = build_notification_batch(
            MonitorKind.RSS,
            (feed_item(1),),
            self.t,
        )
        multiple = build_notification_batch(
            MonitorKind.RSS,
            (feed_item(1), feed_item(2)),
            self.t,
        )

        self.assertIsNone(single.summary)
        self.assertEqual(1, len(single.notifications))
        self.assertIsNotNone(multiple.summary)
        self.assertEqual(3, len(multiple.notifications))

    def test_blank_item_titles_use_explicit_localized_fallbacks(self) -> None:
        subscription = FeedSubscription(
            "Source",
            "https://example.test/feed.xml",
        )
        article = FeedArticle("", "https://example.test/item")
        rss_item = NewFeedItem(
            MonitorKind.RSS, subscription, article, "blank-rss"
        )
        podcast_item = NewFeedItem(
            MonitorKind.PODCAST, subscription, article, "blank-podcast"
        )

        rss = build_notification_batch(MonitorKind.RSS, (rss_item,), self.t)
        podcast = build_notification_batch(
            MonitorKind.PODCAST,
            (podcast_item,),
            self.t,
        )

        self.assertEqual("The article title is unavailable.", rss.items[0].title)
        self.assertEqual("The episode title is unavailable.", podcast.items[0].title)
        self.assertNotEqual("1 new article", rss.items[0].title)

    def test_system_notification_priority_avoids_repeated_prominent_alerts(self) -> None:
        batch = build_notification_batch(
            MonitorKind.RSS,
            (feed_item(1), feed_item(2), feed_item(3)),
            self.t,
        )

        self.assertEqual(NotificationPriority.NORMAL, batch.items[0].priority)
        self.assertEqual(
            [NotificationPriority.LOW, NotificationPriority.LOW],
            [item.priority for item in batch.items[1:]],
        )
        assert batch.summary is not None
        self.assertEqual(NotificationPriority.LOW, batch.summary.priority)

    def test_podcast_item_opens_the_episode_player_with_full_target(self) -> None:
        item = feed_item(
            4,
            kind=MonitorKind.PODCAST,
            media_url="https://example.test/audio/4.mp3",
        )

        batch = build_notification_batch(
            MonitorKind.PODCAST,
            (item,),
            self.t,
        )

        action = batch.items[0].action
        self.assertEqual("app.play-episode", action.name)
        self.assertEqual("(ssss)", action.signature)
        self.assertEqual("https://example.test/audio/4.mp3", action.value[2])

    def test_invalid_or_unplayable_item_falls_back_to_kind_page(self) -> None:
        item = NewFeedItem(
            kind=MonitorKind.PODCAST,
            subscription=FeedSubscription("Feed", "https://example.test/feed"),
            article=FeedArticle("Episode", "not-a-url", media_url=None),
            stable_id="id:episode",
        )

        batch = build_notification_batch(
            MonitorKind.PODCAST,
            (item,),
            self.t,
        )

        self.assertEqual("app.show-kind", batch.items[0].action.name)
        self.assertEqual("podcast", batch.items[0].action.value)

    def test_rejects_empty_or_mismatched_batches_and_negative_limit(self) -> None:
        with self.assertRaises(ValueError):
            build_notification_batch(MonitorKind.RSS, (), self.t)
        with self.assertRaises(ValueError):
            build_notification_batch(
                MonitorKind.RSS,
                (feed_item(1, kind=MonitorKind.PODCAST),),
                self.t,
            )
        with self.assertRaises(ValueError):
            build_notification_batch(
                MonitorKind.RSS,
                (feed_item(1),),
                self.t,
                maximum_items=-1,
            )


class GNotificationPublisherTest(unittest.TestCase):
    def test_publishes_items_and_summary_without_application_audio(self) -> None:
        application = FakeApplication()
        publisher = GNotificationPublisher(
            application,
            gio=FakeGio,
            glib=FakeGLib,
        )
        items = (feed_item(1), feed_item(2))

        batch = publisher.publish_feed_updates(
            MonitorKind.RSS,
            items,
            Translator("en"),
        )

        self.assertEqual(3, len(application.sent))
        self.assertIsNotNone(batch.summary)
        assert batch.summary is not None
        self.assertEqual(batch.summary.identifier, application.sent[-1][0])
        first = application.sent[0][1]
        self.assertEqual("app.open-article", first.action)
        assert first.target is not None
        self.assertEqual("s", first.target.signature)

    def test_single_item_withdraws_stale_summary(self) -> None:
        application = FakeApplication()
        publisher = GNotificationPublisher(
            application,
            gio=FakeGio,
            glib=FakeGLib,
        )

        publisher.publish_feed_updates(
            MonitorKind.RSS,
            (feed_item(1),),
            Translator("en"),
        )

        self.assertEqual(["arss-rss-summary"], application.withdrawn)


if __name__ == "__main__":
    unittest.main()
