from __future__ import annotations

from concurrent.futures import Future
import hashlib
import threading
import unittest

from arss.models import FeedArticle, FeedSubscription, ParsedFeed
from arss.monitor import (
    CheckpointEdge,
    CheckpointResult,
    CheckpointState,
    CheckpointStore,
    FeedMonitor,
    GLibMonitorScheduler,
    MAX_IDS_PER_FEED,
    MemoryCheckpointBackend,
    MonitorKind,
    NewFeedItem,
    article_identifiers,
    article_identity,
    evaluate_checkpoint,
)


class FakeSubscriptionStore:
    def __init__(self, *subscriptions: FeedSubscription) -> None:
        self.subscriptions = tuple(subscriptions)
        self.error: Exception | None = None

    def load(self) -> tuple[FeedSubscription, ...]:
        if self.error is not None:
            raise self.error
        return self.subscriptions


class SequenceFeedClient:
    def __init__(self, responses: dict[str, list[ParsedFeed | Exception]]) -> None:
        self.responses = responses
        self.indices: dict[str, int] = {}

    def fetch(self, url: str) -> ParsedFeed:
        index = self.indices.get(url, 0)
        choices = self.responses[url]
        response = choices[min(index, len(choices) - 1)]
        self.indices[url] = index + 1
        if isinstance(response, Exception):
            raise response
        return response


class BlockingFeedClient:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def fetch(self, url: str) -> ParsedFeed:
        if url == "https://example.test/rss":
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise RuntimeError("test timed out")
        return ParsedFeed("Feed", ())


class FakeGLib:
    def __init__(self) -> None:
        self.next_source = 1
        self.sources: dict[int, tuple[int, object, tuple[object, ...]]] = {}
        self.removed: list[int] = []

    def timeout_add_seconds(
        self,
        seconds: int,
        callback: object,
        *arguments: object,
    ) -> int:
        source = self.next_source
        self.next_source += 1
        self.sources[source] = (seconds, callback, arguments)
        return source

    def source_remove(self, source: object) -> None:
        source_id = int(source)
        self.removed.append(source_id)
        self.sources.pop(source_id, None)

    def fire(self, source: int) -> bool:
        _seconds, callback, arguments = self.sources[source]
        return callback(*arguments)  # type: ignore[operator]


class ImmediateExecutor:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def submit(self, function: object, *arguments: object) -> Future[object]:
        future: Future[object] = Future()
        try:
            result = function(*arguments)  # type: ignore[operator]
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures
        self.shutdown_calls += 1


class RecordingMonitor:
    def __init__(self) -> None:
        self.kinds: list[MonitorKind] = []

    def run_once(self, kind: MonitorKind) -> tuple[NewFeedItem, ...]:
        self.kinds.append(kind)
        return ()


class RecordingRunGuard:
    def __init__(self, available: bool) -> None:
        self.available = available
        self.acquired: list[MonitorKind] = []
        self.released: list[MonitorKind] = []

    def try_acquire(self, kind: MonitorKind) -> bool:
        self.acquired.append(kind)
        return self.available

    def release(self, kind: MonitorKind) -> None:
        self.released.append(kind)


def article(
    title: str,
    *,
    url: str = "",
    source_id: str | None = None,
    media_url: str | None = None,
    published_text: str | None = None,
) -> FeedArticle:
    return FeedArticle(
        title=title,
        url=url,
        source_id=source_id,
        media_url=media_url,
        published_text=published_text,
    )


