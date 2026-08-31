# ARSS Contract

ARSS Contract is the public, language-neutral source of shared data and
behavioural examples for ARSS applications. Platform applications keep native
user interfaces and accessibility integrations while consuming the same
catalogues, identifiers, fixtures, and expected normalized results.

The initial `1.0.0` contract records ARSS for Linux `v1.6.12` at commit
`16245c06ca7659712d2b4788b45ee71c5c8cc9c2` as the behavioural reference. The
station catalogue is an audited union of the Linux, Android, and Apple ports;
no individual port is treated as a complete catalogue by itself.

## Stable files

- `version.json` identifies the contract and upstream reference.
- `catalogs/guide_stations.json` contains stable station IDs and provider maps.
- `catalogs/guide_sources.json` describes live discovery and schedule sources.
- `catalogs/default_feeds.json` and `catalogs/rss_directory.opml` contain the
  shared first-run RSS choices.
- `schemas/` contains JSON Schema Draft 2020-12 descriptions.
- `fixtures/` and `golden/` define cross-language parser behaviour.
- `manifest.sha256` protects every contract input and expected output.

## Validate

Python 3.11 or newer is sufficient; validation has no third-party dependency.

```console
python tools/validate.py
python -m unittest discover -s tests -v
```

Live checks are deliberately separate and never edit catalogues:

```console
python tools/live_drift.py --report live-drift-report.md --json live-drift-report.json
```

Network failures are reported as `unavailable`, not interpreted as catalogue
deletions. A human must review every observed addition, removal, or rename.

## Consuming the contract

Consume an immutable release tag and record the ARSS Contract Git commit plus
the SHA-256 of `manifest.sha256`. Verify the manifest before replacing embedded
assets, then update them atomically. Do not vendor a dirty checkout or silently
download mutable data during an application build. See
`rules/consumer-sync.md` for the complete protocol.

The contract never contains audio, credentials, signing material, device
identifiers, private paths, or platform build artefacts.

## License

ARSS Contract is available under the MIT License except for third-party source
data identified in `THIRD_PARTY_NOTICES.md`. Live providers retain all rights
in their services and data; consumers must comply with provider terms.
