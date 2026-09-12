"""Validated, offline access to the vendored ARSS cross-platform contract.

The application never downloads contract data at runtime.  A maintainer-only
sync tool verifies an immutable upstream release and vendors its complete
manifest into :mod:`arss.data.contract`.  This module repeats the integrity
and schema checks before exposing the guide catalogue to the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Final, Mapping
import unicodedata
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


CONSUMER_VERSION: Final = "1.0.0"
DEFAULT_CONTRACT_DIRECTORY: Final = Path(__file__).with_name("data") / "contract"
LOCK_FILE: Final = "contract.lock.json"
MANIFEST_FILE: Final = "manifest.sha256"
VERSION_FILE: Final = "version.json"
GUIDE_STATIONS_FILE: Final = "catalogs/guide_stations.json"
GUIDE_SOURCES_FILE: Final = "catalogs/guide_sources.json"
DEFAULT_FEEDS_FILE: Final = "catalogs/default_feeds.json"
RSS_DIRECTORY_FILE: Final = "catalogs/rss_directory.opml"
GUIDE_GOLDEN_FILE: Final = "golden/guide/stations.normalized.json"

_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_STATIONS = 2_000
_MAX_SOURCES = 128
_MAX_DEFAULT_FEEDS = 256
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_STATION_ID = re.compile(r"^(?:tv|radio)\.[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_PROVIDER_KEYS = frozenset({"centrum", "ct", "rozhlas", "sms"})
_UNSAFE_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_WINDOWS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$",
    re.IGNORECASE,
)
_WINDOWS_FORBIDDEN = frozenset('<>"|?*')


class ContractError(RuntimeError):
    """The vendored contract is missing, corrupt, or incompatible."""


@dataclass(frozen=True, slots=True)
class ContractLock:
    contract_version: str
    source_repository: str
    source_tag: str
    source_commit: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ContractStation:
    id: str
    medium: str
    display_name: str
    sort_order: int
    family: str
    aliases: tuple[str, ...]
    providers: Mapping[str, Mapping[str, str]]

    @property
    def legacy_ids(self) -> tuple[str, ...]:
        """Return all provider-specific IDs accepted by earlier ARSS builds."""

        result: list[str] = []
        centrum = self.providers.get("centrum")
        if centrum is not None:
            result.append(f"centrum:{centrum['id']}")
        rozhlas = self.providers.get("rozhlas")
        if rozhlas is not None:
            result.append(f"rozhlas:{rozhlas['id']}")
        sms = self.providers.get("sms")
        if sms is not None:
            result.append(f"sms:{sms['name']}")
        return tuple(result)


@dataclass(frozen=True, slots=True)
class ContractGuideSource:
    id: str
    medium: str
    role: str
    base_url: str
    format: str
    priority: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class ContractFeed:
    id: str
    title: str
    url: str
    format: str


@dataclass(frozen=True, slots=True)
class ContractBundle:
    lock: ContractLock
    stations: tuple[ContractStation, ...]
    station_by_id: Mapping[str, ContractStation]
    stable_id_by_legacy_id: Mapping[str, str]
    guide_sources: tuple[ContractGuideSource, ...]
    guide_source_by_id: Mapping[str, ContractGuideSource]
    default_feeds_by_locale: Mapping[str, tuple[ContractFeed, ...]]
    rss_directory_path: Path

    def resolve_station_id(self, value: str) -> str:
        """Resolve either a stable or pre-contract station ID."""

        if value in self.station_by_id:
            return value
        return self.stable_id_by_legacy_id.get(value, value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def _read_limited(path: Path, maximum: int = _MAX_FILE_BYTES) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"Contract path is not a regular file: {path.name}")
        size = path.stat().st_size
        if size > maximum:
            raise ContractError(f"Contract file is too large: {path.name}")
        return path.read_bytes()
    except ContractError:
        raise
    except OSError as exception:
        raise ContractError(f"Could not read contract file: {path.name}") from exception


def _json_object(path: Path) -> dict[str, Any]:
    payload = _read_limited(path)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ContractError(f"Invalid UTF-8 JSON in {path.name}") from exception
    if not isinstance(value, dict):
        raise ContractError(f"Contract JSON root must be an object: {path.name}")
    return value


def _semver(value: Any, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a semantic version.")
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ContractError(f"{label} must be a semantic version.")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _https_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ContractError(f"{label} must be a bounded HTTPS URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ContractError(f"{label} must be a plain HTTPS URL.")
    return value


def _manifest_entries(directory: Path) -> dict[str, str]:
    payload = _read_limited(directory / MANIFEST_FILE, _MAX_MANIFEST_BYTES)
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exception:
        raise ContractError("The contract manifest must be ASCII.") from exception
    entries: dict[str, str] = {}
    portable_paths: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise ContractError(f"Invalid manifest line {line_number}.")
        digest, relative = match.groups()
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or relative != pure.as_posix()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or "\\" in relative
            or ":" in relative
            or relative in {MANIFEST_FILE, LOCK_FILE}
            or ".git" in pure.parts
            or "__pycache__" in pure.parts
            or relative in {"live-drift-report.json", "live-drift-report.md"}
            or relative.endswith((".pyc", ".pyo"))
        ):
            raise ContractError(f"Unsafe manifest path: {relative}")
        for part in pure.parts:
            device_stem = part.split(".", 1)[0].rstrip(" .")
            if (
                unicodedata.normalize("NFC", part) != part
                or part.endswith((" ", "."))
                or any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in part)
                or _WINDOWS_DEVICE.fullmatch(device_stem) is not None
            ):
                raise ContractError(f"Non-portable manifest path: {relative}")
        if relative in entries:
            raise ContractError(f"Duplicate manifest path: {relative}")
        portable = unicodedata.normalize("NFC", relative).casefold()
        collision = portable_paths.get(portable)
        if collision is not None:
            raise ContractError(
                f"Manifest paths collide on portable filesystems: {collision}, {relative}"
            )
        portable_paths[portable] = relative
        entries[relative] = digest
    if not entries:
        raise ContractError("The contract manifest is empty.")
    return entries


def verify_manifest(directory: Path) -> Mapping[str, str]:
    """Verify all manifest-covered files and return their expected digests."""

    directory = directory.resolve()
    entries = _manifest_entries(directory)
    required = {
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        VERSION_FILE,
        GUIDE_STATIONS_FILE,
        GUIDE_SOURCES_FILE,
        DEFAULT_FEEDS_FILE,
        RSS_DIRECTORY_FILE,
        GUIDE_GOLDEN_FILE,
        "schemas/version.schema.json",
        "schemas/default-feeds.schema.json",
        "schemas/guide-sources.schema.json",
        "schemas/guide-stations.schema.json",
        "schemas/normalized-guide.schema.json",
        "fixtures/guide/centrum_channels.json",
        "fixtures/guide/rozhlas_stations.json",
        "golden/guide/centrum_channels.json",
        "golden/guide/rozhlas_stations.json",
    }
    missing = sorted(required.difference(entries))
    if missing:
        raise ContractError(f"Contract manifest omits required files: {', '.join(missing)}")
    total = 0
    for relative, expected in entries.items():
        candidate = directory.joinpath(*PurePosixPath(relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(directory)
        except (OSError, ValueError) as exception:
            raise ContractError(f"Manifest path escapes the contract: {relative}") from exception
        if resolved != candidate.absolute():
            raise ContractError(f"Manifest paths must not traverse symlinks: {relative}")
        payload = _read_limited(candidate)
        total += len(payload)
        if total > _MAX_TOTAL_BYTES:
            raise ContractError("The contract exceeds its total size limit.")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ContractError(f"Contract checksum mismatch: {relative}")
    return MappingProxyType(entries)


def _load_lock(directory: Path) -> ContractLock:
    value = _json_object(directory / LOCK_FILE)
    expected_keys = {
        "schemaVersion",
        "contractVersion",
        "sourceRepository",
        "sourceTag",
        "sourceCommit",
        "manifestSha256",
    }
    if set(value) != expected_keys or value.get("schemaVersion") != "1.0.0":
        raise ContractError("Unsupported contract lock schema.")
    contract_version = value.get("contractVersion")
    _semver(contract_version, "contractVersion")
    repository = _https_url(value.get("sourceRepository"), "sourceRepository")
    tag = value.get("sourceTag")
    commit = value.get("sourceCommit")
    manifest_digest = value.get("manifestSha256")
    if tag != f"v{contract_version}":
        raise ContractError("sourceTag must identify the locked contract version.")
    if not isinstance(commit, str) or _GIT_SHA.fullmatch(commit) is None:
        raise ContractError("sourceCommit must be a full lowercase Git SHA.")
    if not isinstance(manifest_digest, str) or _SHA256.fullmatch(manifest_digest) is None:
        raise ContractError("manifestSha256 is invalid.")
    return ContractLock(
        contract_version=contract_version,
        source_repository=repository,
        source_tag=tag,
        source_commit=commit,
        manifest_sha256=manifest_digest,
    )


def _validate_version(directory: Path, lock: ContractLock) -> None:
    value = _json_object(directory / VERSION_FILE)
    expected_keys = {
        "contractVersion",
        "releasedAt",
        "reference",
        "minimumConsumerVersion",
    }
    if set(value) == expected_keys | {"$schema"}:
        if value.get("$schema") != "./schemas/version.schema.json":
            raise ContractError("version.json has an invalid schema reference.")
    elif set(value) != expected_keys:
        raise ContractError("Unsupported version.json shape.")
    contract_version = value.get("contractVersion")
    contract_parts = _semver(contract_version, "contractVersion")
    consumer_parts = _semver(CONSUMER_VERSION, "consumer version")
    minimum_parts = _semver(value.get("minimumConsumerVersion"), "minimumConsumerVersion")
    if contract_version != lock.contract_version:
        raise ContractError("The lock and version.json disagree on contractVersion.")
    if contract_parts[0] != consumer_parts[0] or minimum_parts > consumer_parts:
        raise ContractError(f"Contract {contract_version} is incompatible with consumer {CONSUMER_VERSION}.")
    released = value.get("releasedAt")
    try:
        Date.fromisoformat(released)
    except (TypeError, ValueError) as exception:
        raise ContractError("releasedAt must be an ISO calendar date.") from exception
    reference = value.get("reference")
    if not isinstance(reference, dict) or set(reference) != {"repository", "tag", "commit"}:
        raise ContractError("version.json reference is invalid.")
    _https_url(reference.get("repository"), "reference.repository")
    if not isinstance(reference.get("tag"), str) or not reference["tag"]:
        raise ContractError("reference.tag is invalid.")
    if not isinstance(reference.get("commit"), str) or _GIT_SHA.fullmatch(reference["commit"]) is None:
        raise ContractError("reference.commit must be a full lowercase Git SHA.")


def _bounded_text(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError(f"{label} must be non-empty bounded text.")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ContractError(f"{label} contains invalid whitespace or controls.")
    return value


def _provider_data(value: Any, station_id: str) -> Mapping[str, Mapping[str, str]]:
    if not isinstance(value, dict) or not value or not set(value).issubset(_PROVIDER_KEYS):
        raise ContractError(f"Station {station_id} has invalid providers.")
    result: dict[str, Mapping[str, str]] = {}
    provider_shapes = {
        "centrum": ({"id"}, {"id", "slug"}),
        "ct": ({"channel"}, {"channel"}),
        "rozhlas": ({"id"}, {"id"}),
        "sms": ({"name"}, {"name"}),
    }
    for provider, raw in value.items():
        required, allowed = provider_shapes[provider]
        if not isinstance(raw, dict) or not required.issubset(raw) or not set(raw).issubset(allowed):
            raise ContractError(f"Station {station_id} has invalid {provider} metadata.")
        fields = {
            key: _bounded_text(field, f"{station_id}.{provider}.{key}")
            for key, field in raw.items()
        }
        if provider == "centrum" and not fields["id"].isdigit():
            raise ContractError(f"Station {station_id} has invalid centrum.id metadata.")
        if provider in {"centrum", "rozhlas"}:
            slug = fields.get("slug") if provider == "centrum" else fields["id"]
            if slug is not None and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None:
                raise ContractError(f"Station {station_id} has invalid {provider} slug metadata.")
        if provider == "ct" and fields["channel"] not in {
            "ct1", "ct2", "ct24", "ct4", "ct5", "ct6"
        }:
            raise ContractError(f"Station {station_id} has invalid ct.channel metadata.")
        result[provider] = MappingProxyType(fields)
    return MappingProxyType(result)


def _load_guide_sources(directory: Path) -> tuple[ContractGuideSource, ...]:
    root = _json_object(directory / GUIDE_SOURCES_FILE)
    if set(root) != {"$schema", "schemaVersion", "sources"}:
        raise ContractError("Unsupported guide source schema.")
    if (
        root.get("$schema") != "../schemas/guide-sources.schema.json"
        or root.get("schemaVersion") != "1.0.0"
    ):
        raise ContractError("Unsupported guide source schema.")
    values = root.get("sources")
    if not isinstance(values, list) or not values or len(values) > _MAX_SOURCES:
        raise ContractError("The guide source array is empty or too large.")
    result: list[ContractGuideSource] = []
    identifiers: set[str] = set()
    roles: set[tuple[str, str]] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict) or set(raw) != {
            "id", "medium", "role", "baseUrl", "format", "priority", "enabled"
        }:
            raise ContractError(f"Guide source {index} has an unsupported shape.")
        source_id = raw.get("id")
        if (
            not isinstance(source_id, str)
            or _SOURCE_ID.fullmatch(source_id) is None
            or source_id in identifiers
        ):
            raise ContractError(f"Guide source {index} has an invalid or duplicate ID.")
        identifiers.add(source_id)
        medium = raw.get("medium")
        role = raw.get("role")
        source_format = raw.get("format")
        priority = raw.get("priority")
        enabled = raw.get("enabled")
        if medium not in {"television", "radio"} or role not in {"discovery", "schedule"}:
            raise ContractError(f"Guide source {source_id} has invalid routing metadata.")
        if source_format not in {"json", "xml", "html"}:
            raise ContractError(f"Guide source {source_id} has an invalid format.")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ContractError(f"Guide source {source_id} has an invalid priority.")
        if not isinstance(enabled, bool):
            raise ContractError(f"Guide source {source_id} has an invalid enabled flag.")
        roles.add((medium, role))
        result.append(
            ContractGuideSource(
                id=source_id,
                medium=medium,
                role=role,
                base_url=_https_url(raw.get("baseUrl"), f"{source_id}.baseUrl"),
                format=source_format,
                priority=priority,
                enabled=enabled,
            )
        )
    if roles != {
        ("television", "discovery"),
        ("television", "schedule"),
        ("radio", "discovery"),
        ("radio", "schedule"),
    }:
        raise ContractError("Each guide medium requires discovery and schedule sources.")
    return tuple(result)


def _load_default_feeds(directory: Path) -> Mapping[str, tuple[ContractFeed, ...]]:
    root = _json_object(directory / DEFAULT_FEEDS_FILE)
    if set(root) != {"$schema", "schemaVersion", "locales"}:
        raise ContractError("Unsupported default feed schema.")
    if (
        root.get("$schema") != "../schemas/default-feeds.schema.json"
        or root.get("schemaVersion") != "1.0.0"
    ):
        raise ContractError("Unsupported default feed schema.")
    locales = root.get("locales")
    if not isinstance(locales, dict) or set(locales) != {"cs", "en"}:
        raise ContractError("Default feeds must contain exactly cs and en locales.")
    result: dict[str, tuple[ContractFeed, ...]] = {}
    identifiers: set[str] = set()
    urls: set[str] = set()
    total = 0
    for locale in ("cs", "en"):
        values = locales[locale]
        if not isinstance(values, list) or not values:
            raise ContractError(f"Default feed locale {locale} must not be empty.")
        feeds: list[ContractFeed] = []
        for index, raw in enumerate(values):
            total += 1
            if total > _MAX_DEFAULT_FEEDS:
                raise ContractError("The default feed catalogue is too large.")
            if not isinstance(raw, dict) or set(raw) != {"id", "title", "url", "format"}:
                raise ContractError(f"Default feed {locale}[{index}] has an unsupported shape.")
            feed_id = raw.get("id")
            if (
                not isinstance(feed_id, str)
                or _SLUG.fullmatch(feed_id) is None
                or feed_id in identifiers
            ):
                raise ContractError(
                    f"Default feed {locale}[{index}] has an invalid or duplicate ID."
                )
            identifiers.add(feed_id)
            url = _https_url(raw.get("url"), f"default feed {feed_id}.url")
            if url in urls:
                raise ContractError(f"Duplicate default feed URL: {url}")
            urls.add(url)
            feed_format = raw.get("format")
            if feed_format not in {"rss", "atom"}:
                raise ContractError(f"Default feed {feed_id} has an invalid format.")
            feeds.append(
                ContractFeed(
                    id=feed_id,
                    title=_bounded_text(raw.get("title"), f"default feed {feed_id}.title"),
                    url=url,
                    format=feed_format,
                )
            )
        result[locale] = tuple(feeds)
    return MappingProxyType(result)


def _validate_rss_directory(directory: Path) -> Path:
    path = directory / RSS_DIRECTORY_FILE
    payload = _read_limited(path)
    if _UNSAFE_XML.search(payload):
        raise ContractError("The RSS directory must not contain DTD or entity declarations.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exception:
        raise ContractError("The RSS directory is not valid XML.") from exception
    if root.tag != "opml" or root.get("version") != "2.0":
        raise ContractError("The RSS directory must be OPML 2.0.")
    urls: set[str] = set()
    count = 0
    for node in root.iter("outline"):
        raw_url = node.get("xmlUrl")
        if raw_url is None:
            continue
        count += 1
        title = node.get("title") or node.get("text")
        _bounded_text(title, f"RSS directory feed {count}.title", 2_048)
        if node.get("type") not in {"rss", "atom"}:
            raise ContractError(f"RSS directory feed {count} has an invalid type.")
        if not isinstance(raw_url, str) or len(raw_url) > 2_048:
            raise ContractError(f"RSS directory feed {count} has an invalid URL.")
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ContractError(f"RSS directory feed {count} has an invalid URL.")
        if raw_url in urls:
            raise ContractError(f"Duplicate RSS directory URL: {raw_url}")
        urls.add(raw_url)
    if count == 0:
        raise ContractError("The RSS directory must contain at least one feed.")
    return path


def _load_stations(directory: Path) -> tuple[ContractStation, ...]:
    root = _json_object(directory / GUIDE_STATIONS_FILE)
    expected_keys = {"schemaVersion", "stations"}
    if set(root) == expected_keys | {"$schema"}:
        if root.get("$schema") != "../schemas/guide-stations.schema.json":
            raise ContractError("The guide catalogue has an invalid schema reference.")
    elif set(root) != expected_keys:
        raise ContractError("Unsupported guide station schema.")
    if root.get("schemaVersion") != "1.0.0":
        raise ContractError("Unsupported guide station schema.")
    values = root.get("stations")
    if not isinstance(values, list) or not values or len(values) > _MAX_STATIONS:
        raise ContractError("The guide station array is empty or too large.")
    result: list[ContractStation] = []
    stable_ids: set[str] = set()
    legacy_ids: set[str] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict) or set(raw) != {
            "id", "medium", "displayName", "sortOrder", "family", "aliases", "providers"
        }:
            raise ContractError(f"Guide station {index} has an unsupported shape.")
        station_id = raw.get("id")
        if not isinstance(station_id, str) or _STATION_ID.fullmatch(station_id) is None:
            raise ContractError(f"Guide station {index} has an invalid stable ID.")
        medium = raw.get("medium")
        if medium not in {"television", "radio"} or not station_id.startswith(
            "tv." if medium == "television" else "radio."
        ):
            raise ContractError(f"Guide station {station_id} has an invalid medium.")
        if station_id in stable_ids:
            raise ContractError(f"Duplicate guide station ID: {station_id}")
        stable_ids.add(station_id)
        display_name = _bounded_text(raw.get("displayName"), f"{station_id}.displayName")
        family = _bounded_text(raw.get("family"), f"{station_id}.family", 128)
        sort_order = raw.get("sortOrder")
        if isinstance(sort_order, bool) or not isinstance(sort_order, int) or not 0 <= sort_order <= 1_000_000:
            raise ContractError(f"Guide station {station_id} has an invalid sortOrder.")
        aliases_value = raw.get("aliases")
        if not isinstance(aliases_value, list) or len(aliases_value) > 128:
            raise ContractError(f"Guide station {station_id} has invalid aliases.")
        aliases = tuple(
            _bounded_text(alias, f"{station_id}.aliases", 256)
            for alias in aliases_value
        )
        if len(set(aliases)) != len(aliases):
            raise ContractError(f"Guide station {station_id} has duplicate aliases.")
        providers = _provider_data(raw.get("providers"), station_id)
        station = ContractStation(
            id=station_id,
            medium=medium,
            display_name=display_name,
            sort_order=sort_order,
            family=family,
            aliases=aliases,
            providers=providers,
        )
        supported = (
            bool({"centrum", "sms"}.intersection(providers))
            if medium == "television"
            else bool({"rozhlas", "sms"}.intersection(providers))
        )
        if not supported:
            raise ContractError(f"Guide station {station_id} has no Linux-supported programme provider.")
        for legacy_id in station.legacy_ids:
            if legacy_id in legacy_ids:
                raise ContractError(f"Duplicate provider-specific station ID: {legacy_id}")
            legacy_ids.add(legacy_id)
        result.append(station)
    for medium in ("television", "radio"):
        medium_stations = [station for station in result if station.medium == medium]
        if [station.sort_order for station in medium_stations] != list(
            range(len(medium_stations))
        ):
            raise ContractError(
                f"Guide {medium} sortOrder values must be contiguous and catalogued in order."
            )
    if [station.medium for station in result] != sorted(
        (station.medium for station in result),
        key=lambda medium: 0 if medium == "television" else 1,
    ):
        raise ContractError("Television stations must precede radio stations.")
    return tuple(result)


def load_contract(directory: Path | None = None) -> ContractBundle:
    """Load and validate an immutable vendored contract directory."""

    root = (directory or DEFAULT_CONTRACT_DIRECTORY).resolve()
    lock = _load_lock(root)
    manifest_payload = _read_limited(root / MANIFEST_FILE, _MAX_MANIFEST_BYTES)
    if hashlib.sha256(manifest_payload).hexdigest() != lock.manifest_sha256:
        raise ContractError("The contract lock does not match manifest.sha256.")
    verify_manifest(root)
    _validate_version(root, lock)
    stations = _load_stations(root)
    guide_sources = _load_guide_sources(root)
    default_feeds = _load_default_feeds(root)
    rss_directory_path = _validate_rss_directory(root)
    by_id = {station.id: station for station in stations}
    by_legacy: dict[str, str] = {}
    for station in stations:
        for legacy_id in station.legacy_ids:
            by_legacy[legacy_id] = station.id
    return ContractBundle(
        lock=lock,
        stations=stations,
        station_by_id=MappingProxyType(by_id),
        stable_id_by_legacy_id=MappingProxyType(by_legacy),
        guide_sources=guide_sources,
        guide_source_by_id=MappingProxyType(
            {source.id: source for source in guide_sources}
        ),
        default_feeds_by_locale=default_feeds,
        rss_directory_path=rss_directory_path,
    )


@lru_cache(maxsize=1)
def load_embedded_contract() -> ContractBundle:
    """Load the installed contract once for runtime consumers."""

    return load_contract(DEFAULT_CONTRACT_DIRECTORY)


__all__ = [
    "CONSUMER_VERSION",
    "ContractBundle",
    "ContractError",
    "ContractFeed",
    "ContractGuideSource",
    "ContractLock",
    "ContractStation",
    "DEFAULT_CONTRACT_DIRECTORY",
    "DEFAULT_FEEDS_FILE",
    "GUIDE_GOLDEN_FILE",
    "GUIDE_SOURCES_FILE",
    "GUIDE_STATIONS_FILE",
    "LOCK_FILE",
    "MANIFEST_FILE",
    "RSS_DIRECTORY_FILE",
    "VERSION_FILE",
    "load_contract",
    "load_embedded_contract",
    "verify_manifest",
]
