# Consumer synchronization

1. Fetch an immutable `v<contractVersion>` ARSS Contract tag.
2. Resolve and record the 40-character Git commit of that tag.
3. Verify every entry in `manifest.sha256` before parsing any contract file.
4. Validate JSON documents and the OPML document.
5. Generate or copy platform assets into a temporary location.
6. Run the platform's parity and parser tests against the golden files.
7. Replace the previous embedded assets atomically only after all checks pass.
8. Record `contractVersion`, contract repository URL, tag commit, and the
   SHA-256 of `manifest.sha256` in the consumer lock file.

A failed sync must leave the previous known-good snapshot untouched. Runtime
operation must not depend on GitHub availability.

## Station migration

The canonical runtime ID is `station.id`, such as `tv.ct1` or
`radio.cro-radiozurnal`. Existing provider-based preferences are migrated by
matching these forms against `providers`:

- `centrum:<providers.centrum.id>`
- `rozhlas:<providers.rozhlas.id>`
- `sms:<providers.sms.name>`

After resolving a legacy value, persist the canonical ID. Schedule lookup uses
provider metadata rather than extracting a provider from the canonical ID.
