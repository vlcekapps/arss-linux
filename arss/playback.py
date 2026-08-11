"""Lifecycle-owned podcast playback built on GStreamer's ``playbin``.

The public :class:`PodcastPlayer` is deliberately independent of GTK.  It
publishes immutable state snapshots through a callback and accepts backend and
timer implementations, which keeps the state machine testable without audio
hardware.  GStreamer and GLib are imported only when their concrete adapters
are constructed.

Desktop audio focus is deliberately expressed through the small
:class:`~arss.audio_session.AudioSession` port.  Fedora does not expose the
Android audio-focus API; the default adapter therefore observes standard
PipeWire media roles when WirePlumber's introspection library is available and
otherwise degrades to a no-op session.  No screen-reader process is detected
or named here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, StrEnum
import math
from pathlib import Path
import threading
from typing import Final, Protocol
from urllib.parse import urlsplit

from .audio_session import (
    AudioInterruption,
    AudioSession,
    NoopAudioSession,
    create_desktop_audio_session,
)


DEFAULT_PLAYBACK_SPEED: Final = 1.0
SUPPORTED_PLAYBACK_SPEEDS: Final = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
SEEK_BACK_MS: Final = 15_000
SEEK_FORWARD_MS: Final = 30_000
TICK_INTERVAL_MS: Final = 250
_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000
_SPEED_EPSILON: Final = 0.001
NORMAL_VOLUME: Final = 1.0
DUCKED_VOLUME: Final = 0.2
DESKTOP_MEDIA_ROLE: Final = "music"
PIPEWIRE_DESKTOP_MEDIA_ROLE: Final = "Music"
DESKTOP_APPLICATION_ID: Final = "cz.pvlcek.arss"


class PlaybackPhase(StrEnum):
    """Externally visible phase of one podcast player."""

    IDLE = "idle"
    PREPARING = "preparing"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PlaybackState:
    """Immutable state suitable for binding directly to an accessible UI."""

    phase: PlaybackPhase = PlaybackPhase.IDLE
    position_ms: int = 0
    duration_ms: int = 0
    speed: float = DEFAULT_PLAYBACK_SPEED
    speed_change_failed: bool = False
    audio_session_denied: bool = False
    ducked: bool = False
    error_message: str | None = None
    volume: float = NORMAL_VOLUME

    @property
    def is_prepared(self) -> bool:
        return self.phase in {
            PlaybackPhase.READY,
            PlaybackPhase.PLAYING,
            PlaybackPhase.PAUSED,
            PlaybackPhase.COMPLETED,
        }

    # Descriptive aliases make state binding read naturally in callers which
    # use the Android application's original terminology.
    @property
    def position_millis(self) -> int:
        return self.position_ms

    @property
    def duration_millis(self) -> int:
        return self.duration_ms

    @property
    def playback_speed(self) -> float:
        return self.speed


class PlaybackError(RuntimeError):
    """A playback operation failed without escaping into the UI event loop."""


class PlaybackUnavailableError(PlaybackError):
    """The installed runtime does not provide GStreamer or GLib bindings."""


class BackendEvent(Enum):
    """Small event vocabulary shared by concrete and fake audio backends."""

    READY = "ready"
    END_OF_STREAM = "end-of-stream"
    ERROR = "error"


BackendEventCallback = Callable[[BackendEvent, str | None], None]
StateCallback = Callable[[PlaybackState], None]


@dataclass(frozen=True, slots=True)
class PlaybackMetadata:
    """Metadata shared with the audio server and desktop media controls."""

    title: str = ""
    artist: str = ""
    album: str = "ARSS"
    art_url: str = ""
    media_uri: str = ""


class PlaybackBackend(Protocol):
    """Audio operations required by :class:`PodcastPlayer`."""

    def set_event_callback(self, callback: BackendEventCallback) -> None: ...

    def prepare(self, uri: str) -> None: ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def seek(self, position_ms: int) -> None: ...

    def set_speed(self, speed: float) -> None: ...

    def set_volume(self, volume: float) -> None: ...

    def set_metadata(self, metadata: PlaybackMetadata) -> None: ...

    def position_ms(self) -> int: ...

    def duration_ms(self) -> int: ...

    def close(self) -> None: ...


class TimerScheduler(Protocol):
    """Repeating timer abstraction used for 250 ms position updates."""

    def schedule_repeating(
        self,
        interval_ms: int,
        callback: Callable[[], bool],
    ) -> object: ...

    def cancel(self, handle: object) -> None: ...


class GLibTimerScheduler:
    """A repeating timer backed by the application's GLib main context."""

    def __init__(self, glib: object | None = None) -> None:
        if glib is None:
            try:
                import gi

                gi.require_version("GLib", "2.0")
                from gi.repository import GLib
            except (ImportError, ValueError) as error:
                raise PlaybackUnavailableError(
                    "GLib Python bindings are unavailable"
                ) from error
            glib = GLib
        self._glib = glib

    def schedule_repeating(
        self,
        interval_ms: int,
        callback: Callable[[], bool],
    ) -> object:
        return self._glib.timeout_add(interval_ms, callback)  # type: ignore[attr-defined]

    def cancel(self, handle: object) -> None:
        self._glib.source_remove(handle)  # type: ignore[attr-defined]


