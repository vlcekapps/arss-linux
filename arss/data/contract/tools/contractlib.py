"""Standard-library validation helpers for the ARSS Contract repository."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable
from urllib.parse import urlsplit
import unicodedata
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "1.0.0"
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
STATION_ID = re.compile(r"^(tv|radio)\.[a-z0-9]+(?:[.-][a-z0-9]+)*$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SOURCE_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
CT_CHANNELS = {"ct1", "ct2", "ct24", "ct4", "ct5", "ct6"}
TEXT_SUFFIXES = {
    ".json", ".md", ".opml", ".py", ".xml", ".html", ".yml", ".yaml",
    ".txt", ".gitattributes", ".gitignore",
}
FORBIDDEN_SUFFIXES = {
    ".apk", ".aab", ".apks", ".idsig", ".jks", ".keystore", ".p12", ".pfx",
    ".pem", ".key", ".wav", ".mp3", ".m4a", ".ogg", ".flac",
}
FORBIDDEN_TEXT = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)\b[A-Z]:\\Users\\"),
    re.compile(r"/(?:Users|home)/[^/\s]+/"),
)


class ContractValidationError(ValueError):
    """Raised when a public contract invariant is violated."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractValidationError(f"Invalid JSON in {path}: {error}") from error
    require(isinstance(value, dict), f"JSON root must be an object: {path}")
    return value


def _keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    require(set(value) == expected, f"Unexpected fields in {where}: expected {sorted(expected)}, got {sorted(value)}")


def _https_url(value: Any, where: str) -> str:
    require(isinstance(value, str) and value.strip() == value, f"{where} must be a trimmed string")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.netloc), f"{where} must be an absolute HTTPS URL")
    require(not parsed.username and not parsed.password, f"{where} must not contain credentials")
    return value


def _http_url(value: Any, where: str) -> str:
    require(isinstance(value, str) and value.strip() == value, f"{where} must be a trimmed string")
    parsed = urlsplit(value)
    require(parsed.scheme in {"http", "https"} and bool(parsed.netloc), f"{where} must be an absolute HTTP(S) URL")
    require(not parsed.username and not parsed.password, f"{where} must not contain credentials")
    return value


def _nullable_https_url(value: Any, where: str) -> None:
    if value is not None:
        _https_url(value, where)


def _trimmed(value: Any, where: str, *, allow_empty: bool = False) -> str:
    require(isinstance(value, str) and value == value.strip(), f"{where} must be a trimmed string")
    require(allow_empty or bool(value), f"{where} must not be blank")
    return value


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            "".join(char for char in decomposed if not unicodedata.combining(char)).casefold(),
        ).split()
    )


def _schema_ref(root: Path, document: dict[str, Any], path: Path) -> None:
    reference = document.get("$schema")
    require(isinstance(reference, str) and reference, f"Missing $schema in {path}")
    target = (path.parent / reference).resolve()
    require(target.is_relative_to(root.resolve()), f"$schema escapes repository in {path}")
    require(target.is_file(), f"Missing schema {reference} referenced by {path}")


def validate_version(root: Path) -> dict[str, Any]:
    path = root / "version.json"
    value = load_json(path)
    _schema_ref(root, value, path)
    _keys(value, {"$schema", "contractVersion", "releasedAt", "minimumConsumerVersion", "reference"}, "version.json")
    require(bool(SEMVER.fullmatch(value["contractVersion"])), "contractVersion must be SemVer")
    require(bool(SEMVER.fullmatch(value["minimumConsumerVersion"])), "minimumConsumerVersion must be SemVer")
    try:
        date.fromisoformat(value["releasedAt"])
    except (TypeError, ValueError) as error:
        raise ContractValidationError("releasedAt must be an ISO date") from error
    reference = value["reference"]
    require(isinstance(reference, dict), "reference must be an object")
    _keys(reference, {"repository", "tag", "commit"}, "version.reference")
    _https_url(reference["repository"], "version.reference.repository")
    _trimmed(reference["tag"], "version.reference.tag")
    require(isinstance(reference["commit"], str) and bool(COMMIT.fullmatch(reference["commit"])), "reference.commit must be 40 lowercase hexadecimal characters")
    return value


