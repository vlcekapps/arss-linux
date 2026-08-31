# Guide catalogue rules

The catalogue is a curated offline fallback and identifier registry. Live
discovery may add provider-only stations for the current session, but it must
not silently rewrite this file.

## Ordering

Consumers filter by `medium`, then sort by `sortOrder` ascending. Numbering
starts at zero independently for television and radio; ties within one medium
are invalid. `family` is a stable grouping hint and is not localized UI text.

## Provider choice

- Czech Television stations prefer the official `ct` schedule because it
  carries audio-description metadata, then use Centrum, then SMS.
- Other television stations prefer Centrum and fall back to SMS.
- Czech Radio stations prefer `rozhlas` and fall back to SMS.
- SMS-only stations use the exact `providers.sms.name` value; display names and
  aliases must never be substituted into provider requests.

Provider failure falls through to the next declared provider. It never changes
the station's stable ID.

## Names and aliases

`displayName` is the default visible, speakable name. `aliases` supports search,
legacy names, spelling variants, and provider terminology. Search is
case-insensitive, diacritic-insensitive, whitespace-normalized, and matches
prefixes of individual terms as described in `rules/search-normalization.md`.
