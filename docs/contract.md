# ARSS Contract in the Linux port

The Linux application is the behavioural reference recorded by ARSS Contract,
but it is also a normal contract consumer.  Its GTK and accessibility layers
remain native; shared identifiers, station metadata, provider mappings,
fixtures and golden results come from an immutable vendored contract release.

Runtime code reads only `arss/data/contract`.  It never downloads mutable
contract data, invokes Git, or requires the network to start.  The
`contract.lock.json` file records the exact release tag, full Git commit and
SHA-256 of `manifest.sha256`.  The loader verifies the complete manifest before
using guide sources, station metadata, first-run feeds or the RSS directory.
The consumer-owned lock is deliberately outside the public manifest; run
`python3 -B tools/validate_vendored_contract.py` to validate the unchanged
public snapshot and the additional lock as two explicit layers.

## Updating the vendored release

Check out an immutable semantic tag of `vlcekapps/arss-contract` separately,
then run:

```console
python3 -B tools/sync-contract.py \
  --source ../arss-contract \
  --source-repository https://github.com/vlcekapps/arss-contract \
  --source-tag v1.0.1 \
  --source-commit FULL_40_CHARACTER_COMMIT
```

The tool has no network client.  It verifies that the source is the clean Git
checkout root, that `HEAD` and the requested tag both resolve to the supplied
full commit, and that `origin` matches the locked HTTPS repository. It validates every manifest
path and digest, checks the contract schemas Linux consumes, runs the actual
Centrum and Czech Radio catalogue parsers against the shared fixtures and
compares their exact normalized golden output.  Invalid input leaves the
existing embedded directory untouched. Symlinked path components are rejected,
and an interrupted directory swap is recovered from a validated deterministic
backup on the next update. `--check` performs the same process
without updating files and returns a non-zero status when the vendored tree
differs.

The scheduled GitHub workflow selects the newest `vMAJOR.MINOR.PATCH` tag and
opens or refreshes a pull request. Candidate data is parsed and tested in a
read-only job that never executes vendored Python, including the upstream
`tools/validate.py`. Only the validated data tree is handed to a fresh write
job; checkout credentials are not persisted and the write token is exposed
only to the final push/PR step. The workflow never merges automatically. A
human must review live-provider drift, user-visible catalogue changes, release
metadata and the final Fedora RPM.

## Stable IDs and upgrades

Contract IDs such as `tv.ct1` and `radio.radiozurnal` are the canonical runtime
and persistence IDs.  Provider-specific IDs from earlier Linux releases remain
accepted.  After the verified catalogue has loaded, the selected station is
resolved through its `centrum:`, `rozhlas:` or `sms:` mapping and atomically
saved under the stable ID.  Provider metadata then drives the programme source
priority without leaking provider IDs back into preferences.