def validate_default_feeds(root: Path) -> dict[str, Any]:
    path = root / "catalogs" / "default_feeds.json"
    value = load_json(path)
    _schema_ref(root, value, path)
    _keys(value, {"$schema", "schemaVersion", "locales"}, str(path.relative_to(root)))
    require(value["schemaVersion"] == SCHEMA_VERSION, "Unexpected default feed schemaVersion")
    locales = value["locales"]
    require(isinstance(locales, dict) and set(locales) == {"cs", "en"}, "Default feeds must contain exactly cs and en locales")
    known_ids: set[str] = set()
    known_urls: set[str] = set()
    for locale in ("cs", "en"):
        feeds = locales[locale]
        require(isinstance(feeds, list) and feeds, f"Default feed locale {locale} must not be empty")
        for index, feed in enumerate(feeds):
            where = f"default feed {locale}[{index}]"
            require(isinstance(feed, dict), f"{where} must be an object")
            _keys(feed, {"id", "title", "url", "format"}, where)
            feed_id = _trimmed(feed["id"], f"{where}.id")
            require(bool(SLUG.fullmatch(feed_id)), f"Invalid feed ID: {feed_id}")
            require(feed_id not in known_ids, f"Duplicate feed ID: {feed_id}")
            known_ids.add(feed_id)
            title = _trimmed(feed["title"], f"{where}.title")
            url = _https_url(feed["url"], f"{where}.url")
            require(url not in known_urls, f"Duplicate default feed URL: {url}")
            known_urls.add(url)
            require(feed["format"] in {"rss", "atom"}, f"Invalid feed format in {where}")

    opml_path = root / "catalogs" / "rss_directory.opml"
    raw = opml_path.read_bytes()
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "OPML must not contain DTD or entities")
    try:
        opml = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ContractValidationError(f"Invalid OPML: {error}") from error
    require(opml.tag == "opml" and opml.attrib.get("version") == "2.0", "rss_directory.opml must be OPML 2.0")
    observed_opml = [
        (node.attrib.get("title") or node.attrib.get("text") or "", node.attrib["xmlUrl"], node.attrib.get("type", ""))
        for node in opml.iter("outline")
        if "xmlUrl" in node.attrib
    ]
    require(bool(observed_opml), "rss_directory.opml must contain at least one feed")
    opml_urls: set[str] = set()
    for index, (title, url, feed_format) in enumerate(observed_opml):
        _trimmed(title, f"OPML feed[{index}].title")
        _http_url(url, f"OPML feed[{index}].xmlUrl")
        require(feed_format in {"rss", "atom"}, f"Invalid OPML type at feed[{index}]")
        require(url not in opml_urls, f"Duplicate OPML URL: {url}")
        opml_urls.add(url)
    return value