class ArticleIdentityTest(unittest.TestCase):
    def test_identity_precedence_is_source_then_article_then_media_then_text(self) -> None:
        self.assertEqual(
            "id:guid",
            article_identity(
                article(
                    "Title",
                    source_id=" guid ",
                    url="https://example.test/article",
                    media_url="https://example.test/audio.mp3",
                )
            ),
        )
        identifiers = article_identifiers(
            article(
                "Title",
                url="https://example.test/article",
                media_url="https://example.test/audio.mp3",
            )
        )
        self.assertEqual("url:https://example.test/article", identifiers.primary)
        self.assertEqual(
            frozenset({"media:https://example.test/audio.mp3"}),
            identifiers.legacy_aliases,
        )
        self.assertEqual(
            "media:https://example.test/audio.mp3",
            article_identity(article("Title", media_url="https://example.test/audio.mp3")),
        )

        digest = hashlib.sha256("Title\x1fToday".encode()).hexdigest()
        self.assertEqual(
            f"text:{digest}",
            article_identity(article("Title", published_text="Today")),
        )

    def test_long_inline_identity_is_hashed_but_legacy_alias_remains_recognizable(self) -> None:
        long_id = "x" * 513

        identifiers = article_identifiers(article("Title", source_id=long_id))

        self.assertTrue(identifiers.primary.startswith("id-hash:"))
        self.assertEqual(72, len(identifiers.primary))
        self.assertEqual(frozenset({f"id:{long_id}"}), identifiers.legacy_aliases)


class CheckpointPolicyTest(unittest.TestCase):
    def test_first_success_is_silent_then_only_new_ids_are_reported(self) -> None:
        store = CheckpointStore()

        baseline = store.record_success(MonitorKind.RSS, "feed", ["old-1", "old-2"])
        second = store.record_success(
            MonitorKind.RSS,
            "feed",
            ["new", "old-1", "old-2"],
        )

        self.assertTrue(baseline.is_baseline)
        self.assertEqual((), baseline.new_ids)
        self.assertFalse(second.is_baseline)
        self.assertEqual(("new",), second.new_ids)

    def test_backend_atomic_compare_and_commit_is_preferred(self) -> None:
        class AtomicBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def record_success_atomic(self, *arguments: object) -> CheckpointResult:
                self.calls.append(arguments)
                return CheckpointResult(False, ("atomic-new",))

            def load(self, *_arguments: object) -> CheckpointState | None:
                raise AssertionError("non-atomic load must not be used")

            def save(self, *_arguments: object) -> None:
                raise AssertionError("non-atomic save must not be used")

        backend = AtomicBackend()
        result = CheckpointStore(backend).record_success(
            MonitorKind.RSS,
            " feed ",
            (identity for identity in ("new", "old")),
        )

        self.assertEqual(("atomic-new",), result.new_ids)
        self.assertEqual(1, len(backend.calls))
        self.assertEqual(MonitorKind.RSS, backend.calls[0][0])
        self.assertEqual("feed", backend.calls[0][1])
        self.assertEqual(("new", "old"), backend.calls[0][2])

    def test_rss_and_podcast_checkpoints_are_separate_even_for_same_url(self) -> None:
        store = CheckpointStore()

        rss = store.record_success(MonitorKind.RSS, "feed", ["one"])
        podcast = store.record_success(MonitorKind.PODCAST, "feed", ["one"])

        self.assertTrue(rss.is_baseline)
        self.assertTrue(podcast.is_baseline)

    def test_legacy_alias_migrates_without_a_false_discovery(self) -> None:
        backend = MemoryCheckpointBackend()
        backend.save(
            MonitorKind.PODCAST,
            "feed",
            CheckpointState(("media:https://example.test/episode.mp3",)),
        )
        store = CheckpointStore(backend)

        result = store.record_success(
            MonitorKind.PODCAST,
            "feed",
            ["url:https://example.test/show-notes"],
            {
                "url:https://example.test/show-notes": {
                    "media:https://example.test/episode.mp3"
                }
            },
        )

        self.assertEqual((), result.new_ids)
        state = backend.load(MonitorKind.PODCAST, "feed")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(("url:https://example.test/show-notes",), state.ids)

    def test_long_feed_is_bounded_and_learns_the_new_item_edge(self) -> None:
        identities = [f"item-{index}" for index in range(300)]
        store = CheckpointStore()

        baseline = store.record_success(MonitorKind.RSS, "long", identities)
        first_new = store.record_success(
            MonitorKind.RSS,
            "long",
            ["new-1", *identities],
        )
        second_new = store.record_success(
            MonitorKind.RSS,
            "long",
            ["new-2", "new-1", *identities],
        )

        self.assertTrue(baseline.is_baseline)
        self.assertEqual(("new-1",), first_new.new_ids)
        self.assertEqual(("new-2",), second_new.new_ids)
        backend = store.backend
        assert isinstance(backend, MemoryCheckpointBackend)
        state = backend.load(MonitorKind.RSS, "long")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertLessEqual(len(state.ids), MAX_IDS_PER_FEED)
        self.assertEqual(CheckpointEdge.START, state.edge)
        self.assertFalse(state.complete)

    def test_unknown_middle_of_long_feed_is_learned_silently(self) -> None:
        identities = [f"item-{index}" for index in range(300)]
        baseline = evaluate_checkpoint(
            has_baseline=False,
            previous_ids=(),
            current_ids=identities,
        )

        decision = evaluate_checkpoint(
            has_baseline=True,
            previous_ids=baseline.retained_ids,
            previous_complete=baseline.complete,
            previous_edge=baseline.edge,
            current_ids=[*identities[:150], "unknown-middle", *identities[150:]],
        )

        self.assertEqual((), decision.new_ids)
        self.assertEqual(CheckpointEdge.UNKNOWN, decision.edge)


