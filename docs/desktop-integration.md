# Desktop audio, media controls and notifications

ARSS uses GNOME/Fedora interfaces directly and keeps every optional integration
fail-safe.  A missing session bus or WirePlumber typelib never prevents the
podcast player from working.

## Playback and audio-session policy

`arss.playback.GStreamerPlaybinBackend` labels compatible PulseAudio/PipeWire
sinks with their standard `media.role=music`/`Music` spelling, plus the
application ID and current media metadata.  The optional
`WirePlumberAudioSession` watches only standard
PipeWire node properties:

- a running `Accessibility`/Pulse `a11y` stream ducks ARSS to 20%;
- a running `Communication`/Pulse `phone` stream temporarily pauses ARSS and
  resumes it when the role no longer runs;
- permanent loss and a verified output-removal event pause without resuming.

This is role-based policy.  It neither looks for the Orca process nor depends
on Orca's D-Bus names.  If the `Wp-0.5` GI typelib or PipeWire connection is
unavailable, `NoopAudioSession` grants playback and the desktop mixer retains
normal control.

There is no standard GNOME desktop equivalent of Android's exclusive audio
focus request.  WirePlumber's built-in role policy is configurable and its
documentation describes it primarily for automotive/mobile use, so ARSS does
not assume that Fedora enables it.  The app-side observer is cooperative: it
can react to correctly tagged streams, but it cannot make an untagged speech
engine identify itself.

