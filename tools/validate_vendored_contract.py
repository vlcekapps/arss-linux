#!/usr/bin/env python3
"""Validate the public contract data plus its consumer-owned lock.

Only consumer-owned Python is executed here.  In particular, the vendored
``tools/validate.py`` is untrusted input while evaluating a new upstream tag
and must never be imported or run by this wrapper.
"""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arss.contract import (  # noqa: E402
    ContractError,
    DEFAULT_CONTRACT_DIRECTORY,
    LOCK_FILE,
    MANIFEST_FILE,
    load_contract,
    verify_manifest,
)


def _contract_files(root: Path) -> set[str]:
    result: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ContractError(f"The vendored contract contains a symbolic link: {path}")
        if path.is_file():
            result.add(path.relative_to(root).as_posix())
    return result


def validate_vendored_contract(root: Path = DEFAULT_CONTRACT_DIRECTORY) -> None:
    """Validate manifest coverage, the consumer lock, schemas and catalogues."""

    root = root.resolve()
    entries = verify_manifest(root)
    expected = set(entries) | {MANIFEST_FILE, LOCK_FILE}
    actual = _contract_files(root)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise ContractError(
            "The consumer contract tree differs from its manifest and lock ("
            + "; ".join(details)
            + ")."
        )
    load_contract(root)


def main() -> int:
    try:
        validate_vendored_contract()
    except (ContractError, OSError) as exception:
        print(f"Vendored contract validation failed: {exception}", file=sys.stderr)
        return 1
    print("Vendored ARSS Contract and consumer lock are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
