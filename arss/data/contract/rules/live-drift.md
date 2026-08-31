# Live drift policy

Scheduled checks compare live provider discovery responses with provider IDs in
the curated catalogue. Results are evidence for human review, never an update
instruction.

- HTTP, TLS, timeout, rate-limit, and parser failures are `unavailable`.
- A missing station is `possiblyRemoved`, never automatically deleted.
- A new provider ID is `unmapped` and requires identity/rebrand review.
- A changed name is `possiblyRenamed` and requires alias/history review.
- Generated reports and workflow artefacts must not be committed by the check.
- Workflows must never commit, push, merge, or modify release tags.
