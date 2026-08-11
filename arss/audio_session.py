"""Cooperative desktop audio-session policy for podcast playback.

Linux desktops do not define an application-facing equivalent of Android's
``AudioManager`` focus requests.  ARSS therefore uses two standards-based,
best-effort pieces:

* its own stream is tagged with the ``Music`` media role by the GStreamer
  backend;
* when libwireplumber's optional GObject introspection data is present, active
  PipeWire streams with an accessibility role duck ARSS and active
  communication streams transiently pause it.

The policy only inspects media roles and node state.  It never identifies Orca
or any other process.  Missing WirePlumber integration is intentionally a
no-op: Fedora's audio server remains responsible for routing and mixing.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import threading
from typing import Final, Protocol


class AudioInterruption(StrEnum):
    """Events understood by a lifecycle-owned media player."""

    LOSS = "loss"
    TRANSIENT_LOSS = "transient-loss"
    DUCK = "duck"
    GAIN = "gain"
    OUTPUT_REMOVED = "output-removed"


InterruptionCallback = Callable[[AudioInterruption], None]


class AudioSession(Protocol):
    """Small port replacing Android's platform-specific audio focus object."""

    def set_interruption_callback(self, callback: InterruptionCallback) -> None: ...

    def acquire(self) -> bool: ...

    def release(self) -> None: ...

    def close(self) -> None: ...


class NoopAudioSession:
    """Fallback for systems without a cooperative desktop policy adapter."""

    def __init__(self) -> None:
        self._callback: InterruptionCallback = _ignore_interruption

    def set_interruption_callback(self, callback: InterruptionCallback) -> None:
        self._callback = callback

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return

    def close(self) -> None:
        self._callback = _ignore_interruption


class InterruptionMode(StrEnum):
    """Aggregate effect of all currently running external audio streams."""

    NONE = "none"
    DUCK = "duck"
    PAUSE = "pause"


ACCESSIBILITY_ROLES: Final = frozenset({"accessibility", "a11y"})
COMMUNICATION_ROLES: Final = frozenset({"communication", "phone"})


class RoleInterruptionPolicy:
    """Pure aggregate policy keyed by opaque PipeWire node identities."""

    def __init__(
        self,
        callback: InterruptionCallback | None = None,
    ) -> None:
        self._callback = callback or _ignore_interruption
        self._nodes: dict[object, tuple[str, bool]] = {}
        self._mode = InterruptionMode.NONE

    @property
    def mode(self) -> InterruptionMode:
        return self._mode

    def set_callback(self, callback: InterruptionCallback) -> None:
        self._callback = callback

    def update(self, identity: object, role: str | None, running: bool) -> None:
        normalized_role = normalize_media_role(role)
        if normalized_role is None:
            self.remove(identity)
            return
        self._nodes[identity] = (normalized_role, bool(running))
        self._recompute()

    def remove(self, identity: object) -> None:
        if self._nodes.pop(identity, None) is not None:
            self._recompute()

    def clear(self, *, notify: bool = True) -> None:
        previous = self._mode
        self._nodes.clear()
        self._mode = InterruptionMode.NONE
        if notify and previous is not InterruptionMode.NONE:
            self._callback(AudioInterruption.GAIN)

    def activation_event(self) -> AudioInterruption | None:
        """Return the current non-blocking policy event for a new session."""

        if self._mode is InterruptionMode.DUCK:
            return AudioInterruption.DUCK
        return None

    def can_acquire(self) -> bool:
        return self._mode is not InterruptionMode.PAUSE

    def _recompute(self) -> None:
        roles = {
            role
            for role, running in self._nodes.values()
            if running
        }
        if roles & COMMUNICATION_ROLES:
            current = InterruptionMode.PAUSE
        elif roles & ACCESSIBILITY_ROLES:
            current = InterruptionMode.DUCK
        else:
            current = InterruptionMode.NONE
        previous = self._mode
        if current is previous:
            return
        self._mode = current
        event = _transition_event(previous, current)
        if event is not None:
            self._callback(event)


def normalize_media_role(role: str | None) -> str | None:
    """Normalize only standardized PipeWire/Pulse role spellings."""

    if not isinstance(role, str):
        return None
    normalized = role.strip().casefold()
    if normalized in ACCESSIBILITY_ROLES | COMMUNICATION_ROLES:
        return normalized
    return None


def _transition_event(
    previous: InterruptionMode,
    current: InterruptionMode,
) -> AudioInterruption | None:
    if current is InterruptionMode.PAUSE:
        return AudioInterruption.TRANSIENT_LOSS
    if current is InterruptionMode.DUCK:
        # PAUSE -> DUCK also tells the player that it may resume, but at the
        # attenuated volume.  PodcastPlayer's DUCK handler implements that.
        return AudioInterruption.DUCK
    if previous is not InterruptionMode.NONE:
        return AudioInterruption.GAIN
    return None