def validate_stations(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    path = root / "catalogs" / "guide_stations.json"
    value = load_json(path)
    _schema_ref(root, value, path)
    _keys(value, {"$schema", "schemaVersion", "stations"}, str(path.relative_to(root)))
    require(value["schemaVersion"] == SCHEMA_VERSION, "Unexpected guide station schemaVersion")
    stations = value["stations"]
    require(isinstance(stations, list) and stations, "Station catalogue must not be empty")
    ids: set[str] = set()
    orders: dict[str, set[int]] = {"television": set(), "radio": set()}
    legacy: dict[str, str] = {}
    allowed_provider_keys = {"centrum", "ct", "rozhlas", "sms"}
    for index, station in enumerate(stations):
        where = f"station[{index}]"
        require(isinstance(station, dict), f"{where} must be an object")
        _keys(station, {"id", "medium", "displayName", "sortOrder", "family", "aliases", "providers"}, where)
        station_id = _trimmed(station["id"], f"{where}.id")
        require(bool(STATION_ID.fullmatch(station_id)), f"Invalid station ID: {station_id}")
        require(station_id not in ids, f"Duplicate station ID: {station_id}")
        ids.add(station_id)
        medium = station["medium"]
        require(medium in {"television", "radio"}, f"Invalid medium for {station_id}")
        require(station_id.startswith("tv.") == (medium == "television"), f"ID/medium mismatch for {station_id}")
        display_name = _trimmed(station["displayName"], f"{where}.displayName")
        order = station["sortOrder"]
        require(isinstance(order, int) and not isinstance(order, bool) and order >= 0, f"Invalid sortOrder for {station_id}")
        require(order not in orders[medium], f"Duplicate sortOrder for {medium}: {order}")
        orders[medium].add(order)
        family = station["family"]
        require(isinstance(family, str) and bool(SLUG.fullmatch(family)), f"Invalid family for {station_id}")
        aliases = station["aliases"]
        require(isinstance(aliases, list), f"aliases must be an array for {station_id}")
        folded_aliases: set[str] = set()
        for alias in aliases:
            alias = _trimmed(alias, f"alias for {station_id}")
            folded_alias = _fold(alias)
            require(folded_alias != _fold(display_name), f"Redundant alias for {station_id}: {alias}")
            require(folded_alias not in folded_aliases, f"Duplicate alias for {station_id}: {alias}")
            folded_aliases.add(folded_alias)
        providers = station["providers"]
        require(isinstance(providers, dict) and providers, f"providers must not be empty for {station_id}")
        require(set(providers).issubset(allowed_provider_keys), f"Unknown provider for {station_id}")
        require(not (medium == "television" and "rozhlas" in providers), f"TV station has radio provider: {station_id}")
        require(not (medium == "radio" and ({"centrum", "ct"} & set(providers))), f"Radio station has TV provider: {station_id}")
        provider_legacy: list[str] = []
        if "centrum" in providers:
            provider = providers["centrum"]
            require(isinstance(provider, dict) and set(provider).issubset({"id", "slug"}) and "id" in provider, f"Invalid Centrum provider for {station_id}")
            require(isinstance(provider["id"], str) and provider["id"].isdigit(), f"Invalid Centrum ID for {station_id}")
            if "slug" in provider:
                require(isinstance(provider["slug"], str) and bool(SLUG.fullmatch(provider["slug"])), f"Invalid Centrum slug for {station_id}")
            provider_legacy.append(f"centrum:{provider['id']}")
        if "ct" in providers:
            provider = providers["ct"]
            require(isinstance(provider, dict) and set(provider) == {"channel"} and provider["channel"] in CT_CHANNELS, f"Invalid CT provider for {station_id}")
            require("centrum" in providers, f"CT station must retain a Centrum mapping: {station_id}")
        if "rozhlas" in providers:
            provider = providers["rozhlas"]
            require(isinstance(provider, dict) and set(provider) == {"id"}, f"Invalid Rozhlas provider for {station_id}")
            require(isinstance(provider["id"], str) and bool(SLUG.fullmatch(provider["id"])), f"Invalid Rozhlas ID for {station_id}")
            provider_legacy.append(f"rozhlas:{provider['id']}")
        if "sms" in providers:
            provider = providers["sms"]
            require(isinstance(provider, dict) and set(provider) == {"name"}, f"Invalid SMS provider for {station_id}")
            provider_legacy.append(f"sms:{_trimmed(provider['name'], f'{where}.providers.sms.name')}")
        for legacy_id in provider_legacy:
            if legacy_id in legacy:
                raise ContractValidationError(
                    f"Legacy provider ID collision: {legacy_id} maps to {legacy[legacy_id]} and {station_id}"
                )
            legacy[legacy_id] = station_id
    for medium, medium_orders in orders.items():
        require(medium_orders == set(range(len(medium_orders))), f"sortOrder values for {medium} must be contiguous from zero")
    return value, legacy


def validate_sources(root: Path) -> dict[str, Any]:
    path = root / "catalogs" / "guide_sources.json"
    value = load_json(path)
    _schema_ref(root, value, path)
    _keys(value, {"$schema", "schemaVersion", "sources"}, str(path.relative_to(root)))
    require(value["schemaVersion"] == SCHEMA_VERSION, "Unexpected guide source schemaVersion")
    sources = value["sources"]
    require(isinstance(sources, list) and sources, "Guide sources must not be empty")
    ids: set[str] = set()
    roles: set[tuple[str, str]] = set()
    for index, source in enumerate(sources):
        where = f"guide source[{index}]"
        require(isinstance(source, dict), f"{where} must be an object")
        _keys(source, {"id", "medium", "role", "baseUrl", "format", "priority", "enabled"}, where)
        source_id = _trimmed(source["id"], f"{where}.id")
        require(bool(SOURCE_ID.fullmatch(source_id)) and source_id not in ids, f"Invalid or duplicate source ID: {source_id}")
        ids.add(source_id)
        require(source["medium"] in {"television", "radio"}, f"Invalid source medium: {source_id}")
        require(source["role"] in {"discovery", "schedule"}, f"Invalid source role: {source_id}")
        roles.add((source["medium"], source["role"]))
        _https_url(source["baseUrl"], f"{where}.baseUrl")
        require(source["format"] in {"json", "xml", "html"}, f"Invalid source format: {source_id}")
        require(isinstance(source["priority"], int) and not isinstance(source["priority"], bool) and 0 <= source["priority"] <= 100, f"Invalid source priority: {source_id}")
        require(isinstance(source["enabled"], bool), f"Invalid enabled value: {source_id}")
    require(roles == {("television", "discovery"), ("television", "schedule"), ("radio", "discovery"), ("radio", "schedule")}, "Each medium requires discovery and schedule sources")
    return value


def _validate_normalized_station_document(root: Path, path: Path, station_ids: set[str]) -> dict[str, Any]:
    value = load_json(path)
    _schema_ref(root, value, path)
    _keys(value, {"$schema", "schemaVersion", "stations"}, str(path.relative_to(root)))
    require(value["schemaVersion"] == SCHEMA_VERSION and isinstance(value["stations"], list), f"Invalid normalized guide document: {path}")
    for index, station in enumerate(value["stations"]):
        _keys(station, {"id", "medium", "displayName", "sortOrder"}, f"{path.name}.stations[{index}]")
        require(station["id"] in station_ids, f"Unknown station in {path}: {station['id']}")
        _trimmed(station["displayName"], f"{path.name}.displayName")
        require(station["medium"] in {"television", "radio"}, f"Invalid medium in {path}")
        require(isinstance(station["sortOrder"], int) and station["sortOrder"] >= 0, f"Invalid sortOrder in {path}")
    return value


def validate_goldens(root: Path, catalog: dict[str, Any], legacy: dict[str, str]) -> None:
    stations = catalog["stations"]
    station_by_id = {station["id"]: station for station in stations}
    station_ids = set(station_by_id)
    projection = [{key: station[key] for key in ("id", "medium", "displayName", "sortOrder")} for station in stations]
    full = _validate_normalized_station_document(root, root / "golden" / "guide" / "stations.normalized.json", station_ids)
    require(full["stations"] == projection, "stations.normalized.json must exactly project guide_stations.json")

    centrum_raw = load_json(root / "fixtures" / "guide" / "centrum_channels.json")
    centrum_ids = [
        f"centrum:{item['id']}"
        for item in centrum_raw.values()
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        and isinstance(item.get("name"), str) and item["name"].strip()
    ]
    rozhlas_raw = load_json(root / "fixtures" / "guide" / "rozhlas_stations.json")
    require(isinstance(rozhlas_raw.get("data"), list), "Rozhlas station fixture requires a data array")
    rozhlas_ids = [
        f"rozhlas:{item['id']}"
        for item in rozhlas_raw["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        and isinstance(item.get("name"), str) and item["name"].strip()
    ]
    for name, legacy_ids in (("centrum_channels", centrum_ids), ("rozhlas_stations", rozhlas_ids)):
        expected = _validate_normalized_station_document(root, root / "golden" / "guide" / f"{name}.json", station_ids)
        actual = [
            {key: station_by_id[legacy[legacy_id]][key] for key in ("id", "medium", "displayName", "sortOrder")}
            for legacy_id in legacy_ids
        ]
        require(expected["stations"] == actual, f"{name} golden does not match provider fixture and catalogue")

    for path in sorted((root / "golden" / "feeds").glob("*.json")):
        value = load_json(path)
        _schema_ref(root, value, path)
        _keys(value, {"$schema", "schemaVersion", "sourceUrl", "title", "items"}, str(path.relative_to(root)))
        require(value["schemaVersion"] == SCHEMA_VERSION, f"Invalid schemaVersion in {path}")
        _https_url(value["sourceUrl"], f"{path.name}.sourceUrl")
        _trimmed(value["title"], f"{path.name}.title", allow_empty=True)
        require(isinstance(value["items"], list), f"items must be an array in {path}")
        previous_date: int | None = None
        saw_undated = False
        for index, item in enumerate(value["items"]):
            where = f"{path.name}.items[{index}]"
            _keys(item, {"title", "url", "sourceId", "publishedText", "publishedAtMillis", "mediaUrl", "mediaType", "durationText"}, where)
            _trimmed(item["title"], f"{where}.title", allow_empty=True)
            _https_url(item["url"], f"{where}.url")
            for field in ("sourceId", "publishedText", "mediaType", "durationText"):
                require(item[field] is None or isinstance(item[field], str), f"Invalid {field} in {where}")
            published = item["publishedAtMillis"]
            require(published is None or (isinstance(published, int) and not isinstance(published, bool) and published >= 0), f"Invalid publishedAtMillis in {where}")
            if published is None:
                saw_undated = True
            else:
                require(not saw_undated, f"Dated item follows undated item in {path}")
                if previous_date is not None:
                    require(published <= previous_date, f"Feed golden is not newest-first: {path}")
                previous_date = published
            _nullable_https_url(item["mediaUrl"], f"{where}.mediaUrl")

    for path in sorted((root / "golden" / "guide").glob("*_program*.json")):
        value = load_json(path)
        _schema_ref(root, value, path)
        _keys(value, {"$schema", "schemaVersion", "source", "stationId", "date", "programs"}, str(path.relative_to(root)))
        require(value["schemaVersion"] == SCHEMA_VERSION and value["stationId"] in station_ids, f"Invalid programme header in {path}")
        _trimmed(value["source"], f"{path.name}.source")
        try:
            date.fromisoformat(value["date"])
        except (TypeError, ValueError) as error:
            raise ContractValidationError(f"Invalid date in {path}") from error
        require(isinstance(value["programs"], list), f"programs must be an array in {path}")
        previous_start: int | None = None
        for index, program in enumerate(value["programs"]):
            where = f"{path.name}.programs[{index}]"
            _keys(program, {"id", "startMillis", "endMillis", "title", "description", "audioDescription", "audioDescriptionKnown", "programUrl", "archiveUrl"}, where)
            _trimmed(program["id"], f"{where}.id")
            _trimmed(program["title"], f"{where}.title")
            _trimmed(program["description"], f"{where}.description", allow_empty=True)
            start = program["startMillis"]
            end = program["endMillis"]
            require(isinstance(start, int) and not isinstance(start, bool) and start >= 0, f"Invalid start in {where}")
            require(end is None or (isinstance(end, int) and not isinstance(end, bool) and end >= start), f"Invalid end in {where}")
            require(previous_start is None or start >= previous_start, f"Programs are not chronological in {path}")
            previous_start = start
            require(isinstance(program["audioDescription"], bool) and isinstance(program["audioDescriptionKnown"], bool), f"Invalid AD flags in {where}")
            require(not program["audioDescription"] or program["audioDescriptionKnown"], f"AD true while metadata is unknown in {where}")
            _nullable_https_url(program["programUrl"], f"{where}.programUrl")
            _nullable_https_url(program["archiveUrl"], f"{where}.archiveUrl")

    for path in sorted((root / "fixtures").rglob("*.xml")):
        raw = path.read_bytes()
        require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), f"Unsafe XML fixture: {path}")
        try:
            ET.fromstring(raw)
        except ET.ParseError as error:
            raise ContractValidationError(f"Invalid XML fixture {path}: {error}") from error