class FeedMonitorTest(unittest.TestCase):
    def test_cross_process_guard_skips_without_fetch_and_releases_after_run(self) -> None:
        subscription = FeedSubscription("News", "https://example.test/rss")
        client = SequenceFeedClient(
            {subscription.url: [ParsedFeed("News", ())]}
        )
        unavailable = RecordingRunGuard(False)
        monitor = FeedMonitor(
            client,
            FakeSubscriptionStore(subscription),
            FakeSubscriptionStore(),
            run_guard=unavailable,
        )

        self.assertEqual((), monitor.run_rss())
        self.assertEqual({}, client.indices)
        self.assertEqual([], unavailable.released)

        available = RecordingRunGuard(True)
        monitor.run_guard = available
        self.assertEqual((), monitor.run_rss())
        self.assertEqual(1, client.indices[subscription.url])
        self.assertEqual([MonitorKind.RSS], available.released)

    def test_monitor_baselines_silently_then_notifies_one_batch(self) -> None:
        subscription = FeedSubscription("News", "https://example.test/rss")
        old = article("Old", source_id="old", url="https://example.test/old")
        new = article("New", source_id="new", url="https://example.test/new")
        client = SequenceFeedClient(
            {
                subscription.url: [
                    ParsedFeed("News", (old,)),
                    ParsedFeed("News", (new, old)),
                ]
            }
        )
        notifications: list[tuple[MonitorKind, tuple[NewFeedItem, ...]]] = []
        monitor = FeedMonitor(
            client,
            FakeSubscriptionStore(subscription),
            FakeSubscriptionStore(),
            notification_callback=lambda kind, items: notifications.append((kind, items)),
        )

        self.assertEqual((), monitor.run_rss())
        discoveries = monitor.run_rss()

        self.assertEqual(["New"], [item.article.title for item in discoveries])
        self.assertEqual([(MonitorKind.RSS, discoveries)], notifications)

    def test_podcast_pass_ignores_items_without_audio(self) -> None:
        subscription = FeedSubscription("Show", "https://example.test/podcast")
        old = article(
            "Old episode",
            source_id="old",
            media_url="https://example.test/old.mp3",
        )
        text_only = article("Article", source_id="article")
        new = article(
            "New episode",
            source_id="new",
            media_url="https://example.test/new.mp3",
        )
        client = SequenceFeedClient(
            {
                subscription.url: [
                    ParsedFeed("Show", (old, text_only)),
                    ParsedFeed("Show", (new, text_only, old)),
                ]
            }
        )
        monitor = FeedMonitor(
            client,
            FakeSubscriptionStore(),
            FakeSubscriptionStore(subscription),
        )

        self.assertEqual((), monitor.run_podcasts())
        discoveries = monitor.run_podcasts()

        self.assertEqual(["New episode"], [item.article.title for item in discoveries])

    def test_broken_feed_does_not_stop_remaining_subscriptions(self) -> None:
        broken = FeedSubscription("Broken", "https://example.test/broken")
        working = FeedSubscription("Working", "https://example.test/working")
        errors: list[tuple[MonitorKind, FeedSubscription | None, BaseException]] = []
        client = SequenceFeedClient(
            {
                broken.url: [RuntimeError("offline")],
                working.url: [ParsedFeed("Working", ())],
            }
        )
        monitor = FeedMonitor(
            client,
            FakeSubscriptionStore(broken, working),
            FakeSubscriptionStore(),
            error_callback=lambda kind, subscription, error: errors.append(
                (kind, subscription, error)
            ),
        )

        self.assertEqual((), monitor.run_rss())

        self.assertEqual(1, client.indices[working.url])
        self.assertEqual([(MonitorKind.RSS, broken)], [(k, s) for k, s, _e in errors])

    def test_rss_and_podcast_runs_do_not_share_a_busy_guard(self) -> None:
        client = BlockingFeedClient()
        monitor = FeedMonitor(
            client,
            FakeSubscriptionStore(
                FeedSubscription("RSS", "https://example.test/rss")
            ),
            FakeSubscriptionStore(
                FeedSubscription("Podcast", "https://example.test/podcast")
            ),
        )
        worker = threading.Thread(target=monitor.run_rss)
        worker.start()
        self.assertTrue(client.entered.wait(timeout=1))

        try:
            self.assertTrue(monitor.is_running(MonitorKind.RSS))
            self.assertFalse(monitor.is_running(MonitorKind.PODCAST))
            self.assertEqual((), monitor.run_podcasts())
            self.assertEqual((), monitor.run_rss())
        finally:
            client.release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


