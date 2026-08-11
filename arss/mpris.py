"""MPRIS 2 desktop media controls for :mod:`arss.playback`.

The controller and snapshot builder are GTK-free and fully testable without a
session bus.  :class:`GioMprisTransport` is a small lazy Gio adapter which owns
``org.mpris.MediaPlayer2.arss`` and exports the standard object path.  GNOME
Shell consumes MPRIS for media keys; WirePlumber can also use it to pause a
player when its actual audio target disappears.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
from typing import Final, Protocol
from urllib.parse import unquote, urlsplit

from .playback import (
    PlaybackMetadata,
    PlaybackPhase,
    PlaybackState,
    PodcastPlayer,
    SUPPORTED_PLAYBACK_SPEEDS,
)


MPRIS_BUS_NAME: Final = "org.mpris.MediaPlayer2.arss"
MPRIS_OBJECT_PATH: Final = "/org/mpris/MediaPlayer2"
MPRIS_ROOT_INTERFACE: Final = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_INTERFACE: Final = "org.mpris.MediaPlayer2.Player"
DBUS_PROPERTIES_INTERFACE: Final = "org.freedesktop.DBus.Properties"
MICROSECONDS_PER_MILLISECOND: Final = 1_000


@dataclass(frozen=True, slots=True)
class MprisSnapshot:
    """Plain Python values corresponding to MPRIS Player properties."""

    playback_status: str
    loop_status: str
    rate: float
    shuffle: bool
    metadata: Mapping[str, object]
    volume: float
    position_us: int
    minimum_rate: float
    maximum_rate: float
    can_go_next: bool
    can_go_previous: bool
    can_play: bool
    can_pause: bool
    can_seek: bool
    can_control: bool = True

    def properties(self) -> dict[str, object]:
        return {
            "PlaybackStatus": self.playback_status,
            "LoopStatus": self.loop_status,
            "Rate": self.rate,
            "Shuffle": self.shuffle,
            "Metadata": dict(self.metadata),
            "Volume": self.volume,
            "Position": self.position_us,
            "MinimumRate": self.minimum_rate,
            "MaximumRate": self.maximum_rate,
            "CanGoNext": self.can_go_next,
            "CanGoPrevious": self.can_go_previous,
            "CanPlay": self.can_play,
            "CanPause": self.can_pause,
            "CanSeek": self.can_seek,
            "CanControl": self.can_control,
        }


class MprisTransport(Protocol):
    def publish_player_properties(self, changed: Mapping[str, object]) -> None: ...

    def emit_seeked(self, position_us: int) -> None: ...

    def close(self) -> None: ...


class MprisController:
    """Translate MPRIS commands into the safe PodcastPlayer API."""

    def __init__(
        self,
        player: PodcastPlayer,
        *,
        raise_callback: Callable[[], None] | None = None,
        quit_callback: Callable[[], None] | None = None,
    ) -> None:
        self.player = player
        self.raise_callback = raise_callback
        self.quit_callback = quit_callback
        self.stopped = False
        self._play_when_ready = False

    def observe_state(self, state: PlaybackState) -> None:
        if state.phase is PlaybackPhase.PLAYING:
            self.stopped = False
        if self._play_when_ready and state.phase is PlaybackPhase.READY:
            self._play_when_ready = False
            self.player.play()
        elif state.phase is PlaybackPhase.ERROR:
            self._play_when_ready = False

    def dispatch(self, interface_name: str, method_name: str, parameters: tuple[object, ...]) -> bool:
        """Run one method and report whether it changed a supported state."""

        if interface_name == MPRIS_ROOT_INTERFACE:
            if method_name == "Raise" and self.raise_callback is not None:
                self.raise_callback()
                return True
            if method_name == "Quit" and self.quit_callback is not None:
                self.quit_callback()
                return True
            return False
        if interface_name != MPRIS_PLAYER_INTERFACE:
            return False
        if method_name in {"Next", "Previous"}:
            return False
        if method_name == "Pause":
            self.stopped = False
            self._play_when_ready = False
            return self.player.pause()
        if method_name == "PlayPause":
            self.stopped = False
            if self.player.state.phase is PlaybackPhase.PREPARING:
                self._play_when_ready = True
                return True
            return self.player.toggle()
        if method_name == "Stop":
            self._play_when_ready = False
            self.stopped = True
            changed = self.player.pause()
            return changed or self.player.state.is_prepared
        if method_name == "Play":
            self.stopped = False
            if self.player.state.phase is PlaybackPhase.PREPARING:
                self._play_when_ready = True
                return True
            return self.player.play()
        if method_name == "Seek" and len(parameters) == 1:
            try:
                offset_ms = int(parameters[0]) // MICROSECONDS_PER_MILLISECOND
            except (TypeError, ValueError, OverflowError):
                return False
            return self.player.seek_by(offset_ms)
        if method_name == "SetPosition" and len(parameters) == 2:
            track, position = parameters
            if str(track) != track_id_for_metadata(self.player.metadata):
                return False
            try:
                position_us = int(position)
            except (TypeError, ValueError, OverflowError):
                return False
            duration_us = self.player.state.duration_ms * MICROSECONDS_PER_MILLISECOND
            if position_us < 0 or (duration_us > 0 and position_us > duration_us):
                return False
            return self.player.seek_to(position_us // MICROSECONDS_PER_MILLISECOND)
        if method_name == "OpenUri" and len(parameters) == 1:
            uri = str(parameters[0]).strip()
            if not supported_open_uri(uri):
                return False
            self.stopped = False
            # Arm autoplay before prepare(). Local/cached media may report
            # READY synchronously from inside the backend call; arming it
            # afterwards would strand that media in READY forever.
            self._play_when_ready = True
            accepted = self.player.prepare(uri)
            if not accepted:
                self._play_when_ready = False
            return accepted
        return False

    def set_property(self, name: str, value: object) -> bool:
        if name == "Rate":
            try:
                return self.player.set_speed(float(value))
            except (TypeError, ValueError, OverflowError):
                return False
        if name == "Volume":
            try:
                return self.player.set_volume(float(value))
            except (TypeError, ValueError, OverflowError):
                return False
        # LoopStatus and Shuffle are mandatory MPRIS surface fields, but ARSS
        # intentionally does not claim controls it does not implement.
        return False


class MprisService:
    """Bind a player/controller to an injectable or Gio D-Bus transport."""

    def __init__(
        self,
        player: PodcastPlayer,
        *,
        raise_callback: Callable[[], None] | None = None,
        quit_callback: Callable[[], None] | None = None,
        transport: MprisTransport | None = None,
    ) -> None:
        self.player = player
        self.controller = MprisController(
            player,
            raise_callback=raise_callback,
            quit_callback=quit_callback,
        )
        self._closed = False
        self._last_snapshot = build_mpris_snapshot(
            player.state,
            player.metadata,
            stopped=self.controller.stopped,
        )
        self.transport = (
            transport if transport is not None else GioMprisTransport(self)
        )
        try:
            self.player.add_state_listener(self._on_player_state)
            self.player.add_close_callback(self.close)
        except BaseException:
            # A transport may already have exported its D-Bus object. Undo
            # every possible partial registration before preserving the
            # original constructor failure.
            self._closed = True
            for cleanup in (
                lambda: self.player.remove_state_listener(self._on_player_state),
                lambda: self.player.remove_close_callback(self.close),
                self.transport.close,
            ):
                try:
                    cleanup()
                except BaseException:
                    pass
            raise

    @classmethod
    def try_start(
        cls,
        player: PodcastPlayer,
        *,
        raise_callback: Callable[[], None] | None = None,
        quit_callback: Callable[[], None] | None = None,
    ) -> MprisService | None:
        """Start MPRIS when a session bus exists, otherwise return ``None``."""

        try:
            return cls(
                player,
                raise_callback=raise_callback,
                quit_callback=quit_callback,
            )
        except Exception:
            return None

    def root_property(self, name: str) -> object:
        values: dict[str, object] = {
            "CanQuit": self.controller.quit_callback is not None,
            "Fullscreen": False,
            "CanSetFullscreen": False,
            "CanRaise": self.controller.raise_callback is not None,
            "HasTrackList": False,
            "Identity": "ARSS",
            "DesktopEntry": "cz.pvlcek.arss",
            "SupportedUriSchemes": ["file", "http", "https"],
            "SupportedMimeTypes": [
                "audio/mpeg",
                "audio/ogg",
                "audio/opus",
                "audio/mp4",
            ],
        }
        if name not in values:
            raise KeyError(name)
        return values[name]

    def player_property(self, name: str) -> object:
        properties = self.snapshot.properties()
        if name not in properties:
            raise KeyError(name)
        return properties[name]

    @property
    def snapshot(self) -> MprisSnapshot:
        return build_mpris_snapshot(
            self.player.state,
            self.player.metadata,
            stopped=self.controller.stopped,
        )

    def handle_method(
        self,
        interface_name: str,
        method_name: str,
        parameters: tuple[object, ...],
    ) -> None:
        before = self.player.state.position_ms
        changed = self.controller.dispatch(interface_name, method_name, parameters)
        self._refresh()
        if changed and method_name in {"Seek", "SetPosition"}:
            after = self.player.state.position_ms
            if after != before:
                self.transport.emit_seeked(after * MICROSECONDS_PER_MILLISECOND)

    def set_player_property(self, name: str, value: object) -> bool:
        accepted = self.controller.set_property(name, value)
        self._refresh()
        return accepted

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for cleanup in (
            lambda: self.player.remove_state_listener(self._on_player_state),
            lambda: self.player.remove_close_callback(self.close),
            self.transport.close,
        ):
            try:
                cleanup()
            except Exception:
                # A failed presentation detach must not strand the bus name or
                # exported object; each cleanup is independent.
                pass

    def _on_player_state(self, state: PlaybackState) -> None:
        if self._closed:
            return
        self.controller.observe_state(state)
        self._refresh()

    def _refresh(self) -> None:
        if self._closed:
            return
        current = self.snapshot
        previous_properties = self._last_snapshot.properties()
        current_properties = current.properties()
        # MPRIS explicitly says Position should not emit PropertiesChanged.
        changed = {
            key: value
            for key, value in current_properties.items()
            if key != "Position" and previous_properties.get(key) != value
        }
        self._last_snapshot = current
        if changed:
            self.transport.publish_player_properties(changed)


def build_mpris_snapshot(
    state: PlaybackState,
    metadata: PlaybackMetadata,
    *,
    stopped: bool = False,
) -> MprisSnapshot:
    if stopped or state.phase in {
        PlaybackPhase.IDLE,
        PlaybackPhase.PREPARING,
        PlaybackPhase.COMPLETED,
        PlaybackPhase.ERROR,
    }:
        status = "Stopped"
    elif state.phase is PlaybackPhase.PLAYING:
        status = "Playing"
    else:
        status = "Paused"
    track_metadata: dict[str, object] = {
        "mpris:trackid": track_id_for_metadata(metadata),
        "xesam:title": _display_title(metadata),
    }
    if state.duration_ms > 0:
        track_metadata["mpris:length"] = (
            state.duration_ms * MICROSECONDS_PER_MILLISECOND
        )
    if metadata.artist.strip():
        track_metadata["xesam:artist"] = [metadata.artist.strip()]
    if metadata.album.strip():
        track_metadata["xesam:album"] = metadata.album.strip()
    if metadata.art_url.strip():
        track_metadata["mpris:artUrl"] = metadata.art_url.strip()
    if metadata.media_uri.strip():
        track_metadata["xesam:url"] = metadata.media_uri.strip()
    return MprisSnapshot(
        playback_status=status,
        loop_status="None",
        rate=state.speed,
        shuffle=False,
        metadata=track_metadata,
        volume=state.volume,
        position_us=state.position_ms * MICROSECONDS_PER_MILLISECOND,
        minimum_rate=min(SUPPORTED_PLAYBACK_SPEEDS),
        maximum_rate=max(SUPPORTED_PLAYBACK_SPEEDS),
        can_go_next=False,
        can_go_previous=False,
        can_play=state.is_prepared,
        can_pause=state.phase is PlaybackPhase.PLAYING,
        can_seek=state.is_prepared,
    )


def track_id_for_metadata(metadata: PlaybackMetadata) -> str:
    identity = metadata.media_uri.strip() or metadata.title.strip() or "none"
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return f"/cz/pvlcek/arss/track/t_{digest}"


def supported_open_uri(uri: str) -> bool:
    try:
        return urlsplit(uri).scheme.casefold() in {"file", "http", "https"}
    except ValueError:
        return False


def _display_title(metadata: PlaybackMetadata) -> str:
    title = metadata.title.strip()
    if title:
        return title
    try:
        name = PurePosixPath(unquote(urlsplit(metadata.media_uri).path)).name
    except ValueError:
        name = ""
    return name or "Podcast"


class GioMprisTransport:
    """Raw Gio D-Bus export of the two mandatory MPRIS interfaces."""

    def __init__(
        self,
        service: MprisService,
        *,
        gio: object | None = None,
        glib: object | None = None,
    ) -> None:
        if gio is None or glib is None:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            gio = Gio
            glib = GLib
        self.service = service
        self._gio = gio
        self._glib = glib
        self._closed = False
        self._connection = self._gio.bus_get_sync(  # type: ignore[attr-defined]
            self._gio.BusType.SESSION,  # type: ignore[attr-defined]
            None,
        )
        self._node_info = self._gio.DBusNodeInfo.new_for_xml(  # type: ignore[attr-defined]
            _MPRIS_INTROSPECTION_XML
        )
        self._registrations: list[int] = []
        try:
            for interface_info in self._node_info.interfaces:
                registration = self._connection.register_object(
                    MPRIS_OBJECT_PATH,
                    interface_info,
                    self._on_method_call,
                    self._on_get_property,
                    self._on_set_property,
                )
                if not registration:
                    raise RuntimeError(
                        f"Could not export {interface_info.name} on the session bus"
                    )
                self._registrations.append(registration)
            self._owner_id = self._gio.bus_own_name_on_connection(  # type: ignore[attr-defined]
                self._connection,
                MPRIS_BUS_NAME,
                self._gio.BusNameOwnerFlags.NONE,  # type: ignore[attr-defined]
                None,
                None,
            )
        except Exception:
            self.close()
            raise

    def publish_player_properties(self, changed: Mapping[str, object]) -> None:
        if self._closed or not changed:
            return
        variants = {
            name: _player_variant(self._glib, name, value)
            for name, value in changed.items()
        }
        self._connection.emit_signal(
            None,
            MPRIS_OBJECT_PATH,
            DBUS_PROPERTIES_INTERFACE,
            "PropertiesChanged",
            self._glib.Variant(
                "(sa{sv}as)",
                (MPRIS_PLAYER_INTERFACE, variants, []),
            ),
        )

    def emit_seeked(self, position_us: int) -> None:
        if self._closed:
            return
        self._connection.emit_signal(
            None,
            MPRIS_OBJECT_PATH,
            MPRIS_PLAYER_INTERFACE,
            "Seeked",
            self._glib.Variant("(x)", (int(position_us),)),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        owner_id = getattr(self, "_owner_id", 0)
        if owner_id:
            try:
                self._gio.bus_unown_name(owner_id)  # type: ignore[attr-defined]
            except Exception:
                pass
        connection = getattr(self, "_connection", None)
        if connection is not None:
            for registration in getattr(self, "_registrations", ()):
                try:
                    connection.unregister_object(registration)
                except Exception:
                    pass
        self._registrations = []

    def _on_method_call(
        self,
        _connection: object,
        _sender: str,
        _object_path: str,
        interface_name: str,
        method_name: str,
        parameters: object,
        invocation: object,
    ) -> None:
        try:
            unpacked = parameters.unpack()  # type: ignore[attr-defined]
            if not isinstance(unpacked, tuple):
                unpacked = (unpacked,)
            self.service.handle_method(interface_name, method_name, unpacked)
            invocation.return_value(self._glib.Variant("()", ()))  # type: ignore[attr-defined]
        except Exception as error:
            invocation.return_dbus_error(  # type: ignore[attr-defined]
                "org.mpris.MediaPlayer2.Error.Failed",
                str(error) or error.__class__.__name__,
            )

    def _on_get_property(
        self,
        _connection: object,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
    ) -> object:
        if interface_name == MPRIS_ROOT_INTERFACE:
            value = self.service.root_property(property_name)
            return _root_variant(self._glib, property_name, value)
        if interface_name == MPRIS_PLAYER_INTERFACE:
            value = self.service.player_property(property_name)
            return _player_variant(self._glib, property_name, value)
        raise KeyError(property_name)

    def _on_set_property(
        self,
        _connection: object,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
        value: object,
    ) -> bool:
        if interface_name != MPRIS_PLAYER_INTERFACE:
            return False
        unpacked = value.unpack()  # type: ignore[attr-defined]
        return self.service.set_player_property(property_name, unpacked)


def _root_variant(glib: object, name: str, value: object) -> object:
    signatures = {
        "CanQuit": "b",
        "Fullscreen": "b",
        "CanSetFullscreen": "b",
        "CanRaise": "b",
        "HasTrackList": "b",
        "Identity": "s",
        "DesktopEntry": "s",
        "SupportedUriSchemes": "as",
        "SupportedMimeTypes": "as",
    }
    return glib.Variant(signatures[name], value)  # type: ignore[attr-defined]


def _player_variant(glib: object, name: str, value: object) -> object:
    signatures = {
        "PlaybackStatus": "s",
        "LoopStatus": "s",
        "Rate": "d",
        "Shuffle": "b",
        "Volume": "d",
        "Position": "x",
        "MinimumRate": "d",
        "MaximumRate": "d",
        "CanGoNext": "b",
        "CanGoPrevious": "b",
        "CanPlay": "b",
        "CanPause": "b",
        "CanSeek": "b",
        "CanControl": "b",
    }
    if name == "Metadata":
        metadata = {
            key: _metadata_variant(glib, key, item)
            for key, item in dict(value).items()  # type: ignore[arg-type]
        }
        return glib.Variant("a{sv}", metadata)  # type: ignore[attr-defined]
    return glib.Variant(signatures[name], value)  # type: ignore[attr-defined]


def _metadata_variant(glib: object, name: str, value: object) -> object:
    signatures = {
        "mpris:trackid": "o",
        "mpris:length": "x",
        "mpris:artUrl": "s",
        "xesam:title": "s",
        "xesam:artist": "as",
        "xesam:album": "s",
        "xesam:url": "s",
    }
    return glib.Variant(signatures[name], value)  # type: ignore[attr-defined]


_MPRIS_INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""