def validate_schemas(root: Path) -> None:
    schema_paths = sorted((root / "schemas").glob("*.schema.json"))
    require(bool(schema_paths), "No JSON schemas found")
    ids: set[str] = set()
    for path in schema_paths:
        value = load_json(path)
        require(value.get("$schema") == "https://json-schema.org/draft/2020-12/schema", f"Schema must use Draft 2020-12: {path}")
        schema_id = _https_url(value.get("$id"), f"{path.name}.$id")
        require(schema_id not in ids, f"Duplicate schema $id: {schema_id}")
        ids.add(schema_id)
        require(value.get("type") == "object", f"Schema root must describe an object: {path}")


def _ignored_manifest_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return (
        relative == "manifest.sha256"
        or ".git" in parts
        or "__pycache__" in parts
        or relative in {"live-drift-report.json", "live-drift-report.md"}
        or relative.endswith((".pyc", ".pyo"))
    )


def manifest_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
            and not _ignored_manifest_path(path.relative_to(root).as_posix())
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def write_manifest(root: Path) -> None:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}"
        for path in manifest_files(root)
    ]
    (root / "manifest.sha256").write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def validate_manifest(root: Path) -> None:
    path = root / "manifest.sha256"
    require(path.is_file(), "manifest.sha256 is missing")
    observed: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        require(len(line) > 66 and line[64:66] == "  ", f"Malformed manifest line {number}")
        digest, relative = line[:64], line[66:]
        require(bool(SHA256.fullmatch(digest)), f"Invalid SHA-256 on manifest line {number}")
        pure = PurePosixPath(relative)
        require(not pure.is_absolute() and ".." not in pure.parts and "\\" not in relative, f"Unsafe manifest path: {relative}")
        require(relative not in observed, f"Duplicate manifest path: {relative}")
        observed[relative] = digest
    expected_paths = {file.relative_to(root).as_posix() for file in manifest_files(root)}
    require(set(observed) == expected_paths, "manifest.sha256 file list does not match repository contract files")
    for relative, expected in observed.items():
        actual = hashlib.sha256((root / PurePosixPath(relative)).read_bytes()).hexdigest()
        require(actual == expected, f"Checksum mismatch: {relative}")


