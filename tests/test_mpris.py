from __future__ import annotations

from collections.abc import Callable, Mapping
import unittest

from arss.mpris import (
    MICROSECONDS_PER_MILLISECOND,
    MPRIS_PLAYER_INTERFACE,
    MprisService,
    build_mpris_snapshot,
    track_id_for_metadata,
)
from arss.playback import (
    BackendEvent,
    PlaybackMetadata,
    PlaybackPhase,
    PodcastPlayer,
)


class FakeBackend:
    def __init__(self) -> None:
        self.callback: Callable[[BackendEvent, str | None], None] = (
            lambda _event, _detail: None
        )
        self.prepared: list[str] = []
        self.play_calls = 0
        self.pause_calls = 0
        self.seek_calls: list[int] = []
        self.speed_calls: list[float] = []
        self.position = 0
        self.duration = 90_000
        self.ready_during_prepare = False
        self.volume_calls: list[float] = []

    def set_event_callback(
        self,
        callback: Callable[[BackendEvent, str | None], None],
    ) -> None:
        self.callback = callback

    def prepare(self, uri: str) -> None:
        self.prepared.append(uri)
        if self.ready_during_prepare:
            self.callback(BackendEvent.READY, None)

    def play(self) -> None:
        self.play_calls += 1

    def pause(self) -> None:
        self.pause_calls += 1

    def seek(self, position_ms: int) -> None:
        self.position = position_ms
        self.seek_calls.append(position_ms)

    def set_speed(self, speed: float) -> None:
        self.speed_calls.append(speed)

    def set_volume(self, volume: float) -> None:
        self.volume_calls.append(volume)

    def set_metadata(self, _metadata: PlaybackMetadata) -> None:
        return

    def position_ms(self) -> int:
        return self.position

    def duration_ms(self) -> int:
        return self.duration

    def close(self) -> None:
        return

    def emit(self, event: BackendEvent) -> None:
        self.callback(event, None)


class FakeTransport:
    def __init__(self) -> None:
        self.property_changes: list[dict[str, object]] = []
        self.seeked: list[int] = []
        self.closed = False

    def publish_player_properties(self, changed: Mapping[str, object]) -> None:
        self.property_changes.append(dict(changed))

    def emit_seeked(self, position_us: int) -> None:
        self.seeked.append(position_us)

    def close(self) -> None:
        self.closed = True


class FakeTimerScheduler:
    def schedule_repeating(
        self,
        _interval_ms: int,
        _callback: Callable[[], bool],
    ) -> object:
        return object()

    def cancel(self, _handle: object) -> None:
        return


class MprisServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=FakeTimerScheduler(),
        )
        self.player.set_metadata(
            title="Accessible episode",
            artist="Example feed",
            art_url="https://example.test/cover.png",
        )
        self.transport = FakeTransport()
        self.service = MprisService(self.player, transport=self.transport)

    def make_ready(self) -> None:
        self.assertTrue(self.player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)

    def tearDown(self) -> None:
        self.player.close()

    def test_snapshot_contains_standard_track_metadata(self) -> None:
        self.make_ready()

        snapshot = self.service.snapshot

        self.assertEqual("Paused", snapshot.playback_status)
        self.assertEqual("Accessible episode", snapshot.metadata["xesam:title"])
        self.assertEqual(["Example feed"], snapshot.metadata["xesam:artist"])
        self.assertEqual(90_000_000, snapshot.metadata["mpris:length"])
        self.assertEqual(
            track_id_for_metadata(self.player.metadata),
            snapshot.metadata["mpris:trackid"],
        )
        self.assertTrue(snapshot.can_play)
        self.assertTrue(snapshot.can_seek)

    def test_media_commands_update_player_and_emit_seeked(self) -> None:
        self.make_ready()

        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Play", ())
        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "Seek",
            (5_000_000,),
        )
        accepted_rate = self.service.set_player_property("Rate", 1.5)

        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)
        self.assertEqual(1, self.backend.play_calls)
        self.assertEqual(5_000, self.backend.seek_calls[-1])
        self.assertEqual([5_000_000], self.transport.seeked)
        self.assertTrue(accepted_rate)
        self.assertEqual(1.5, self.player.state.speed)
        self.assertTrue(
            any(change.get("PlaybackStatus") == "Playing" for change in self.transport.property_changes)
        )
        self.assertFalse(
            any("Position" in change for change in self.transport.property_changes)
        )


    def test_volume_property_updates_player_snapshot_and_desktop_clients(self) -> None:
        self.make_ready()

        accepted = self.service.set_player_property("Volume", 0.35)

        self.assertTrue(accepted)
        self.assertEqual(0.35, self.player.state.volume)
        self.assertEqual(0.35, self.backend.volume_calls[-1])
        self.assertEqual(0.35, self.service.snapshot.volume)
        self.assertTrue(
            any(change.get("Volume") == 0.35 for change in self.transport.property_changes)
        )
    def test_set_position_rejects_stale_track_and_out_of_range_value(self) -> None:
        self.make_ready()
        current_track = track_id_for_metadata(self.player.metadata)

        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "SetPosition",
            ("/cz/pvlcek/arss/track/t_stale", 10_000_000),
        )
        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "SetPosition",
            (current_track, 100_000_000),
        )
        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "SetPosition",
            (current_track, 10_000_000),
        )

        self.assertEqual([10_000], self.backend.seek_calls)
        self.assertEqual([10_000_000], self.transport.seeked)

    def test_stop_is_reported_as_stopped_and_later_play_clears_it(self) -> None:
        self.make_ready()
        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Play", ())

        changes_before_stop = len(self.transport.property_changes)
        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Stop", ())
        self.assertEqual("Stopped", self.service.snapshot.playback_status)
        stop_statuses = [
            change["PlaybackStatus"]
            for change in self.transport.property_changes[changes_before_stop:]
            if "PlaybackStatus" in change
        ]
        self.assertEqual(["Stopped"], stop_statuses)

        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Play", ())
        self.assertEqual("Playing", self.service.snapshot.playback_status)

    def test_open_uri_prepares_then_starts_only_after_ready(self) -> None:
        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "OpenUri",
            ("https://example.test/from-mpris.mp3",),
        )

        self.assertEqual(0, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PREPARING, self.player.state.phase)

        self.backend.emit(BackendEvent.READY)

        self.assertEqual(1, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)

    def test_open_uri_autoplays_when_ready_is_reported_synchronously(self) -> None:
        self.backend.ready_during_prepare = True

        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "OpenUri",
            ("https://example.test/cached.mp3",),
        )

        self.assertEqual(1, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)

    def test_pause_cancels_open_uri_autoplay_while_preparing(self) -> None:
        self.service.handle_method(
            MPRIS_PLAYER_INTERFACE,
            "OpenUri",
            ("https://example.test/slow.mp3",),
        )

        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Pause", ())
        self.backend.emit(BackendEvent.READY)

        self.assertEqual(0, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.READY, self.player.state.phase)

    def test_play_while_preparing_starts_after_ready(self) -> None:
        self.assertTrue(self.player.prepare("https://example.test/slow.mp3"))

        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "Play", ())
        self.backend.emit(BackendEvent.READY)

        self.assertEqual(1, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)

    def test_constructor_failure_detaches_listener_and_closes_transport(self) -> None:
        class FailingCloseCallbackPlayer(PodcastPlayer):
            def add_close_callback(self, callback: Callable[[], None]) -> None:
                del callback
                raise RuntimeError("close callback registration failed")

        player = FailingCloseCallbackPlayer(
            backend=FakeBackend(),
            timer_scheduler=FakeTimerScheduler(),
        )
        transport = FakeTransport()
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            MprisService(player, transport=transport)

        player.set_metadata(title="Must not reach failed MPRIS service")
        self.assertEqual([], transport.property_changes)
        self.assertTrue(transport.closed)
        player.close()

    def test_play_pause_while_preparing_starts_after_ready(self) -> None:
        self.assertTrue(self.player.prepare("https://example.test/slow.mp3"))

        self.service.handle_method(MPRIS_PLAYER_INTERFACE, "PlayPause", ())
        self.backend.emit(BackendEvent.READY)

        self.assertEqual(1, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)

    def test_player_close_owns_mpris_lifecycle(self) -> None:
        self.player.close()

        self.assertTrue(self.transport.closed)

    def test_completed_snapshot_is_stopped_at_microsecond_position(self) -> None:
        self.make_ready()
        self.player.play()
        self.backend.emit(BackendEvent.END_OF_STREAM)

        snapshot = build_mpris_snapshot(
            self.player.state,
            self.player.metadata,
        )

        self.assertEqual("Stopped", snapshot.playback_status)
        self.assertEqual(
            90_000 * MICROSECONDS_PER_MILLISECOND,
            snapshot.position_us,
        )


if __name__ == "__main__":
    unittest.main()