class GLibMonitorSchedulerTest(unittest.TestCase):
    def test_scheduler_exists_only_between_start_and_stop(self) -> None:
        glib = FakeGLib()
        executor = ImmediateExecutor()
        monitor = RecordingMonitor()
        intervals: dict[MonitorKind, int] = {
            MonitorKind.RSS: 1,
            MonitorKind.PODCAST: 0,
        }
        scheduler = GLibMonitorScheduler(
            monitor,  # type: ignore[arg-type]
            intervals,
            glib=glib,
            executor=executor,
        )

        self.assertFalse(scheduler.run_now(MonitorKind.RSS))
        scheduler.start()

        self.assertEqual(frozenset({MonitorKind.RSS}), scheduler.scheduled_kinds)
        source = next(iter(glib.sources))
        self.assertEqual(60, glib.sources[source][0])
        self.assertTrue(glib.fire(source))
        self.assertEqual([MonitorKind.RSS], monitor.kinds)

        intervals[MonitorKind.RSS] = 0
        intervals[MonitorKind.PODCAST] = 2
        scheduler.refresh()
        self.assertEqual(frozenset({MonitorKind.PODCAST}), scheduler.scheduled_kinds)
        self.assertIn(source, glib.removed)

        remaining_source = next(iter(glib.sources))
        scheduler.stop()
        self.assertFalse(scheduler.running)
        self.assertIn(remaining_source, glib.removed)
        self.assertFalse(scheduler.run_now(MonitorKind.PODCAST))

    def test_immediate_runs_only_happen_after_start(self) -> None:
        monitor = RecordingMonitor()
        scheduler = GLibMonitorScheduler(
            monitor,  # type: ignore[arg-type]
            {MonitorKind.RSS: 1, MonitorKind.PODCAST: 1},
            glib=FakeGLib(),
            executor=ImmediateExecutor(),
            run_immediately=True,
        )

        self.assertEqual([], monitor.kinds)
        scheduler.start()
        self.assertEqual([MonitorKind.RSS, MonitorKind.PODCAST], monitor.kinds)
        scheduler.stop()


if __name__ == "__main__":
    unittest.main()