class GStreamerPlaybinBackend:
    """GStreamer 1.0 ``playbin`` adapter with no GTK dependency."""

    def __init__(
        self,
        event_callback: BackendEventCallback | None = None,
        *,
        gst: object | None = None,
    ) -> None:
        if gst is None:
            try:
                import gi

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst
            except (ImportError, ValueError) as error:
                raise PlaybackUnavailableError(
                    "GStreamer Python bindings are unavailable"
                ) from error
            Gst.init(None)
            gst = Gst

        self._gst = gst
        self._event_callback = event_callback or _ignore_backend_event
        self._speed = DEFAULT_PLAYBACK_SPEED
        self._volume = NORMAL_VOLUME
        self._metadata = PlaybackMetadata()
        self._closed = False
        self._ready_reported = False
        self._playbin = self._gst.ElementFactory.make("playbin3", "arss-podcast-player")  # type: ignore[attr-defined]
        if self._playbin is None:
            self._playbin = self._gst.ElementFactory.make("playbin", "arss-podcast-player")  # type: ignore[attr-defined]
        if self._playbin is None:
            raise PlaybackUnavailableError("GStreamer could not create playbin")
        self._element_added_handler: object | None = None
        try:
            self._element_added_handler = self._playbin.connect(
                "deep-element-added",
                self._on_deep_element_added,
            )
        except (AttributeError, TypeError, ValueError):
            # Old or minimal playbin implementations still remain usable; the
            # media role is a policy hint, not a condition for playback.
            pass
        self._bus = self._playbin.get_bus()
        if self._bus is None:
            self._playbin.set_state(self._gst.State.NULL)  # type: ignore[attr-defined]
            raise PlaybackUnavailableError("GStreamer playbin has no message bus")
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._on_bus_message)

    def set_event_callback(self, callback: BackendEventCallback) -> None:
        self._event_callback = callback

    def prepare(self, uri: str) -> None:
        self._require_open()
        normalized_uri = _media_uri(uri)
        self._playbin.set_state(self._gst.State.NULL)  # type: ignore[attr-defined]
        self._playbin.set_property("uri", normalized_uri)
        self._metadata = replace(self._metadata, media_uri=normalized_uri)
        self._speed = DEFAULT_PLAYBACK_SPEED
        self._ready_reported = False
        result = self._playbin.set_state(self._gst.State.PAUSED)  # type: ignore[attr-defined]
        if result == self._gst.StateChangeReturn.FAILURE:  # type: ignore[attr-defined]
            raise PlaybackError("GStreamer could not prepare the media")
        # Local and already-cached media can preroll synchronously.
        if result in {
            self._gst.StateChangeReturn.SUCCESS,  # type: ignore[attr-defined]
            self._gst.StateChangeReturn.NO_PREROLL,  # type: ignore[attr-defined]
        }:
            self._report_ready()

    def play(self) -> None:
        self._set_state(self._gst.State.PLAYING, "start playback")  # type: ignore[attr-defined]

    def pause(self) -> None:
        self._set_state(self._gst.State.PAUSED, "pause playback")  # type: ignore[attr-defined]

    def seek(self, position_ms: int) -> None:
        self._require_open()
        target_ns = max(0, int(position_ms)) * _NANOSECONDS_PER_MILLISECOND
        accepted = self._playbin.seek(
            self._speed,
            self._gst.Format.TIME,  # type: ignore[attr-defined]
            self._gst.SeekFlags.FLUSH | self._gst.SeekFlags.ACCURATE,  # type: ignore[attr-defined]
            self._gst.SeekType.SET,  # type: ignore[attr-defined]
            target_ns,
            self._gst.SeekType.NONE,  # type: ignore[attr-defined]
            # Gst.CLOCK_TIME_NONE is an unsigned max value, while the GI seek
            # signature accepts a signed gint64.  -1 is the documented
            # sentinel for a stop position paired with SeekType.NONE.
            -1,
        )
        if not accepted:
            raise PlaybackError("GStreamer rejected the seek request")

    def set_speed(self, speed: float) -> None:
        self._require_open()
        position = self.position_ms()
        previous = self._speed
        self._speed = float(speed)
        try:
            self.seek(position)
        except Exception:
            self._speed = previous
            raise

    def set_volume(self, volume: float) -> None:
        """Set a normalized software volume used for cooperative ducking."""

        self._require_open()
        normalized = min(NORMAL_VOLUME, max(0.0, float(volume)))
        self._playbin.set_property("volume", normalized)
        self._volume = normalized

    def set_metadata(self, metadata: PlaybackMetadata) -> None:
        self._require_open()
        self._metadata = metadata

    def position_ms(self) -> int:
        self._require_open()
        success, value = self._playbin.query_position(self._gst.Format.TIME)  # type: ignore[attr-defined]
        if not success or value < 0:
            return 0
        return int(value // _NANOSECONDS_PER_MILLISECOND)

    def duration_ms(self) -> int:
        self._require_open()
        success, value = self._playbin.query_duration(self._gst.Format.TIME)  # type: ignore[attr-defined]
        if not success or value < 0:
            return 0
        return int(value // _NANOSECONDS_PER_MILLISECOND)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._playbin.set_state(self._gst.State.NULL)  # type: ignore[attr-defined]
        finally:
            try:
                self._bus.disconnect(self._bus_handler)
            except (AttributeError, TypeError, ValueError):
                pass
            try:
                self._bus.remove_signal_watch()
            except (AttributeError, TypeError, ValueError):
                pass
            if self._element_added_handler is not None:
                try:
                    self._playbin.disconnect(self._element_added_handler)
                except (AttributeError, TypeError, ValueError):
                    pass
            self._event_callback = _ignore_backend_event

    def _set_state(self, state: object, operation: str) -> None:
        self._require_open()
        result = self._playbin.set_state(state)
        if result == self._gst.StateChangeReturn.FAILURE:  # type: ignore[attr-defined]
            raise PlaybackError(f"GStreamer could not {operation}")

    def _require_open(self) -> None:
        if self._closed:
            raise PlaybackError("Playback backend is closed")

    def _report_ready(self) -> None:
        if self._closed or self._ready_reported:
            return
        self._ready_reported = True
        self._event_callback(BackendEvent.READY, None)

    def _on_deep_element_added(
        self,
        _playbin: object,
        _sub_bin: object,
        element: object,
    ) -> None:
        """Attach standard PipeWire/Pulse stream metadata when supported.

        ``playbin`` chooses its concrete sink lazily.  Watching nested elements
        avoids forcing PulseAudio compatibility on systems which have a native
        PipeWire sink, while both implementations can consume the conventional
        ``stream-properties`` structure.
        """

        try:
            property_spec = element.find_property("stream-properties")  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            return
        if property_spec is None:
            return
        try:
            properties = self._gst.Structure.new_empty("arss-stream-properties")  # type: ignore[attr-defined]
            role = DESKTOP_MEDIA_ROLE
            try:
                factory = element.get_factory()  # type: ignore[attr-defined]
                factory_name = factory.get_name() if factory is not None else ""
                if factory_name == "pipewiresink":
                    role = PIPEWIRE_DESKTOP_MEDIA_ROLE
            except (AttributeError, TypeError, ValueError):
                pass
            properties.set_value("media.role", role)
            properties.set_value("application.name", "ARSS")
            properties.set_value("application.id", DESKTOP_APPLICATION_ID)
            title = self._metadata.title.strip()
            if title:
                properties.set_value("media.title", title)
            artist = self._metadata.artist.strip()
            if artist:
                properties.set_value("media.artist", artist)
            album = self._metadata.album.strip()
            if album:
                properties.set_value("media.album", album)
            element.set_property("stream-properties", properties)  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            # Some sinks advertise the property but reject keys they do not
            # understand.  Playback must continue even without the policy hint.
            return

    def _on_bus_message(self, _bus: object, message: object) -> None:
        if self._closed:
            return
        message_type = message.type  # type: ignore[attr-defined]
        if message_type == self._gst.MessageType.ASYNC_DONE:  # type: ignore[attr-defined]
            self._report_ready()
        elif message_type == self._gst.MessageType.EOS:  # type: ignore[attr-defined]
            self._event_callback(BackendEvent.END_OF_STREAM, None)
        elif message_type == self._gst.MessageType.ERROR:  # type: ignore[attr-defined]
            error, _debug = message.parse_error()  # type: ignore[attr-defined]
            detail = str(error).strip() or "GStreamer playback failed"
            self._event_callback(BackendEvent.ERROR, detail)


class PodcastPlayer:
    """Safe, callback-driven controller for one podcast episode.

    ``prepare`` only prerolls the stream; the user must explicitly call
    :meth:`play`.  Every public operation absorbs backend failures, publishes a
    useful state, and returns whether the request was accepted.
    """

    def __init__(
        self,
        state_callback: StateCallback | None = None,
        *,
        backend: PlaybackBackend | None = None,
        timer_scheduler: TimerScheduler | None = None,
        audio_session: AudioSession | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._state = PlaybackState()
        self._state_callbacks: list[StateCallback] = []
        if state_callback is not None:
            self._state_callbacks.append(state_callback)
        self._close_callbacks: list[Callable[[], None]] = []
        self._closed = False
        self._timer_handle: object | None = None
        self._metadata = PlaybackMetadata()
        self._audio_session_acquired = False
        self._audio_session_suspended = False
        self._resume_after_transient = False

        supplied_backend = backend is not None
        if backend is None:
            try:
                backend = GStreamerPlaybinBackend()
            except Exception as error:
                backend = _UnavailableBackend(error)
        if timer_scheduler is None:
            try:
                timer_scheduler = GLibTimerScheduler()
            except Exception:
                timer_scheduler = _NoopTimerScheduler()
        if audio_session is None:
            # Tests and other embedders which provide their own backend should
            # also opt into a session explicitly.  The concrete desktop path
            # gets WirePlumber role observation when available.
            audio_session = (
                NoopAudioSession()
                if supplied_backend
                else create_desktop_audio_session()
            )
        self._backend = backend
        self._timer_scheduler = timer_scheduler
        self._audio_session = audio_session
        self._backend.set_event_callback(self._on_backend_event)
        self._audio_session.set_interruption_callback(
            self.handle_audio_interruption
        )

    @property
    def state(self) -> PlaybackState:
        with self._lock:
            return self._state

    @property
    def metadata(self) -> PlaybackMetadata:
        with self._lock:
            return self._metadata

    def add_state_listener(
        self,
        callback: StateCallback,
        *,
        notify_immediately: bool = False,
    ) -> None:
        """Observe state without replacing the presentation callback."""

        with self._lock:
            if self._closed or callback in self._state_callbacks:
                return
            self._state_callbacks.append(callback)
            state = self._state
        if notify_immediately:
            _safe_callback(callback, state)

    def remove_state_listener(self, callback: StateCallback) -> None:
        with self._lock:
            try:
                self._state_callbacks.remove(callback)
            except ValueError:
                pass

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Register lifecycle cleanup for a desktop integration such as MPRIS."""

        with self._lock:
            if not self._closed and callback not in self._close_callbacks:
                self._close_callbacks.append(callback)

    def remove_close_callback(self, callback: Callable[[], None]) -> None:
        """Detach an integration which was closed before the player itself."""

        with self._lock:
            try:
                self._close_callbacks.remove(callback)
            except ValueError:
                pass

    def set_metadata(
        self,
        metadata: PlaybackMetadata | None = None,
        **changes: str,
    ) -> None:
        """Update episode metadata before or after :meth:`prepare`.

        Keyword changes are convenient for UI adapters which only know the
        title and feed name.  Unknown field names retain dataclass' explicit
        ``TypeError`` instead of being silently ignored.
        """

        with self._lock:
            if self._closed:
                return
            current = metadata or self._metadata
            updated = replace(current, **changes) if changes else current
            self._metadata = updated
        setter = getattr(self._backend, "set_metadata", None)
        if setter is not None:
            try:
                setter(updated)
            except Exception:
                pass
        # Integration listeners (notably MPRIS) need to refresh metadata even
        # when the playback phase itself did not change.
        self._publish(self.state)

    def prepare(self, uri: str) -> bool:
        """Prepare a URI without starting playback."""

        with self._lock:
            if self._closed:
                return False
        if not isinstance(uri, str) or not uri.strip():
            self._fail(PlaybackError("Media URI must not be blank"))
            return False
        self._resume_after_transient = False
        self._release_audio_session()
        current_volume = self.state.volume
        self._set_backend_volume(current_volume)
        self._stop_ticker()
        self._publish(
            PlaybackState(phase=PlaybackPhase.PREPARING, volume=current_volume)
        )
        self.set_metadata(media_uri=uri.strip())
        try:
            self._backend.prepare(uri.strip())
        except Exception as error:
            self._fail(error)
            return False
        return True

    # ``open`` mirrors the original controller and is convenient for UI code.
    open = prepare

    def play(self) -> bool:
        with self._lock:
            if self._closed or not self._state.is_prepared:
                return False
            completed = self._state.phase == PlaybackPhase.COMPLETED
            speed = self._state.speed
        if not self._acquire_audio_session():
            self._publish(
                replace(
                    self.state,
                    audio_session_denied=True,
                )
            )
            return False
        # Acquisition may synchronously report an already-running
        # Accessibility stream.  Re-read the snapshot after that callback so
        # the following backend setup cannot accidentally undo its ducking.
        ducked = self.state.ducked
        try:
            if completed:
                self._backend.seek(0)
            self._backend.set_speed(speed)
            self._set_backend_volume(self._effective_volume(ducked=ducked))
            self._backend.play()
        except Exception as error:
            self._release_audio_session()
            self._fail(error)
            return False
        state = self.state
        self._publish(
            replace(
                state,
                phase=PlaybackPhase.PLAYING,
                position_ms=0 if completed else state.position_ms,
                speed_change_failed=False,
                audio_session_denied=False,
                error_message=None,
            )
        )
        self._start_ticker()
        return True

    def pause(self) -> bool:
        with self._lock:
            if self._closed or self._state.phase != PlaybackPhase.PLAYING:
                return False
        return self._pause(release_session=True)

    def _pause(self, *, release_session: bool) -> bool:
        try:
            self._backend.pause()
        except Exception as error:
            if release_session:
                self._release_audio_session()
            self._fail(error)
            return False
        self._stop_ticker()
        position, duration = self._read_timeline()
        state = self.state
        self._publish(
            replace(
                state,
                phase=PlaybackPhase.PAUSED,
                position_ms=position,
                duration_ms=max(duration, state.duration_ms),
            )
        )
        if release_session:
            self._resume_after_transient = False
            self._set_ducked(False)
            self._release_audio_session()
        return True

    def toggle(self) -> bool:
        return self.pause() if self.state.phase == PlaybackPhase.PLAYING else self.play()

    toggle_playback = toggle

    def seek_to(self, position_ms: int) -> bool:
        with self._lock:
            if self._closed or not self._state.is_prepared:
                return False
            duration = self._state.duration_ms
            phase = self._state.phase
        try:
            requested = int(position_ms)
        except (TypeError, ValueError, OverflowError):
            return False
        target = max(0, requested)
        if duration > 0:
            target = min(target, duration)
        try:
            self._backend.seek(target)
        except Exception as error:
            self._fail(error)
            return False
        self._publish(
            replace(
                self.state,
                phase=PlaybackPhase.PAUSED
                if phase == PlaybackPhase.COMPLETED
                else phase,
                position_ms=target,
                error_message=None,
            )
        )
        return True

    def seek_by(self, delta_ms: int) -> bool:
        try:
            delta = int(delta_ms)
        except (TypeError, ValueError, OverflowError):
            return False
        return self.seek_to(self.state.position_ms + delta)

    def seek_back(self) -> bool:
        return self.seek_by(-SEEK_BACK_MS)

    def seek_forward(self) -> bool:
        return self.seek_by(SEEK_FORWARD_MS)

    def set_speed(self, requested_speed: float) -> bool:
        speed = supported_playback_speed(requested_speed)
        if speed is None:
            return False
        with self._lock:
            if self._closed or not self._state.is_prepared:
                return False
            previous = self._state.speed
        try:
            self._backend.set_speed(speed)
        except Exception as error:
            self._publish(
                replace(
                    self.state,
                    speed=previous,
                    speed_change_failed=True,
                    error_message=_safe_error_message(error),
                )
            )
            return False
        self._publish(
            replace(
                self.state,
                speed=speed,
                speed_change_failed=False,
                error_message=None,
            )
        )
        return True

    set_playback_speed = set_speed

    def set_volume(self, requested_volume: float) -> bool:
        """Set and expose the user's normalized volume without losing ducking."""

        try:
            requested = float(requested_volume)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(requested):
            return False
        normalized = min(NORMAL_VOLUME, max(0.0, requested))
        with self._lock:
            if self._closed:
                return False
            ducked = self._state.ducked
        effective = normalized * (DUCKED_VOLUME if ducked else NORMAL_VOLUME)
        if not self._set_backend_volume(effective):
            return False
        self._publish(
            replace(
                self.state,
                volume=normalized,
                error_message=None,
            )
        )
        return True

    def handle_audio_interruption(self, event: AudioInterruption) -> None:
        """Apply one standards-based desktop audio-session event.

        A permanent loss or output removal pauses without automatic resume.  A
        transient loss remembers whether audio had been playing. Duck lowers
        the chosen software volume to 20%, and gain restores the chosen volume
        and resumes only playback which was interrupted by policy.
        """

        try:
            normalized = AudioInterruption(event)
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._closed:
                return
            playing = self._state.phase == PlaybackPhase.PLAYING

        if normalized is AudioInterruption.LOSS:
            self._resume_after_transient = False
            if playing:
                self._pause(release_session=False)
            self._set_ducked(False)
            self._release_audio_session()
        elif normalized is AudioInterruption.TRANSIENT_LOSS:
            with self._lock:
                self._audio_session_suspended = True
            self._resume_after_transient = playing
            if playing:
                self._pause(release_session=False)
        elif normalized is AudioInterruption.DUCK:
            with self._lock:
                self._audio_session_suspended = False
            resume = self._resume_after_transient
            self._resume_after_transient = False
            self._set_ducked(True)
            if resume:
                self.play()
        elif normalized is AudioInterruption.GAIN:
            with self._lock:
                self._audio_session_suspended = False
            resume = self._resume_after_transient
            self._resume_after_transient = False
            self._set_ducked(False)
            if resume:
                self.play()
        elif normalized is AudioInterruption.OUTPUT_REMOVED:
            self._resume_after_transient = False
            if playing:
                self._pause(release_session=False)
            self._set_ducked(False)
            self._release_audio_session()

    # Terminology used by native audio-session integrations.
    on_audio_interruption = handle_audio_interruption

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            was_playing = self._state.phase == PlaybackPhase.PLAYING
            self._closed = True
            close_callbacks = tuple(self._close_callbacks)
            self._close_callbacks.clear()
        self._stop_ticker()
        if was_playing:
            try:
                self._backend.pause()
            except Exception:
                pass
        self._release_audio_session()
        try:
            self._audio_session.close()
        except Exception:
            pass
        try:
            self._backend.close()
        except Exception:
            pass
        self._publish(PlaybackState())
        with self._lock:
            self._state_callbacks.clear()
        for callback in close_callbacks:
            try:
                callback()
            except Exception:
                pass

    def _on_backend_event(self, event: BackendEvent, detail: str | None) -> None:
        with self._lock:
            if self._closed:
                return
            phase = self._state.phase
        if event is BackendEvent.READY:
            if phase != PlaybackPhase.PREPARING:
                return
            position, duration = self._read_timeline()
            self._publish(
                replace(
                    self.state,
                    phase=PlaybackPhase.READY,
                    position_ms=position,
                    duration_ms=duration,
                    error_message=None,
                )
            )
        elif event is BackendEvent.END_OF_STREAM:
            if not self.state.is_prepared:
                return
            self._stop_ticker()
            state = self.state
            self._publish(
                replace(
                    state,
                    phase=PlaybackPhase.COMPLETED,
                    position_ms=state.duration_ms,
                )
            )
            self._resume_after_transient = False
            self._set_ducked(False)
            self._release_audio_session()
        elif event is BackendEvent.ERROR:
            self._fail(PlaybackError(detail or "Playback failed"))

    def _start_ticker(self) -> None:
        self._stop_ticker()
        try:
            handle = self._timer_scheduler.schedule_repeating(
                TICK_INTERVAL_MS,
                self._on_position_tick,
            )
        except Exception:
            return
        with self._lock:
            if self._closed or self._state.phase != PlaybackPhase.PLAYING:
                try:
                    self._timer_scheduler.cancel(handle)
                except Exception:
                    pass
                return
            self._timer_handle = handle

    def _stop_ticker(self) -> None:
        with self._lock:
            handle = self._timer_handle
            self._timer_handle = None
        if handle is not None:
            try:
                self._timer_scheduler.cancel(handle)
            except Exception:
                pass

    def _on_position_tick(self) -> bool:
        with self._lock:
            if self._closed or self._state.phase != PlaybackPhase.PLAYING:
                self._timer_handle = None
                return False
        position, duration = self._read_timeline()
        state = self.state
        self._publish(
            replace(
                state,
                position_ms=position,
                duration_ms=max(duration, state.duration_ms),
            )
        )
        return True

    def _read_timeline(self) -> tuple[int, int]:
        state = self.state
        try:
            position = max(0, int(self._backend.position_ms()))
        except Exception:
            position = state.position_ms
        try:
            duration = max(0, int(self._backend.duration_ms()))
        except Exception:
            duration = state.duration_ms
        if duration > 0:
            position = min(position, duration)
        return position, duration

    def _acquire_audio_session(self) -> bool:
        with self._lock:
            if self._audio_session_acquired and not self._audio_session_suspended:
                return True
            if self._audio_session_suspended:
                return False
        try:
            accepted = bool(self._audio_session.acquire())
        except Exception:
            # Role observation is advisory on the desktop.  A broken optional
            # adapter must not make otherwise valid local playback unusable.
            accepted = True
        with self._lock:
            self._audio_session_acquired = accepted
            suspended = self._audio_session_suspended
        return accepted and not suspended

    def _release_audio_session(self) -> None:
        with self._lock:
            self._audio_session_acquired = False
            self._audio_session_suspended = False
        try:
            self._audio_session.release()
        except Exception:
            pass

    def _effective_volume(
        self,
        *,
        ducked: bool | None = None,
    ) -> float:
        state = self.state
        use_ducking = state.ducked if ducked is None else ducked
        return state.volume * (
            DUCKED_VOLUME if use_ducking else NORMAL_VOLUME
        )

    def _set_backend_volume(self, volume: float) -> bool:
        setter = getattr(self._backend, "set_volume", None)
        if setter is None:
            return False
        try:
            setter(volume)
        except Exception:
            return False
        return True

    def _set_ducked(self, ducked: bool) -> None:
        # Ducking is cooperative: a backend failure must not turn a transient
        # desktop policy hint into a fatal playback error.
        self._set_backend_volume(self._effective_volume(ducked=ducked))
        self._publish(
            replace(
                self.state,
                ducked=ducked,
            )
        )

    def _fail(self, error: BaseException) -> None:
        self._stop_ticker()
        self._resume_after_transient = False
        self._release_audio_session()
        self._set_backend_volume(self._effective_volume(ducked=False))
        self._publish(
            replace(
                self.state,
                phase=PlaybackPhase.ERROR,
                ducked=False,
                error_message=_safe_error_message(error),
            )
        )

    def _publish(self, state: PlaybackState) -> None:
        with self._lock:
            self._state = state
            callbacks = tuple(self._state_callbacks)
        for callback in callbacks:
            _safe_callback(callback, state)


def supported_playback_speed(requested_speed: float) -> float | None:
    """Return the canonical supported speed nearest ``requested_speed``."""

    try:
        requested = float(requested_speed)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(requested):
        return None
    return next(
        (
            speed
            for speed in SUPPORTED_PLAYBACK_SPEEDS
            if abs(speed - requested) < _SPEED_EPSILON
        ),
        None,
    )


class _UnavailableBackend:
    def __init__(self, error: BaseException) -> None:
        self._error = PlaybackUnavailableError(_safe_error_message(error))

    def set_event_callback(self, callback: BackendEventCallback) -> None:
        del callback

    def prepare(self, uri: str) -> None:
        del uri
        raise self._error

    def play(self) -> None:
        raise self._error

    def pause(self) -> None:
        raise self._error

    def seek(self, position_ms: int) -> None:
        del position_ms
        raise self._error

    def set_speed(self, speed: float) -> None:
        del speed
        raise self._error

    def set_volume(self, volume: float) -> None:
        del volume
        raise self._error

    def set_metadata(self, metadata: PlaybackMetadata) -> None:
        del metadata
        raise self._error

    def position_ms(self) -> int:
        return 0

    def duration_ms(self) -> int:
        return 0

    def close(self) -> None:
        return


class _NoopTimerScheduler:
    def schedule_repeating(
        self,
        interval_ms: int,
        callback: Callable[[], bool],
    ) -> object:
        del interval_ms, callback
        return object()

    def cancel(self, handle: object) -> None:
        del handle


def _media_uri(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise PlaybackError("Media URI must not be blank")
    if urlsplit(normalized).scheme:
        return normalized
    return Path(normalized).expanduser().resolve().as_uri()


def _safe_error_message(error: BaseException) -> str:
    return str(error).strip() or error.__class__.__name__


def _ignore_backend_event(event: BackendEvent, detail: str | None) -> None:
    del event, detail


def _safe_callback(callback: StateCallback, state: PlaybackState) -> None:
    try:
        callback(state)
    except Exception:
        # A presentation/integration callback must never corrupt audio state.
        pass


# Compatibility names matching the domain terminology used by the Android app.
PodcastPlaybackPhase = PlaybackPhase
PodcastPlayerState = PlaybackState
PodcastPlayerController = PodcastPlayer
