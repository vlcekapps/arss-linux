# ARSS contract project rules

- This repository contains only language-neutral, public ARSS contracts, fixtures, documentation, and validation tooling.
- Never add credentials, signing material, device identifiers, private filesystem paths, licensed audio, build artifacts, or application-specific secrets.
- JSON and OPML files are public APIs. Keep stable identifiers backward compatible and update schemas, fixtures, checksums, and the contract version together.
- Treat Linux ARSS v1.6.12 (`16245c06`) as the initial behavioral reference while preserving compatible data from Android and Apple ports.
- Live-source checks report drift for human review. They must never rewrite catalog data or merge changes automatically.
- Keep validation runnable with the Python standard library only.
- Do not commit or push without explicit review and authorization from the coordinating task.