class WirePlumberAudioSession:
    """Optional libwireplumber 0.5 observer for role-based interruptions.

    Connection and signal failures never make playback fail.  The adapter does
    not try to infer the selected physical route; standard WirePlumber policy
    may relink a stream when a device disappears, while recent WirePlumber
    releases can pause MPRIS players on an actual target removal.
    """

    def __init__(self, *, wp: object | None = None, glib: object | None = None) -> None:
        if wp is None or glib is None:
            import gi

            gi.require_version("Wp", "0.5")
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib, Wp

            wp = Wp
            glib = GLib

        self._wp = wp
        self._glib = glib
        self._lock = threading.RLock()
        self._callback: InterruptionCallback = _ignore_interruption
        self._policy = RoleInterruptionPolicy(self._on_policy_event)
        self._enabled = False
        self._closed = False
        self._connection_attempted = False
        self._connected = False
        self._node_handlers: dict[object, tuple[object, object | None]] = {}

        flags = self._wp.InitFlags.PIPEWIRE | self._wp.InitFlags.SPA_TYPES  # type: ignore[attr-defined]
        self._wp.init(flags)  # type: ignore[attr-defined]
        self._core = self._wp.Core.new(None, None, None)  # type: ignore[attr-defined]
        self._manager = self._wp.ObjectManager.new()  # type: ignore[attr-defined]
        interest = self._wp.ObjectInterest.new_type(self._wp.Node.__gtype__)  # type: ignore[attr-defined]
        interest.add_constraint(
            self._wp.ConstraintType.PW_GLOBAL_PROPERTY,  # type: ignore[attr-defined]
            "media.class",
            self._wp.ConstraintVerb.EQUALS,  # type: ignore[attr-defined]
            self._glib.Variant("s", "Stream/Output/Audio"),  # type: ignore[attr-defined]
        )
        self._manager.add_interest_full(interest)
        self._manager.request_object_features(
            self._wp.Node.__gtype__,  # type: ignore[attr-defined]
            int(self._wp.ProxyFeatures.PIPEWIRE_OBJECT_FEATURE_INFO),  # type: ignore[attr-defined]
        )
        self._manager_handlers = (
            self._manager.connect("object-added", self._on_node_added),
            self._manager.connect("object-removed", self._on_node_removed),
        )
        self._core.install_object_manager(self._manager)

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connected

    def set_interruption_callback(self, callback: InterruptionCallback) -> None:
        with self._lock:
            self._callback = callback

    def acquire(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._enabled = True
        self._ensure_connected()
        if not self._policy.can_acquire():
            return False
        event = self._policy.activation_event()
        if event is not None:
            self._emit(event)
        return True

    def release(self) -> None:
        with self._lock:
            self._enabled = False

    def notify_output_removed(self) -> None:
        """Allow a verified route observer to request noisy-output handling."""

        self._emit(AudioInterruption.OUTPUT_REMOVED)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enabled = False
            connected = self._connected
            self._connected = False
            nodes = tuple(self._node_handlers.values())
            self._node_handlers.clear()
            self._callback = _ignore_interruption
        for node, handler in nodes:
            if handler is not None:
                try:
                    node.disconnect(handler)  # type: ignore[attr-defined]
                except (AttributeError, TypeError, ValueError):
                    pass
        for handler in self._manager_handlers:
            try:
                self._manager.disconnect(handler)
            except (AttributeError, TypeError, ValueError):
                pass
        if connected:
            try:
                self._core.disconnect()
            except Exception:
                pass
        self._policy.clear(notify=False)

    def _ensure_connected(self) -> None:
        with self._lock:
            if self._closed or self._connection_attempted:
                return
            self._connection_attempted = True
        try:
            connected = bool(self._core.connect())
        except Exception:
            connected = False
        with self._lock:
            if not self._closed:
                self._connected = connected

    def _on_node_added(self, _manager: object, node: object) -> None:
        identity = self._node_identity(node)
        handler: object | None = None
        try:
            handler = node.connect("state-changed", self._on_node_state_changed)  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            pass
        with self._lock:
            self._node_handlers[identity] = (node, handler)
        self._update_node(identity, node)

    def _on_node_removed(self, _manager: object, node: object) -> None:
        identity = self._node_identity(node)
        with self._lock:
            stored = self._node_handlers.pop(identity, None)
        if stored is not None and stored[1] is not None:
            try:
                stored[0].disconnect(stored[1])  # type: ignore[attr-defined]
            except (AttributeError, TypeError, ValueError):
                pass
        self._policy.remove(identity)

    def _on_node_state_changed(self, node: object, *_args: object) -> None:
        self._update_node(self._node_identity(node), node)

    def _update_node(self, identity: object, node: object) -> None:
        role = _node_property(node, "media.role")
        try:
            state = node.get_state()  # type: ignore[attr-defined]
            if isinstance(state, tuple):
                state = state[0]
            running = state == self._wp.NodeState.RUNNING  # type: ignore[attr-defined]
        except Exception:
            running = False
        self._policy.update(identity, role, running)

    def _node_identity(self, node: object) -> object:
        try:
            return int(node.get_bound_id())  # type: ignore[attr-defined]
        except Exception:
            return id(node)

    def _on_policy_event(self, event: AudioInterruption) -> None:
        self._emit(event)

    def _emit(self, event: AudioInterruption) -> None:
        with self._lock:
            if self._closed or not self._enabled:
                return
            callback = self._callback
        try:
            callback(event)
        except Exception:
            pass


def create_desktop_audio_session() -> AudioSession:
    """Return the best available session without making it a dependency."""

    try:
        return WirePlumberAudioSession()
    except Exception:
        return NoopAudioSession()


def _node_property(node: object, key: str) -> str | None:
    for getter_name in ("get_global_properties", "get_properties"):
        try:
            properties = getattr(node, getter_name)()
            value = properties.get(key)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value
    return None


def _ignore_interruption(event: AudioInterruption) -> None:
    del event
