#!/usr/bin/env python3
"""Vendor one immutable ARSS contract release without using a submodule.

This tool deliberately has no network client.  CI or a maintainer checks out an
exact tagged contract revision, then passes that directory, tag and full commit
SHA here.  The source manifest and schemas are validated in a staging directory
before the existing vendored payload is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arss.contract import (  # noqa: E402
    ContractError,
    DEFAULT_CONTRACT_DIRECTORY,
    GUIDE_GOLDEN_FILE,
    LOCK_FILE,
    MANIFEST_FILE,
    VERSION_FILE,
    load_contract,
    verify_manifest,
)
from arss.guide_catalog import (  # noqa: E402
    parse_centrum_catalog,
    parse_rozhlas_catalog,
)


DEFAULT_REPOSITORY = "https://github.com/vlcekapps/arss-contract"
_BACKUP_MARKER = b"arss-contract-sync-backup-v1\n"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION_TAG = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ContractError(f"Could not parse {path.name}.") from exception
    if not isinstance(value, dict):
        raise ContractError(f"{path.name} must contain a JSON object.")
    return value


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def safe_directory_path(value: Path, label: str) -> Path:
    """Return an absolute lexical path after rejecting symlinked components."""

    path = Path(os.path.abspath(value))
    if path == Path(path.anchor) or not path.name:
        raise ContractError(f"Refusing to use an unsafe {label}: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_link(current):
            raise ContractError(f"The {label} traverses a symbolic link or junction: {current}")
    return path


def _repository_key(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(f"{label} must be a plain HTTPS repository URL.")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or path == "/":
        raise ContractError(f"{label} must identify a repository path.")
    try:
        parsed_port = parsed.port
    except ValueError as exception:
        raise ContractError(f"{label} has an invalid port.") from exception
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"https://{parsed.hostname.casefold()}{port}{path}"


def _git(source: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=source,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exception:
        raise ContractError("Git is required to verify contract provenance.") from exception
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ContractError(f"Git provenance check failed{suffix}")
    return result.stdout.strip()


def verify_git_provenance(
    source: Path,
    *,
    repository: str,
    tag: str,
    commit: str,
) -> None:
    """Prove that source is the clean checkout of the requested immutable tag."""

    if _VERSION_TAG.fullmatch(tag) is None:
        raise ContractError("The source tag must be an immutable v<semantic-version> tag.")
    if _GIT_SHA.fullmatch(commit) is None:
        raise ContractError("The source commit must be a full lowercase Git SHA.")
    top_level = Path(_git(source, "rev-parse", "--show-toplevel")).resolve()
    if top_level != source.resolve():
        raise ContractError("The contract source must be the root of its Git checkout.")
    head = _git(source, "rev-parse", "HEAD")
    if head != commit:
        raise ContractError(f"The contract checkout HEAD is {head}, not locked commit {commit}.")
    tagged_commit = _git(source, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if tagged_commit != commit:
        raise ContractError(f"Tag {tag} does not resolve to locked commit {commit}.")
    remote = _git(source, "config", "--local", "--get", "remote.origin.url")
    if _repository_key(remote, "Git origin") != _repository_key(repository, "sourceRepository"):
        raise ContractError("The contract Git origin does not match sourceRepository.")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ContractError("The contract checkout is dirty; refusing to record forged provenance.")


def write_file(root: Path, relative: str, payload: bytes) -> None:
    destination = root.joinpath(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


def validate_golden(staged: Path) -> None:
    bundle = load_contract(staged)
    normalized = {
        "$schema": "../../schemas/normalized-guide.schema.json",
        "schemaVersion": "1.0.0",
        "stations": [
            {
                "id": station.id,
                "medium": station.medium,
                "displayName": station.display_name,
                "sortOrder": station.sort_order,
            }
            for station in bundle.stations
        ],
    }
    golden = json_object(staged.joinpath(*PurePosixPath(GUIDE_GOLDEN_FILE).parts))
    if golden != normalized:
        raise ContractError(
            "golden/guide/stations.normalized.json does not exactly match the catalogue."
        )

    provider_cases = (
        (
            "centrum",
            "television",
            "fixtures/guide/centrum_channels.json",
            "golden/guide/centrum_channels.json",
            parse_centrum_catalog,
        ),
        (
            "rozhlas",
            "radio",
            "fixtures/guide/rozhlas_stations.json",
            "golden/guide/rozhlas_stations.json",
            parse_rozhlas_catalog,
        ),
    )
    for provider, medium, fixture_path, golden_path, parser in provider_cases:
        payload = staged.joinpath(*PurePosixPath(fixture_path).parts).read_bytes()
        parsed = parser(payload)
        stations: list[dict[str, object]] = []
        for parsed_station in parsed:
            stable_id = bundle.resolve_station_id(parsed_station.legacy_id)
            contract_station = bundle.station_by_id.get(stable_id)
            if contract_station is None or provider not in contract_station.providers:
                raise ContractError(
                    f"The {provider} parser returned an unmapped station: {parsed_station.legacy_id}"
                )
            if contract_station.medium != medium:
                raise ContractError(
                    f"The {provider} parser returned a station with the wrong medium: {stable_id}"
                )
            stations.append(
                {
                    "id": stable_id,
                    "medium": medium,
                    "displayName": parsed_station.display_name,
                    "sortOrder": contract_station.sort_order,
                }
            )
        provider_normalized = {
            "$schema": "../../schemas/normalized-guide.schema.json",
            "schemaVersion": "1.0.0",
            "stations": stations,
        }
        provider_golden = json_object(
            staged.joinpath(*PurePosixPath(golden_path).parts)
        )
        if provider_golden != provider_normalized:
            raise ContractError(
                f"{golden_path} does not match the Linux {provider} parser output."
            )


def prepare_staged_contract(
    source: Path,
    staged: Path,
    *,
    repository: str,
    tag: str,
    commit: str,
) -> None:
    entries = verify_manifest(source)
    version = json_object(source / VERSION_FILE)
    contract_version = version.get("contractVersion")
    if not isinstance(contract_version, str):
        raise ContractError("version.json has no valid contractVersion.")
    if tag != f"v{contract_version}":
        raise ContractError(
            f"Source tag {tag!r} does not identify contract {contract_version!r}."
        )
    manifest_payload = (source / MANIFEST_FILE).read_bytes()
    for relative in entries:
        payload = source.joinpath(*PurePosixPath(relative).parts).read_bytes()
        write_file(staged, relative, payload)
    write_file(staged, MANIFEST_FILE, manifest_payload)
    write_file(
        staged,
        LOCK_FILE,
        json_bytes(
            {
                "schemaVersion": "1.0.0",
                "contractVersion": contract_version,
                "sourceRepository": repository,
                "sourceTag": tag,
                "sourceCommit": commit,
                "manifestSha256": hashlib.sha256(manifest_payload).hexdigest(),
            }
        ),
    )
    validate_golden(staged)


def tree_files(root: Path) -> dict[str, bytes]:
    root = safe_directory_path(root, "contract tree")
    if not root.is_dir():
        return {}
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"Vendored contract contains a symlink: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _recover_interrupted_swap(destination: Path, backup: Path, marker: Path) -> None:
    if _is_link(backup):
        raise ContractError(f"Refusing to follow a stale backup link: {backup}")
    if _is_link(marker):
        raise ContractError(f"Refusing to follow a stale backup marker link: {marker}")
    marker_exists = marker.exists()
    if marker_exists:
        try:
            marker_payload = marker.read_bytes()
        except OSError as exception:
            raise ContractError(f"Could not read the trusted backup marker: {marker}") from exception
        if marker_payload != _BACKUP_MARKER:
            raise ContractError(f"The stale backup marker is invalid: {marker}")
    if not backup.exists():
        if marker_exists:
            try:
                load_contract(destination)
            except ContractError as exception:
                raise ContractError(
                    "A backup marker exists without a recoverable contract."
                ) from exception
            marker.unlink()
        return
    if not backup.is_dir():
        raise ContractError(f"The stale contract backup is not a directory: {backup}")
    if not marker_exists:
        raise ContractError(f"The stale backup has no trusted marker: {backup}")
    try:
        load_contract(backup)
    except ContractError as exception:
        raise ContractError(f"The stale contract backup is not known-good: {backup}") from exception
    if not destination.exists():
        os.replace(backup, destination)
        marker.unlink()
        return
    if not destination.is_dir():
        raise ContractError(f"The contract destination is not a directory: {destination}")
    try:
        load_contract(destination)
    except ContractError as exception:
        raise ContractError(
            "Both the contract destination and its backup exist, but the destination is invalid."
        ) from exception
    shutil.rmtree(backup)
    marker.unlink()


def _write_backup_marker(marker: Path) -> None:
    try:
        with marker.open("xb") as output:
            output.write(_BACKUP_MARKER)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exception:
        raise ContractError(f"Could not create the contract backup marker: {marker}") from exception


def install_staged(staged: Path, destination: Path) -> bool:
    """Install a validated tree, rolling back a failed directory swap."""

    staged = safe_directory_path(staged, "staged contract")
    destination = safe_directory_path(destination, "contract destination")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    backup = parent / f".{destination.name}.previous"
    marker = parent / f".{destination.name}.previous.marker"
    _recover_interrupted_swap(destination, backup, marker)
    if destination.exists() and not destination.is_dir():
        raise ContractError(f"The contract destination is not a directory: {destination}")
    staged_files = tree_files(staged)
    load_contract(staged)
    if tree_files(staged) == tree_files(destination):
        return False
    if backup.exists():
        raise ContractError(f"Refusing to reuse stale backup path: {backup}")
    moved_existing = False
    installed_staged = False
    failed = parent / f".{destination.name}.failed-{os.getpid()}"
    if failed.exists():
        raise ContractError(f"Refusing to reuse stale failed path: {failed}")
    try:
        if destination.exists():
            if _is_link(destination):
                raise ContractError(f"Refusing to replace a linked destination: {destination}")
            _write_backup_marker(marker)
            os.replace(destination, backup)
            moved_existing = True
        os.replace(staged, destination)
        installed_staged = True
        load_contract(destination)
        if tree_files(destination) != staged_files:
            raise ContractError("The installed contract differs from the validated staging tree.")
    except BaseException:
        if moved_existing and backup.exists():
            if destination.exists():
                os.replace(destination, failed)
            os.replace(backup, destination)
            if failed.exists():
                shutil.rmtree(failed)
            marker.unlink(missing_ok=True)
        elif installed_staged and destination.exists():
            shutil.rmtree(destination)
        elif marker.exists() and not backup.exists():
            marker.unlink()
        raise
    if moved_existing:
        shutil.rmtree(backup)
        marker.unlink()
    return True


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_CONTRACT_DIRECTORY,
    )
    parser.add_argument("--source-repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--source-tag", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and report drift without changing the destination.",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse_arguments(arguments if arguments is not None else sys.argv[1:])
    try:
        source = safe_directory_path(options.source, "contract source")
        destination = safe_directory_path(options.destination, "contract destination")
        if not source.is_dir():
            print(f"Contract source is not a directory: {source}", file=sys.stderr)
            return 2
        if source.resolve() == destination.resolve():
            print("Source and destination must be different directories.", file=sys.stderr)
            return 2
        verify_git_provenance(
            source,
            repository=options.source_repository,
            tag=options.source_tag,
            commit=options.source_commit,
        )
        if not options.check:
            destination.parent.mkdir(parents=True, exist_ok=True)
        staging_parent = destination.parent if destination.parent.is_dir() else None
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.staging-",
                dir=staging_parent,
            )
        )
        staged = temporary / destination.name
        staged.mkdir()
        prepare_staged_contract(
            source,
            staged,
            repository=options.source_repository,
            tag=options.source_tag,
            commit=options.source_commit,
        )
        if options.check:
            if tree_files(staged) != tree_files(destination):
                print("The vendored ARSS contract is out of date.", file=sys.stderr)
                return 1
            print("The vendored ARSS contract matches the requested immutable release.")
            return 0
        changed = install_staged(staged, destination)
        print(
            "Updated the vendored ARSS contract."
            if changed
            else "The vendored ARSS contract was already current."
        )
        return 0
    except (ContractError, OSError) as exception:
        print(f"Contract sync failed: {exception}", file=sys.stderr)
        return 2
    finally:
        if "temporary" in locals() and temporary.exists():
            shutil.rmtree(temporary)


if __name__ == "__main__":
    raise SystemExit(main())
