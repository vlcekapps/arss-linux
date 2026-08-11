# ARSS Linux project rules

- The application is accessibility-first. Every user action must remain reachable by keyboard and Orca, without relying on icons, colour, pointer gestures, hover, or context menus alone.
- Prefer standard GTK 4/libadwaita widgets and their native AT-SPI semantics. Any custom accessible role, state, name, relation, announcement, or focus change needs a test or a documented manual Orca check.
- Keep the domain, parser, storage, directory, guide, monitor and playback policy modules independent of GTK wherever practical.
- Unit tests must be deterministic and offline. Put explicitly requested live endpoint checks behind `ARSS_RUN_LIVE_TESTS=1`.
- Preserve feed and OPML security limits, HTTPS downgrade protection, XML DTD/entity rejection, and atomic persistence.
- Articles always open in the external browser. Podcast playback is internal and continues when its window/application loses focus; it stops when the player/application closes and pauses only on an explicit user/MPRIS command or a genuine audio-session interruption.
- Do not add autostart, a user systemd service/timer, or background operation after the app closes without an explicit user opt-in design.
- Notification feedback is owned by GNOME through `GNotification`; do not bundle or play application-specific notification audio.
- Preserve the project license as GPL-3.0-or-later and retain the CC0 notice for the bundled RSS directory.
- Never change or test against the Android Debug/Production packages or connected devices from this Linux repository.
- Every completed source, user-interface, packaging, bug-fix, or feature batch handed to the user must end with a newly built, upgradeable Fedora RPM containing the final changes. Bump the upstream version for a user-visible release; otherwise increment the RPM Release. Run the relevant unit, metadata, GUI, and accessibility gates first, then report the absolute RPM path and SHA-256.