def validate_public_repository(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        require(path.suffix.casefold() not in FORBIDDEN_SUFFIXES, f"Forbidden binary or secret material: {relative}")
        lowered_name = path.name.casefold()
        require(lowered_name not in {"keystore.properties", "local.properties", ".env", "google-services.json"}, f"Forbidden local configuration: {relative}")
        if path.suffix.casefold() in TEXT_SUFFIXES or path.name in {"LICENSE", "AGENTS.md", "README.md", ".gitattributes", ".gitignore", "manifest.sha256"}:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError as error:
                raise ContractValidationError(f"Text file is not UTF-8: {relative}") from error
            for pattern in FORBIDDEN_TEXT:
                require(pattern.search(text) is None, f"Potential secret or private path in {relative}")


def validate_contract(root: Path, *, verify_manifest: bool = True) -> dict[str, int | str]:
    root = root.resolve()
    version = validate_version(root)
    validate_schemas(root)
    feeds = validate_default_feeds(root)
    catalog, legacy = validate_stations(root)
    sources = validate_sources(root)
    validate_goldens(root, catalog, legacy)
    validate_public_repository(root)
    if verify_manifest:
        validate_manifest(root)
    stations = catalog["stations"]
    return {
        "contractVersion": version["contractVersion"],
        "stations": len(stations),
        "televisionStations": sum(item["medium"] == "television" for item in stations),
        "radioStations": sum(item["medium"] == "radio" for item in stations),
        "legacyIds": len(legacy),
        "defaultFeeds": sum(len(items) for items in feeds["locales"].values()),
        "guideSources": len(sources["sources"]),
    }
