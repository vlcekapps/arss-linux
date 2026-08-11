from __future__ import annotations

from collections.abc import Callable
import unittest

from arss.playback import (
    AudioInterruption,
    BackendEvent,
    DUCKED_VOLUME,
    NORMAL_VOLUME,
    PlaybackMetadata,
    PlaybackPhase,
    PlaybackState,
    PodcastPlayer,
    SEEK_BACK_MS,
    SEEK_FORWARD_MS,
    SUPPORTED_PLAYBACK_SPEEDS,
    TICK_INTERVAL_MS,
    supported_playback_speed,
)


class FakeBackend:
    def __init__(self) -> None:
        self.callback: Callable[[BackendEvent, str | None], None] = (
            lambda _event, _detail: None
        )
        self.prepared_uri: str | None = None
        self.play_calls = 0
        self.pause_calls = 0
        self.close_calls = 0
        self.seek_calls: list[int] = []
        self.speed_calls: list[float] = []
        self.volume_calls: list[float] = []
        self.metadata_calls: list[PlaybackMetadata] = []
        self.position = 0
        self.duration = 120_000
        self.failure: str | None = None

    def set_event_callback(
        self,
        callback: Callable[[BackendEvent, str | None], None],
    ) -> None:
        self.callback = callback

    def prepare(self, uri: str) -> None:
        self._maybe_fail("prepare")
        self.prepared_uri = uri

    def play(self) -> None:
        self._maybe_fail("play")
        self.play_calls += 1

    def pause(self) -> None:
        self._maybe_fail("pause")
        self.pause_calls += 1

    def seek(self, position_ms: int) -> None:
        self._maybe_fail("seek")
        self.position = position_ms
        self.seek_calls.append(position_ms)

    def set_speed(self, speed: float) -> None:
        self._maybe_fail("speed")
        self.speed_calls.append(speed)

    def set_volume(self, volume: float) -> None:
        self._maybe_fail("volume")
        self.volume_calls.append(volume)

    def set_metadata(self, metadata: PlaybackMetadata) -> None:
        self.metadata_calls.append(metadata)

    def position_ms(self) -> int:
        self._maybe_fail("position")
        return self.position

    def duration_ms(self) -> int:
        self._maybe_fail("duration")
        return self.duration

    def close(self) -> None:
        self.close_calls += 1

    def emit(self, event: BackendEvent, detail: str | None = None) -> None:
        self.callback(event, detail)

    def _maybe_fail(self, operation: str) -> None:
        if self.failure == operation:
            raise RuntimeError(f"{operation} failed")


class FakeTimerScheduler:
    def __init__(self) -> None:
        self.next_handle = 1
        self.callbacks: dict[int, Callable[[], bool]] = {}
        self.intervals: list[int] = []
        self.cancelled: list[int] = []

    def schedule_repeating(
        self,
        interval_ms: int,
        callback: Callable[[], bool],
    ) -> object:
        handle = self.next_handle
        self.next_handle += 1
        self.callbacks[handle] = callback
        self.intervals.append(interval_ms)
        return handle

    def cancel(self, handle: object) -> None:
        numeric_handle = int(handle)
        self.cancelled.append(numeric_handle)
        self.callbacks.pop(numeric_handle, None)

    def fire(self, handle: int) -> bool:
        return self.callbacks[handle]()


class FakeAudioSession:
    def __init__(
        self,
        *,
        accepted: bool = True,
        event_on_acquire: AudioInterruption | None = None,
    ) -> None:
        self.callback: Callable[[AudioInterruption], None] = lambda _event: None
        self.accepted = accepted
        self.event_on_acquire = event_on_acquire
        self.acquire_calls = 0
        self.release_calls = 0
        self.close_calls = 0

    def set_interruption_callback(
        self,
        callback: Callable[[AudioInterruption], None],
    ) -> None:
        self.callback = callback

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if self.event_on_acquire is not None:
            self.callback(self.event_on_acquire)
        return self.accepted

    def release(self) -> None:
        self.release_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def emit(self, event: AudioInterruption) -> None:
        self.callback(event)


class PodcastPlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.timer = FakeTimerScheduler()
        self.states: list[PlaybackState] = []
        self.player = PodcastPlayer(
            self.states.append,
            backend=self.backend,
            timer_scheduler=self.timer,
        )

    def make_ready(self) -> None:
        self.assertTrue(self.player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)
        self.assertEqual(PlaybackPhase.READY, self.player.state.phase)

    def test_prepare_never_autoplays_and_ready_reports_duration(self) -> None:
        self.assertTrue(self.player.prepare("https://example.test/episode.mp3"))

        self.assertEqual("https://example.test/episode.mp3", self.backend.prepared_uri)
        self.assertEqual(0, self.backend.play_calls)
        self.assertEqual(PlaybackPhase.PREPARING, self.player.state.phase)

        self.backend.emit(BackendEvent.READY)

        self.assertEqual(PlaybackPhase.READY, self.player.state.phase)
        self.assertEqual(120_000, self.player.state.duration_ms)
        self.assertFalse(self.timer.intervals)

    def test_play_ticks_every_250_ms_and_pause_stops_ticker(self) -> None:
        self.make_ready()

        self.assertTrue(self.player.play())
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)
        self.assertEqual([TICK_INTERVAL_MS], self.timer.intervals)
        handle = next(iter(self.timer.callbacks))

        self.backend.position = 12_345
        self.assertTrue(self.timer.fire(handle))
        self.assertEqual(12_345, self.player.state.position_ms)

        self.assertTrue(self.player.pause())
        self.assertEqual(PlaybackPhase.PAUSED, self.player.state.phase)
        self.assertEqual([handle], self.timer.cancelled)
        self.assertEqual(1, self.backend.pause_calls)

    def test_seek_actions_clamp_to_zero_and_duration(self) -> None:
        self.make_ready()
        self.backend.duration = 50_000
        self.backend.emit(BackendEvent.READY)  # duplicate READY is ignored

        self.assertTrue(self.player.seek_back())
        self.assertEqual(0, self.backend.seek_calls[-1])
        self.assertTrue(self.player.seek_forward())
        self.assertEqual(SEEK_FORWARD_MS, self.backend.seek_calls[-1])
        self.assertTrue(self.player.seek_by(-SEEK_BACK_MS))
        self.assertEqual(15_000, self.backend.seek_calls[-1])
        self.assertTrue(self.player.seek_to(999_999))
        # The original READY snapshot reported 120 seconds.
        self.assertEqual(120_000, self.backend.seek_calls[-1])
        self.assertTrue(self.player.seek_to(-1))
        self.assertEqual(0, self.backend.seek_calls[-1])

    def test_every_supported_speed_is_applied_and_invalid_values_are_safe(self) -> None:
        self.make_ready()

        for speed in SUPPORTED_PLAYBACK_SPEEDS:
            with self.subTest(speed=speed):
                self.assertTrue(self.player.set_speed(speed))
                self.assertEqual(speed, self.player.state.speed)
        calls_before_invalid = tuple(self.backend.speed_calls)

        self.assertFalse(self.player.set_speed(1.1))
        self.assertFalse(self.player.set_speed(float("nan")))
        self.assertEqual(calls_before_invalid, tuple(self.backend.speed_calls))
        self.assertEqual(1.25, supported_playback_speed(1.2501))

    def test_speed_backend_failure_is_nonfatal_and_preserves_previous_speed(self) -> None:
        self.make_ready()
        self.assertTrue(self.player.set_speed(1.25))
        self.backend.failure = "speed"

        self.assertFalse(self.player.set_speed(1.5))

        self.assertEqual(PlaybackPhase.READY, self.player.state.phase)
        self.assertEqual(1.25, self.player.state.speed)
        self.assertTrue(self.player.state.speed_change_failed)
        self.assertIn("speed failed", self.player.state.error_message or "")

    def test_user_volume_is_clamped_and_backend_failures_preserve_state(self) -> None:
        self.assertTrue(self.player.set_volume(0.4))
        self.assertEqual(0.4, self.player.state.volume)

        self.assertFalse(self.player.set_volume(float("nan")))
        self.assertFalse(self.player.set_volume(float("inf")))
        self.assertFalse(self.player.set_volume("not a number"))
        self.assertEqual(0.4, self.player.state.volume)

        self.backend.failure = "volume"
        self.assertFalse(self.player.set_volume(0.7))
        self.assertEqual(0.4, self.player.state.volume)

        self.backend.failure = None
        self.assertTrue(self.player.set_volume(2.0))
        self.assertEqual(1.0, self.player.state.volume)
        self.assertTrue(self.player.set_volume(-1.0))
        self.assertEqual(0.0, self.player.state.volume)

    def test_user_volume_survives_prepare_play_and_cooperative_ducking(self) -> None:
        session = FakeAudioSession()
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        self.assertTrue(player.set_volume(0.4))
        self.assertEqual(0.4, player.state.volume)
        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.assertAlmostEqual(0.4, self.backend.volume_calls[-1])
        self.backend.emit(BackendEvent.READY)
        self.assertTrue(player.play())
        self.assertAlmostEqual(0.4, self.backend.volume_calls[-1])

        session.emit(AudioInterruption.DUCK)
        self.assertTrue(player.state.ducked)
        self.assertEqual(0.4, player.state.volume)
        self.assertAlmostEqual(0.08, self.backend.volume_calls[-1])

        self.assertTrue(player.set_volume(0.5))
        self.assertAlmostEqual(0.1, self.backend.volume_calls[-1])
        session.emit(AudioInterruption.GAIN)
        self.assertFalse(player.state.ducked)
        self.assertEqual(0.5, player.state.volume)
        self.assertAlmostEqual(0.5, self.backend.volume_calls[-1])

    def test_backend_errors_become_error_state_instead_of_escaping(self) -> None:
        self.backend.failure = "prepare"

        self.assertFalse(self.player.prepare("https://example.test/broken.mp3"))

        self.assertEqual(PlaybackPhase.ERROR, self.player.state.phase)
        self.assertIn("prepare failed", self.player.state.error_message or "")
        self.assertFalse(self.player.play())

    def test_end_of_stream_and_replay_seek_to_start(self) -> None:
        self.make_ready()
        self.assertTrue(self.player.play())

        self.backend.emit(BackendEvent.END_OF_STREAM)

        self.assertEqual(PlaybackPhase.COMPLETED, self.player.state.phase)
        self.assertEqual(120_000, self.player.state.position_ms)
        self.assertTrue(self.player.play())
        self.assertEqual(0, self.backend.seek_calls[-1])
        self.assertEqual(PlaybackPhase.PLAYING, self.player.state.phase)

    def test_close_pauses_active_audio_and_is_idempotent(self) -> None:
        self.make_ready()
        self.assertTrue(self.player.play())

        self.player.close()
        self.player.close()

        self.assertEqual(1, self.backend.pause_calls)
        self.assertEqual(1, self.backend.close_calls)
        self.assertEqual(PlaybackPhase.IDLE, self.player.state.phase)
        self.assertFalse(self.player.play())

    def test_presentation_callback_failure_cannot_break_player(self) -> None:
        player = PodcastPlayer(
            lambda _state: (_ for _ in ()).throw(RuntimeError("UI failed")),
            backend=self.backend,
            timer_scheduler=self.timer,
        )

        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)

        self.assertEqual(PlaybackPhase.READY, player.state.phase)

    def test_audio_session_denial_does_not_start_backend(self) -> None:
        session = FakeAudioSession(accepted=False)
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)

        self.assertFalse(player.play())

        self.assertEqual(1, session.acquire_calls)
        self.assertEqual(0, self.backend.play_calls)
        self.assertTrue(player.state.audio_session_denied)

    def test_accessibility_duck_and_gain_restore_volume(self) -> None:
        session = FakeAudioSession()
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)
        self.assertTrue(player.play())

        session.emit(AudioInterruption.DUCK)
        self.assertTrue(player.state.ducked)
        self.assertEqual(DUCKED_VOLUME, self.backend.volume_calls[-1])

        session.emit(AudioInterruption.GAIN)
        self.assertFalse(player.state.ducked)
        self.assertEqual(NORMAL_VOLUME, self.backend.volume_calls[-1])
        self.assertEqual(1, self.backend.play_calls)

    def test_already_running_accessibility_stream_stays_ducked_on_play(self) -> None:
        session = FakeAudioSession(event_on_acquire=AudioInterruption.DUCK)
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)

        self.assertTrue(player.play())

        self.assertEqual(PlaybackPhase.PLAYING, player.state.phase)
        self.assertTrue(player.state.ducked)
        self.assertEqual(DUCKED_VOLUME, self.backend.volume_calls[-1])

    def test_transient_loss_pauses_and_gain_resumes(self) -> None:
        session = FakeAudioSession()
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        self.assertTrue(player.prepare("https://example.test/episode.mp3"))
        self.backend.emit(BackendEvent.READY)
        self.assertTrue(player.play())

        session.emit(AudioInterruption.TRANSIENT_LOSS)
        self.assertEqual(PlaybackPhase.PAUSED, player.state.phase)
        self.assertEqual(1, self.backend.pause_calls)

        # A user/media-key Play before focus gain must not bypass the active
        # transient interruption.
        self.assertFalse(player.play())
        self.assertEqual(1, self.backend.play_calls)

        session.emit(AudioInterruption.GAIN)
        self.assertEqual(PlaybackPhase.PLAYING, player.state.phase)
        self.assertEqual(2, self.backend.play_calls)
        self.assertEqual(1, session.acquire_calls)

    def test_permanent_loss_and_output_removal_never_auto_resume(self) -> None:
        for event in (AudioInterruption.LOSS, AudioInterruption.OUTPUT_REMOVED):
            with self.subTest(event=event):
                backend = FakeBackend()
                session = FakeAudioSession()
                player = PodcastPlayer(
                    backend=backend,
                    timer_scheduler=FakeTimerScheduler(),
                    audio_session=session,
                )
                self.assertTrue(player.prepare("https://example.test/episode.mp3"))
                backend.emit(BackendEvent.READY)
                self.assertTrue(player.play())

                session.emit(event)
                session.emit(AudioInterruption.GAIN)

                self.assertEqual(PlaybackPhase.PAUSED, player.state.phase)
                self.assertEqual(1, backend.play_calls)
                self.assertGreaterEqual(session.release_calls, 1)
                player.close()

    def test_metadata_notifies_secondary_listeners_and_closes_session(self) -> None:
        session = FakeAudioSession()
        observed: list[PlaybackState] = []
        closed: list[bool] = []
        player = PodcastPlayer(
            backend=self.backend,
            timer_scheduler=self.timer,
            audio_session=session,
        )
        player.add_state_listener(observed.append)
        player.add_close_callback(lambda: closed.append(True))

        player.set_metadata(title="Episode", artist="Feed")
        player.close()

        self.assertEqual("Episode", player.metadata.title)
        self.assertTrue(observed)
        self.assertEqual([True], closed)
        self.assertEqual(1, session.close_calls)


if __name__ == "__main__":
    unittest.main()
