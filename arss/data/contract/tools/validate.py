#!/usr/bin/env python3
"""Validate the complete ARSS Contract without third-party packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from contractlib import ContractValidationError, validate_contract, write_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--update-manifest", action="store_true", help="regenerate manifest.sha256 before validation")
    args = parser.parse_args()
    try:
        if args.update_manifest:
            write_manifest(args.root)
        stats = validate_contract(args.root)
    except (ContractValidationError, OSError) as error:
        print(f"ARSS Contract validation failed: {error}", file=sys.stderr)
        return 1
    print("ARSS Contract validation passed")
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
