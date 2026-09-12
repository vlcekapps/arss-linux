# Contract versioning

`contractVersion` follows Semantic Versioning.

- Patch: corrected metadata, aliases, fixtures, or provider mappings without
  changing the meaning of an existing field or stable ID.
- Minor: backward-compatible fields, stations, locales, sources, or fixtures.
- Major: removed or renamed fields, changed normalization semantics, removed
  stable IDs, or otherwise incompatible consumer behaviour.

Stable station IDs are permanent. A rebrand changes `displayName` and retains
the previous name in `aliases`; it does not change `id`. If two services merge,
keep both old IDs resolvable and document the preferred one in release notes.

Every release updates `releasedAt`, regenerates `manifest.sha256`, passes all
offline validation, and is tagged `v<contractVersion>` from a clean commit.