References: [PipeWire media property keys](https://docs.pipewire.org/group__pw__keys.html),
[WirePlumber node state API](https://pipewire.pages.freedesktop.org/wireplumber/library/c_api/node_api.html),
[WirePlumber role-based policy](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/features.html).

## MPRIS and output removal

`arss.mpris.MprisService` exports MPRIS 2.2 as
`org.mpris.MediaPlayer2.arss` at `/org/mpris/MediaPlayer2`.  It supports:

- Play, Pause, PlayPause and Stop;
- relative Seek and track-checked SetPosition;
- supported playback rates and read/write application volume;
- URI opening, playback status, position, duration and track metadata;
- `PropertiesChanged` and `Seeked` with MPRIS units and rules.

This is the standard GNOME path for media keys.  WirePlumber 0.5.11 and newer
also has MPRIS pause handling when an audio target such as headphones is
removed.  That is safer than pausing whenever *any* sink disappears: standard
desktop policy may transparently relink the active stream to another output,
and libwireplumber does not expose an Android-style “becoming noisy” event tied
to one application.

`PlayerWindow` attaches the service directly:

```python
player.set_metadata(
    title=episode_title,
    artist=feed_title,
    media_uri=media_url,
)
mpris = MprisService.try_start(player, raise_callback=player_window.present)
```

Losing window focus, minimizing ARSS or switching to another application does
not pause playback. Closing or replacing the player still stops it.
`MainWindow` keeps at most one podcast player open, so the MPRIS bus name and
GNOME media controls always describe the same episode. The player retains the
state and close callbacks; closing it unregisters the MPRIS object.

Reference: [MPRIS 2.2 specification](https://specifications.freedesktop.org/mpris/latest/).

## Feed notifications

`arss.notifications.build_notification_batch()` creates up to eight stable,
actionable item notifications and, when there is more than one item, one stable
low-priority summary.  A single item is not duplicated by a summary.  RSS items
open the article, playable podcast items open the internal player, and all
fallbacks open the relevant ARSS page.  Reusing an ID lets
`GApplication.send_notification()` replace the same logical notification.
When a later batch contains only one item, the publisher withdraws a stale
summary from an earlier multi-item batch.

`GNotificationPublisher` sends standard Gio notifications using normal
priority for the first item and low priority for the remaining items and
summary. ARSS does not bundle or play notification audio. GNOME applies the
user's desktop notification policy and chooses any audible system feedback.

The foreground and headless monitor paths use the same publisher and pure
batch builder so their identifiers, priorities and action targets remain
identical.

Portable limits are important:

- Gio's `GNotification` API has no portable sound-selection field. GNOME and
  the user's desktop settings retain final authority over audible feedback.
- `GNotification` has no parent/child group identifier.  GNOME may group the
  item and summary notifications by application, but ARSS cannot require it.
- Gio exposes no per-notification lock-screen public/private variant.  The
  user's GNOME notification and lock-screen settings decide visibility.

References: [Gio.Notification](https://docs.gtk.org/gio/class.Notification.html),
[GApplication.send_notification](https://docs.gtk.org/gio/method.Application.send_notification.html),
[freedesktop notification hints](https://specifications.freedesktop.org/notification/latest/hints.html).

## GNOME application launcher upgrades

The desktop entry is generated with the stable system Python interpreter as
the first `Exec` program and the absolute ARSS launcher path as its second
argument.  In the Fedora RPM these are `/usr/bin/python3` and `/usr/bin/arss`.
GIO validates the first program while rebuilding GNOME's application index;
keeping that program outside the ARSS package prevents the application entry
from disappearing during the instant in which RPM replaces `/usr/bin/arss`.
This remains necessary with `DBusActivatable=true`: D-Bus activation controls
how an accepted entry is launched, but does not bypass GIO's indexing check.
The D-Bus activation service and opt-in systemd monitor use the same stable
interpreter-first command so every packaged launch surface has one contract.
The desktop ID, command line, name, description and icon are kept stable across
later package upgrades so an already displayed GNOME Shell icon stays bound to
the current application object.

## GNOME Software package details

The upstream metainfo component is installed as
`/usr/share/metainfo/cz.pvlcek.arss.metainfo.xml`. A locally installed RPM is
not represented in a repository-generated software catalog, so the package
also installs a standalone distribution catalog component at
`/usr/share/swcatalog/xml/arss.xml`. Its `<pkgname>arss</pkgname>` association
lets GNOME Software join the application component to the PackageKit package
which provides installation, upgrade and removal.

The catalog is intentionally a complete base component, not an orphan
`merge="append"` fragment. The release gate validates both XML files strictly,
converts the catalog in isolation, then loads catalog and metainfo into an
isolated AppStream pool. It requires exactly one `cz.pvlcek.arss` component
whose package name is `arss`. The Fedora `appstream` package refreshes its
catalog cache through an RPM file trigger whenever the catalog is installed or
removed.

References: [GNOME Software metadata](https://gnome.pages.gitlab.gnome.org/gnome-software/help/C/software-metadata.html),
[AppStream distribution metadata](https://www.freedesktop.org/software/appstream/docs/chap-CollectionData.html).

## Accessibility audit scope and gates

The GTK interface was audited against Linux Accessibility Development Guide
commit `a477501d3f97ffa1465a81c4a86508ce9af4ff38`. The applicable guide chapters
cover buttons and icon-only tooltips, check buttons, menus and menu buttons,
drop-downs, sliders, switches, keyboard operation, and low-vision requirements
such as platform colors and fonts, non-color state cues, visible focus, target
size, 200% text resizing and 320-pixel reflow.

The audit found and fixed two concrete gaps. Each custom popover now gives its
`MENU` container an explicit localized accessible name matching its opener;
previously only the opener and menu items were named. The large-text audit also
found controls, form labels, composite rows, window headings and main navigation
whose intrinsic width prevented 200% text from reflowing into 320 pixels. Those
views now wrap or change layout while keeping every action and the complete text
available without horizontal scrolling, and without changing native GTK roles,
accessible names or keyboard behavior.

These fixes are release-gated by the complete offline unit suite, the GTK GUI
smoke test, a dedicated English-and-Czech 200%/320-pixel reflow test (also run
with the High Contrast theme), and the AT-SPI smoke test including names and
keyboard behavior for custom menus. A Fedora RPM is
built only after those checks and the desktop/AppStream validators pass; its
payload and a test upgrade are then verified before handoff.

This guide revision does not yet contain dedicated chapters for tabs, lists or
list boxes, entries, dialogs, or live regions. ARSS still tests those controls
with their native GTK/AT-SPI contracts and Orca, but does not describe those
requirements as originating in the guide.

## Offline verification

The role policy, interruption state machine, MPRIS controller/snapshots and
notification batching/publishing all have injected adapters and need no audio
hardware, D-Bus daemon, network, or running desktop:

```sh
python3 -m unittest \
  tests.test_audio_session \
  tests.test_playback \
  tests.test_mpris \
  tests.test_notifications
```
