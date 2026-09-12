"""Platform-independent Czech television and radio programme guide.

All timestamps are Unix milliseconds.  Broadcasters' civil dates and clock
times are interpreted in Europe/Prague regardless of the host time zone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from enum import Enum
import html as html_module
import json
import re
import threading
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote_plus, urldefrag, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from .contract import ContractBundle, ContractError, ContractStation, load_embedded_contract
from .guide_catalog import (
    GuideCatalogParseError,
    parse_centrum_catalog,
    parse_rozhlas_catalog,
)
from .search import normalize_search_text, search_text_matches

if TYPE_CHECKING:
    import requests


TIME_ZONE_ID = "Europe/Prague"
PRAGUE_TIME_ZONE = ZoneInfo(TIME_ZONE_ID)

JSON_ACCEPT = "application/json, text/json;q=0.9, */*;q=0.1"
XML_ACCEPT = "application/xml, text/xml;q=0.9, */*;q=0.1"
HTML_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.1"

def _contract_source_url(
    source_id: str,
    *,
    medium: str,
    role: str,
    source_format: str,
) -> str:
    source = load_embedded_contract().guide_source_by_id.get(source_id)
    if source is None or (
        source.medium,
        source.role,
        source.format,
        source.enabled,
    ) != (medium, role, source_format, True):
        raise ContractError(f"Required guide source is unavailable: {source_id}")
    return source.base_url


CENTRUM_CHANNELS_URL = _contract_source_url(
    "centrum.channels", medium="television", role="discovery", source_format="json"
)
CENTRUM_SCHEDULE_BASE = _contract_source_url(
    "centrum.schedule", medium="television", role="schedule", source_format="json"
)
CT_WEB_SCHEDULE_BASE = _contract_source_url(
    "ct.web-schedule", medium="television", role="schedule", source_format="html"
)
CT_SCHEDULE_URL = _contract_source_url(
    "ct.schedule", medium="television", role="schedule", source_format="xml"
)
ROZHLAS_STATIONS_URL = _contract_source_url(
    "rozhlas.stations", medium="radio", role="discovery", source_format="json"
)
ROZHLAS_SCHEDULE_BASE = _contract_source_url(
    "rozhlas.schedule", medium="radio", role="schedule", source_format="json"
)
SMS_SCHEDULE_URL = _contract_source_url(
    "sms.television-schedule",
    medium="television",
    role="schedule",
    source_format="html",
)
SMS_RADIO_SCHEDULE_URL = _contract_source_url(
    "sms.radio-schedule", medium="radio", role="schedule", source_format="html"
)
if SMS_RADIO_SCHEDULE_URL != SMS_SCHEDULE_URL:
    raise ContractError("Linux requires the television and radio SMS schedule base URLs to match.")

_TELEVISION_SOURCE_ORDER = (
    "ct.schedule",
    "ct.web-schedule",
    "centrum.schedule",
    "sms.television-schedule",
)
if tuple(
    sorted(
        _TELEVISION_SOURCE_ORDER,
        key=lambda source_id: load_embedded_contract().guide_source_by_id[source_id].priority,
    )
) != _TELEVISION_SOURCE_ORDER:
    raise ContractError(
        "The guide source priorities do not preserve CT XML, web, Centrum, SMS order."
    )
if not (
    load_embedded_contract().guide_source_by_id["rozhlas.schedule"].priority
    < load_embedded_contract().guide_source_by_id["sms.radio-schedule"].priority
):
    raise ContractError("The guide source priorities do not preserve Rozhlas before SMS.")

_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]*>", re.IGNORECASE | re.DOTALL)
_CLOCK = re.compile(r"^\s*(\d{1,2})[.:](\d{2})(?::\d{2})?\s*$")
_UNSAFE_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_EMPTY_PROVIDERS: Mapping[str, Mapping[str, str]] = MappingProxyType({})


class GuideMedium(str, Enum):
    TELEVISION = "television"
    RADIO = "radio"


@dataclass(frozen=True, slots=True, order=True)
class GuideDate:
    year: int
    month: int
    day: int

    def __post_init__(self) -> None:
        if not 1900 <= self.year <= 2200:
            raise ValueError(f"Invalid guide year: {self.year}")
        try:
            Date(self.year, self.month, self.day)
        except ValueError as exception:
            raise ValueError(
                f"Invalid guide date: {self.year}-{self.month}-{self.day}"
            ) from exception

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    def ct(self) -> str:
        return f"{self.day:02d}.{self.month:02d}.{self.year:04d}"


@dataclass(frozen=True, slots=True)
class GuideStation:
    id: str
    name: str
    medium: GuideMedium
    providers: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: _EMPTY_PROVIDERS,
        compare=False,
        repr=False,
    )
    aliases: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Station id must not be blank.")
        if not self.name.strip():
            raise ValueError("Station name must not be blank.")
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("Station aliases must not be blank.")


def guide_contract() -> ContractBundle:
    """Return the verified embedded contract, loaded once per process."""

    return load_embedded_contract()


def canonical_station_id(station_id: str) -> str:
    """Migrate a provider-specific station ID to its stable contract ID."""

    return guide_contract().resolve_station_id(station_id)


def station_has_provider(station: GuideStation | Any, provider: str) -> bool:
    """Return whether a stable or legacy station uses a provider."""

    providers = getattr(station, "providers", None)
    if isinstance(providers, Mapping) and provider in providers:
        return True
    contract_station = _contract_station(str(getattr(station, "id", "")))
    return contract_station is not None and provider in contract_station.providers


def _contract_station(station_id: str) -> ContractStation | None:
    contract = guide_contract()
    return contract.station_by_id.get(contract.resolve_station_id(station_id))


def _guide_station(station: ContractStation) -> GuideStation:
    return GuideStation(
        id=station.id,
        name=station.display_name,
        medium=GuideMedium(station.medium),
        providers=station.providers,
        aliases=station.aliases,
    )


def _fallback_stations(medium: GuideMedium) -> tuple[GuideStation, ...]:
    return tuple(
        _guide_station(station)
        for station in guide_contract().stations
        if station.medium == medium.value
    )


def _canonical_live_station(station: GuideStation) -> GuideStation:
    contract_station = _contract_station(station.id)
    if contract_station is None:
        return station
    return GuideStation(
        id=contract_station.id,
        name=contract_station.display_name,
        medium=station.medium,
        providers=contract_station.providers,
        aliases=contract_station.aliases,
    )


def _normalized_station_text(value: str) -> str:
    return normalize_search_text(value)


def station_matches_search(query: str, station: GuideStation) -> bool:
    """Match a visible station name and its contract aliases exactly as specified."""

    return search_text_matches(query, station.name, *station.aliases)


def _natural_station_key(value: str) -> tuple[tuple[int, int | str], ...]:
    normalized = _normalized_station_text(value)
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", normalized)
        if part
    )


def _television_station_family(name: str) -> str | None:
    normalized = _normalized_station_text(name)
    words = set(re.findall(r"[a-z0-9]+", normalized))
    if normalized.startswith("ct"):
        return "ct"
    if "nova" in words or normalized.startswith("nova"):
        return "nova"
    if "prima" in words or normalized.startswith("prima"):
        return "prima"
    if "barrandov" in words:
        return "barrandov"
    if normalized.startswith("hbo"):
        return "hbo"
    if "cinemax" in words:
        return "cinemax"
    if "animal" in words or "discovery" in words:
        return "discovery"
    if "eurosport" in words or normalized.startswith("eurosport"):
        return "eurosport"
    if "oneplay" in words or normalized.startswith("oneplay"):
        return "oneplay"
    return None


def _television_station_sort_key(station: GuideStation) -> tuple[Any, ...]:
    known = _contract_station(station.id)
    if known is not None:
        return (
            known.sort_order * 2,
            0,
            0,
            (),
            known.id,
        )
    television = tuple(
        value
        for value in guide_contract().stations
        if value.medium == GuideMedium.TELEVISION.value
    )
    family = _television_station_family(station.name)
    family_end = max(
        (
            value.sort_order
            for value in television
            if family is not None and value.family == family
        ),
        default=max(value.sort_order for value in television),
    )
    return (
        family_end * 2 + 1,
        1,
        0,
        _natural_station_key(station.name),
        _normalized_station_text(station.id),
    )


def order_guide_stations(
    stations: Iterable[GuideStation],
    medium: GuideMedium | str,
) -> list[GuideStation]:
    """Return stations in desktop order without separating names from IDs."""

    selected_medium = GuideMedium(medium)
    result = [
        station
        for station in stations
        if GuideMedium(station.medium) is selected_medium
    ]
    if selected_medium is GuideMedium.TELEVISION:
        return sorted(result, key=_television_station_sort_key)
    return sorted(
        result,
        key=lambda station: (
            (0, known.sort_order, (), known.id)
            if (known := _contract_station(station.id)) is not None
            else (
                1,
                0,
                _natural_station_key(station.name),
                _normalized_station_text(station.id),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class GuideProgramEntry:
    id: str
    start_millis: int
    end_millis: int | None
    title: str
    description: str
    audio_description: bool = False
    audio_description_known: bool = False
    program_url: str | None = None
    archive_url: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Program id must not be blank.")
        if not self.title.strip():
            raise ValueError("Program title must not be blank.")
        if self.audio_description and not self.audio_description_known:
            raise ValueError(
                "Audio description cannot be present when its metadata is unknown."
            )
        if self.end_millis is not None and self.end_millis < self.start_millis:
            raise ValueError("Program end must not precede its start.")


class GuideTime:
    TIME_ZONE_ID = TIME_ZONE_ID

    @staticmethod
    def date_at(instant_millis: int) -> GuideDate:
        value = datetime.fromtimestamp(instant_millis / 1000, timezone.utc).astimezone(
            PRAGUE_TIME_ZONE
        )
        return GuideDate(value.year, value.month, value.day)

    @staticmethod
    def datetime(date: GuideDate, hour: int = 12) -> datetime:
        value = _local_datetime(date, hour, 0)
        if value is None:
            raise ValueError(f"Invalid local guide time: {date.iso()} {hour}:00")
        return value

    @staticmethod
    def format_date(date: GuideDate, pattern: str = "%Y-%m-%d") -> str:
        return GuideTime.datetime(date).strftime(pattern)

    @staticmethod
    def format_instant(instant_millis: int, pattern: str = "%Y-%m-%d %H:%M") -> str:
        return datetime.fromtimestamp(
            instant_millis / 1000, timezone.utc
        ).astimezone(PRAGUE_TIME_ZONE).strftime(pattern)


def guide_date_at(instant_millis: int) -> GuideDate:
    return GuideTime.date_at(instant_millis)


class GuideException(OSError):
    pass


class GuideNetworkException(GuideException):
    pass


class GuideHttpException(GuideException):
    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = status_code
        suffix = f": {message}" if message.strip() else ""
        super().__init__(f"HTTP {status_code}{suffix}")


class GuideParseException(GuideException):
    pass


class GuideDataSource(Protocol):
    def get(self, url: str, accept: str) -> bytes: ...


class GuideHttpClient:
    """HTTPS-only bounded guide client with explicit redirect handling."""

    USER_AGENT = "ARSS/Linux"
    DEFAULT_CONNECT_TIMEOUT = 10.0
    DEFAULT_READ_TIMEOUT = 20.0
    DEFAULT_MAXIMUM_RESPONSE_BYTES = 4 * 1024 * 1024
    DEFAULT_MAXIMUM_REDIRECTS = 5

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        maximum_response_bytes: int = DEFAULT_MAXIMUM_RESPONSE_BYTES,
        maximum_redirects: int = DEFAULT_MAXIMUM_REDIRECTS,
    ) -> None:
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum_response_bytes must be positive")
        if maximum_redirects < 0:
            raise ValueError("maximum_redirects must not be negative")
        import requests as requests_module

        self._requests = requests_module
        self.session = session or requests_module.Session()
        self._owns_session = session is None
        self._cancelled = threading.Event()
        self._response_lock = threading.RLock()
        self._active_responses: list[object] = []
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_redirects = maximum_redirects

    def get(self, url: str, accept: str) -> bytes:
        self._require_not_cancelled()
        current = _require_https_url(url)
        visited: set[str] = set()
        redirects = 0
        while True:
            self._require_not_cancelled()
            identity = urldefrag(current)[0]
            if identity in visited:
                raise GuideNetworkException(
                    "The guide server created a redirect loop."
                )
            visited.add(identity)
            try:
                response = self.session.get(
                    current,
                    headers={
                        "User-Agent": self.USER_AGENT,
                        "Accept": accept,
                        "Accept-Encoding": "gzip",
                    },
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=False,
                    stream=True,
                )
            except self._requests.RequestException as exception:
                raise GuideNetworkException(
                    "Could not open the guide connection."
                ) from exception

            self._register_response(response)
            try:
                self._require_not_cancelled()
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location", "").strip()
                    if not location:
                        raise GuideNetworkException(
                            "The guide redirect has no destination."
                        )
                    if redirects >= self.maximum_redirects:
                        raise GuideNetworkException(
                            "The guide server exceeded the redirect limit."
                        )
                    current = _require_https_url(urljoin(current, location))
                    redirects += 1
                    continue
                if not 200 <= response.status_code <= 299:
                    raise GuideHttpException(
                        response.status_code, response.reason or ""
                    )
                declared = response.headers.get("Content-Length", "").strip()
                if declared.isdigit() and int(declared) > self.maximum_response_bytes:
                    raise GuideNetworkException(
                        f"The guide response exceeds {self.maximum_response_bytes} bytes."
                    )
                return self._read_limited(response)
            finally:
                response.close()
                self._unregister_response(response)

    def cancel(self) -> None:
        """Interrupt streamed responses as far as the HTTP backend permits."""

        self._cancelled.set()
        with self._response_lock:
            responses = tuple(self._active_responses)
        for response in responses:
            try:
                response.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def close(self) -> None:
        self.cancel()
        if self._owns_session:
            self.session.close()

    def _require_not_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise GuideNetworkException("The guide request was cancelled.")

    def _register_response(self, response: object) -> None:
        with self._response_lock:
            self._active_responses.append(response)

    def _unregister_response(self, response: object) -> None:
        with self._response_lock:
            try:
                self._active_responses.remove(response)
            except ValueError:
                pass

    def _read_limited(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                self._require_not_cancelled()
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.maximum_response_bytes:
                    raise GuideNetworkException(
                        f"The guide response exceeds {self.maximum_response_bytes} bytes."
                    )
                chunks.append(chunk)
        except self._requests.RequestException as exception:
            raise GuideNetworkException("Could not read the guide response.") from exception
        return b"".join(chunks)


def _require_https_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exception:
        raise GuideNetworkException("The guide URL is invalid.") from exception
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise GuideNetworkException("Guide connections must use HTTPS.")
    return raw


def parse_iso_millis(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw[-1:].casefold() == "z":
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return int(parsed.timestamp() * 1000)


def parse_ct_date(value: str | None, fallback: GuideDate) -> GuideDate:
    try:
        year, month, day = (int(part) for part in (value or "").strip().split("-"))
        return GuideDate(year, month, day)
    except (TypeError, ValueError):
        return fallback


def local_millis(
    date: GuideDate,
    hour: int,
    minute: int,
    second: int = 0,
    *,
    day_offset: int = 0,
) -> int | None:
    try:
        shifted = Date(date.year, date.month, date.day) + timedelta(days=day_offset)
        guide_date = GuideDate(shifted.year, shifted.month, shifted.day)
    except (OverflowError, ValueError):
        return None
    value = _local_datetime(guide_date, hour, minute, second)
    return int(value.timestamp() * 1000) if value is not None else None


def _local_datetime(
    date: GuideDate, hour: int, minute: int, second: int = 0
) -> datetime | None:
    try:
        naive = datetime(date.year, date.month, date.day, hour, minute, second)
    except ValueError:
        return None
    aware = naive.replace(tzinfo=PRAGUE_TIME_ZONE)
    # zoneinfo permits nonexistent wall times.  A UTC round trip detects them.
    round_trip = aware.astimezone(timezone.utc).astimezone(PRAGUE_TIME_ZONE)
    if round_trip.replace(tzinfo=None) != naive:
        return None
    return aware


def parse_clock(value: str | None) -> tuple[int, int] | None:
    match = _CLOCK.fullmatch(value or "")
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour not in range(24) or minute not in range(60):
        return None
    return hour, minute


def clean_text(value: str | None) -> str:
    return _WHITESPACE.sub(" ", (value or "").replace("\u00a0", " ")).strip()


def decode_html_entities(value: str) -> str:
    return html_module.unescape(value)


def clean_html(value: str | None) -> str:
    return clean_text(decode_html_entities(_HTML_TAG.sub(" ", value or "")))


def https_url(value: str | None, base: str | None = None) -> str | None:
    raw = decode_html_entities(value or "").strip()
    if not raw:
        return None
    try:
        resolved = urljoin(base, raw) if base else raw
        parts = urlsplit(resolved.replace(" ", "%20"))
    except ValueError:
        return None
    if parts.scheme.casefold() != "https" or not parts.netloc:
        return None
    return urlunsplit(parts)


def infer_missing_ends(entries: Iterable[GuideProgramEntry]) -> list[GuideProgramEntry]:
    sorted_entries = sorted(entries, key=lambda entry: (entry.start_millis, entry.id))
    result: list[GuideProgramEntry] = []
    for index, entry in enumerate(sorted_entries):
        if entry.end_millis is not None:
            result.append(entry)
            continue
        next_start = (
            sorted_entries[index + 1].start_millis
            if index + 1 < len(sorted_entries)
            else None
        )
        result.append(
            replace(entry, end_millis=next_start)
            if next_start is not None and next_start >= entry.start_millis
            else entry
        )
    return result


def stable_id(prefix: str, *parts: Any) -> str:
    # 64-bit FNV-1a, identical to the Android implementation's overflow.
    value = "\x1f".join("" if part is None else str(part) for part in parts)
    hash_value = 0xCBF29CE484222325
    for character in value:
        hash_value ^= ord(character)
        hash_value = (hash_value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{prefix}:{hash_value:x}"


def _json_object(payload: bytes, source: str) -> Mapping[str, Any]:
    try:
        root = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exception:
        raise GuideParseException(f"The {source} response is invalid JSON.") from exception
    if not isinstance(root, Mapping):
        raise GuideParseException(f"The {source} response is not a JSON object.")
    return root


def _json_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def parse_centrum_stations(payload: bytes) -> list[GuideStation]:
    try:
        stations = parse_centrum_catalog(payload)
    except GuideCatalogParseError as exception:
        raise GuideParseException(str(exception)) from exception
    return [
        GuideStation(station.legacy_id, station.display_name, GuideMedium.TELEVISION)
        for station in stations
    ]


def parse_centrum_program(payload: bytes, channel_id: str) -> list[GuideProgramEntry]:
    root = _json_object(payload, "Centrum guide")
    values = root.get(channel_id)
    if not isinstance(values, list):
        return []
    result: list[GuideProgramEntry] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        title = clean_text(_json_text(value.get("title")))
        start = parse_iso_millis(_json_text(value.get("start")))
        if not title or start is None:
            continue
        end = parse_iso_millis(_json_text(value.get("stop")))
        source_id = _json_text(value.get("id"))
        result.append(
            GuideProgramEntry(
                id=(
                    f"centrum:{channel_id}:{source_id}"
                    if source_id
                    else stable_id(f"centrum:{channel_id}", start, title)
                ),
                start_millis=start,
                end_millis=end if end is not None and end >= start else None,
                title=title,
                description=clean_text(_json_text(value.get("description"))),
            )
        )
    return infer_missing_ends(result)


def parse_rozhlas_stations(payload: bytes) -> list[GuideStation]:
    try:
        stations = parse_rozhlas_catalog(payload)
    except GuideCatalogParseError as exception:
        raise GuideParseException(str(exception)) from exception
    return [
        GuideStation(station.legacy_id, station.display_name, GuideMedium.RADIO)
        for station in stations
    ]


def parse_rozhlas_program(payload: bytes, station_id: str) -> list[GuideProgramEntry]:
    root = _json_object(payload, "Czech Radio")
    values = root.get("data")
    if not isinstance(values, list):
        raise GuideParseException("The Czech Radio schedule has no data array.")
    result: list[GuideProgramEntry] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        title = clean_text(_json_text(value.get("title")))
        start = parse_iso_millis(_json_text(value.get("since")))
        if not title or start is None:
            continue
        end = parse_iso_millis(_json_text(value.get("till")))
        edition = value.get("edition")
        edition = edition if isinstance(edition, Mapping) else {}
        numeric_id = _json_int(value.get("id"))
        source_id = str(numeric_id) if numeric_id is not None else _json_text(value.get("id"))
        result.append(
            GuideProgramEntry(
                id=(
                    f"rozhlas:{station_id}:{source_id}:{start}"
                    if source_id is not None
                    else stable_id(f"rozhlas:{station_id}", start, title)
                ),
                start_millis=start,
                end_millis=end if end is not None and end >= start else None,
                title=title,
                description=clean_text(_json_text(value.get("description"))),
                program_url=https_url(_json_text(edition.get("profile"))),
                archive_url=https_url(_json_text(edition.get("archive"))),
            )
        )
    return infer_missing_ends(result)


def _secure_xml_root(payload: bytes, source: str) -> ET.Element:
    if _UNSAFE_XML.search(payload):
        raise GuideParseException(f"The {source} XML contains forbidden declarations.")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exception:
        raise GuideParseException(f"The {source} XML is invalid.") from exception


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_descendant(element: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in element.iter() if _local_name(node.tag) == name), None)


def _first_text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    found = _first_descendant(element, name)
    return "".join(found.itertext()) if found is not None else None


def parse_ct_program(
    payload: bytes, fallback_date: GuideDate, channel: str
) -> list[GuideProgramEntry]:
    root = _secure_xml_root(payload, "Czech Television schedule")
    error = next(
        (
            clean_text("".join(node.itertext()))
            for node in root.iter()
            if _local_name(node.tag) == "error"
        ),
        "",
    )
    if error:
        raise GuideParseException(f"Czech Television: {error}")
    result: list[GuideProgramEntry] = []
    for show in (node for node in root.iter() if _local_name(node.tag) == "porad"):
        names = _first_descendant(show, "nazvy")
        main_title = clean_text(_first_text(names, "nazev"))
        if not main_title:
            continue
        episode = clean_text(_first_text(names, "nazev_casti"))
        part = clean_text(_first_text(show, "dil"))
        title = main_title
        if part and part not in main_title:
            title += f" ({part})"
        if episode and episode not in main_title:
            title += f" – {episode}"
        guide_date = parse_ct_date(_first_text(show, "datum"), fallback_date)
        clock = parse_clock(_first_text(show, "cas"))
        if clock is None:
            continue
        start = local_millis(guide_date, *clock)
        if start is None:
            continue
        duration = _parse_ct_duration(_first_text(show, "stopaz"))
        links = _first_descendant(show, "linky")
        source_id = clean_text(show.get("id") or _first_text(show, "id"))
        ad = (_first_text(show, "ad") or "").strip()
        result.append(
            GuideProgramEntry(
                id=(
                    f"ct:{channel}:{source_id}"
                    if source_id
                    else stable_id(f"ct:{channel}", start, title)
                ),
                start_millis=start,
                end_millis=start + duration if duration is not None else None,
                title=title,
                description=clean_text(_first_text(show, "noticka")),
                audio_description=ad == "1",
                audio_description_known=ad in {"0", "1"},
                program_url=https_url(_first_text(links, "program")),
                archive_url=https_url(_first_text(links, "ivysilani")),
            )
        )
    return infer_missing_ends(result)


def _parse_ct_duration(value: str | None) -> int | None:
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    if minutes < 0 or seconds not in range(60):
        return None
    return (minutes * 60 + seconds) * 1000


_SMS_PROGRAMS_START = re.compile(
    r"<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bporady\b[^'\"]*['\"][^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_SMS_PROGRAMS_END = re.compile(
    r"<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bpredchozi_porady\b",
    re.IGNORECASE | re.DOTALL,
)
_SMS_ANCHOR = re.compile(
    r"<a\b([^>]*\bclass\s*=\s*['\"][^'\"]*\bnazev\b[^'\"]*['\"][^>]*)>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_SMS_HREF = re.compile(r"\bhref\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_SMS_TIME = re.compile(
    r"<span\b[^>]*>\s*(\d{1,2})[.:](\d{2})\s*</span>",
    re.IGNORECASE | re.DOTALL,
)
_SMS_TITLE = re.compile(
    r"<div\b(?![^>]*\bclass\s*=\s*['\"][^'\"]*\bdetail\b)[^>]*>(.*?)<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bztr\b",
    re.IGNORECASE | re.DOTALL,
)
_SMS_DETAIL = re.compile(
    r"<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bdetail\b[^'\"]*['\"][^>]*>(.*?)<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bztr\b",
    re.IGNORECASE | re.DOTALL,
)
_SMS_NUMERIC_ID = re.compile(r"/(\d{6,})(?=[-/]|$)")


def parse_sms_program(
    payload: bytes, date: GuideDate, station_name: str
) -> list[GuideProgramEntry]:
    html = payload.decode("cp1250", errors="replace")
    start_match = _SMS_PROGRAMS_START.search(html)
    if start_match is None:
        return []
    program_html = html[start_match.end() :]
    end_match = _SMS_PROGRAMS_END.search(program_html)
    if end_match is not None:
        program_html = program_html[: end_match.start()]

    result: list[GuideProgramEntry] = []
    previous_minutes = -1
    day_offset = 0
    for match in _SMS_ANCHOR.finditer(program_html):
        before = program_html[max(0, match.start() - 1024) : match.start()]
        clock_matches = list(_SMS_TIME.finditer(before))
        if not clock_matches:
            continue
        hour, minute = int(clock_matches[-1].group(1)), int(clock_matches[-1].group(2))
        if hour not in range(24) or minute not in range(60):
            continue
        minutes = hour * 60 + minute
        if previous_minutes >= 0 and minutes < previous_minutes:
            day_offset += 1
        previous_minutes = minutes
        start = local_millis(date, hour, minute, day_offset=day_offset)
        if start is None:
            continue
        attributes, body = match.group(1), match.group(2)
        href_match = _SMS_HREF.search(attributes)
        program_url = https_url(
            href_match.group(2) if href_match else None,
            "https://m.tv.sms.cz/",
        )
        title_match = _SMS_TITLE.search(body)
        title = clean_html(title_match.group(1) if title_match else None)
        if not title:
            title = clean_html(body.split('class="detail"', 1)[0])
        if not title:
            continue
        detail_match = _SMS_DETAIL.search(body)
        description = clean_html(detail_match.group(1) if detail_match else None)
        numeric_matches = list(_SMS_NUMERIC_ID.finditer(program_url or ""))
        source_id = numeric_matches[-1].group(1) if numeric_matches else None
        result.append(
            GuideProgramEntry(
                id=(
                    f"sms:{source_id}"
                    if source_id
                    else stable_id("sms", station_name, start, title)
                ),
                start_millis=start,
                end_millis=None,
                title=title,
                description=description,
                program_url=program_url,
            )
        )
    return infer_missing_ends(result)


_CT_CHANNEL_ALIASES: dict[str, frozenset[str]] = {
    "ct1": frozenset({"Channel1", "programmeBlockChannel1"}),
    "ct2": frozenset({"Channel2", "programmeBlockChannel2"}),
    "ct24": frozenset({"Channel24", "programmeBlockChannel24"}),
    "ct4": frozenset({"Channel4", "programmeBlockChannel4"}),
    "ct5": frozenset({"ChannelD", "programmeBlockChannelD"}),
    "ct6": frozenset({"ChannelArt", "programmeBlockChannelArt"}),
}
_CT_TAG_START = re.compile(r"<(?:div|section)\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_CT_LIST_ITEM = re.compile(r"<li\b([^>]*)>(.*?)</li\s*>", re.IGNORECASE | re.DOTALL)
_CT_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_CT_HEADING5 = re.compile(r"<h5\b[^>]*>(.*?)</h5\s*>", re.IGNORECASE | re.DOTALL)
_CT_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.IGNORECASE | re.DOTALL)
_CT_SPAN = re.compile(r"<span\b([^>]*)>(.*?)</span\s*>", re.IGNORECASE | re.DOTALL)
_CT_COLON_DURATION = re.compile(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{2})\s*$")
_CT_TEXT_DURATION = re.compile(
    r"(?i)(?:(\d+)\s*(?:hod(?:in(?:a|y)?)?|h)\b)?\s*"
    r"(?:(\d+)\s*(?:minut(?:a|y)?|min)\b)?\s*"
    r"(?:(\d+)\s*(?:sekund(?:a|y)?|s)\b)?"
)


@dataclass(frozen=True, slots=True)
class _HtmlElement:
    attributes: str
    body: str
    start: int
    end: int


def parse_ct_web_program(
    payload: bytes, date: GuideDate
) -> dict[str, list[GuideProgramEntry]]:
    html = payload.decode("utf-8-sig", errors="replace")
    blocks: list[tuple[re.Match[str], str]] = []
    for match in _CT_TAG_START.finditer(html):
        classes = _class_tokens(match.group(1))
        if "programmeBlock" not in classes:
            continue
        channel = next(
            (
                channel
                for channel, aliases in _CT_CHANNEL_ALIASES.items()
                if aliases.intersection(classes)
            ),
            None,
        )
        if channel is not None:
            blocks.append((match, channel))

    raw_items: dict[str, list[str]] = {channel: [] for channel in _CT_CHANNEL_ALIASES}
    for index, (match, channel) in enumerate(blocks):
        block_end = blocks[index + 1][0].start() if index + 1 < len(blocks) else len(html)
        body = html[match.end() : block_end]
        for item in _CT_LIST_ITEM.finditer(body):
            if "programme" in _class_tokens(item.group(1)):
                raw_items[channel].append(item.group(2))
    return {
        channel: _parse_ct_web_channel(items, channel, date)
        for channel, items in raw_items.items()
    }


def _parse_ct_web_channel(
    items: Iterable[str], channel: str, date: GuideDate
) -> list[GuideProgramEntry]:
    entries: list[GuideProgramEntry] = []
    previous_minutes = -1
    day_offset = 0
    for item in items:
        time_element = _element_by_class(item, "span", "progTime")
        clock = parse_clock(clean_html(time_element.body if time_element else None))
        if clock is None:
            continue
        minutes = clock[0] * 60 + clock[1]
        if previous_minutes >= 0 and minutes < previous_minutes:
            day_offset += 1
        previous_minutes = minutes
        start = local_millis(date, *clock, day_offset=day_offset)
        if start is None:
            continue
        title_element = _element_by_class(item, "a", "progTitle")
        if title_element is None:
            continue
        episode_element = _element_by_class(title_element.body, "span", "dil")
        episode = clean_html(episode_element.body if episode_element else None)
        title_body = title_element.body
        if episode_element is not None:
            title_body = title_body[: episode_element.start] + title_body[episode_element.end :]
        base_title = clean_html(title_body)
        if not base_title:
            continue
        part_match = _CT_HEADING5.search(item)
        part = clean_html(part_match.group(1) if part_match else None)
        title = _combine_ct_title(base_title, episode, part)
        program_url = https_url(
            _html_attribute(title_element.attributes, "href"),
            "https://www.ceskatelevize.cz/",
        )
        duration_element = _element_by_class(item, "span", "stopaz")
        duration = _parse_ct_web_duration(
            clean_html(duration_element.body if duration_element else None)
        )
        audio_description = any(
            _html_attribute(marker.group(1), "title") == "Zvukový popis"
            and clean_html(marker.group(2)) == "AD"
            for marker in _CT_SPAN.finditer(item)
        )
        paragraph = _CT_PARAGRAPH.search(item)
        entries.append(
            GuideProgramEntry(
                id=stable_id(f"ct-web:{channel}", start, title, program_url),
                start_millis=start,
                end_millis=start + duration if duration is not None else None,
                title=title,
                description=clean_html(paragraph.group(1) if paragraph else None),
                audio_description=audio_description,
                audio_description_known=True,
                program_url=program_url,
            )
        )
    unique: list[GuideProgramEntry] = []
    known: set[tuple[int, str, str | None]] = set()
    for entry in entries:
        key = (entry.start_millis, entry.title, entry.program_url)
        if key not in known:
            known.add(key)
            unique.append(entry)
    return infer_missing_ends(unique)


def _combine_ct_title(base_title: str, episode: str, part: str) -> str:
    title = base_title
    if episode and episode.casefold() not in base_title.casefold():
        title += f" {episode}" if episode.startswith("(") else f" ({episode})"
    if (
        part
        and part.casefold() not in base_title.casefold()
        and part.casefold() not in episode.casefold()
    ):
        title += f" – {part}"
    return title


def _parse_ct_web_duration(value: str) -> int | None:
    match = _CT_COLON_DURATION.fullmatch(value)
    if match:
        hours = int(match.group(1) or 0)
        minutes, seconds = int(match.group(2)), int(match.group(3))
        if minutes in range(60) and seconds in range(60):
            return (hours * 3600 + minutes * 60 + seconds) * 1000
    match = _CT_TEXT_DURATION.search(value)
    if match is None or not match.group(0).strip():
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total * 1000 if total > 0 else None


def _element_by_class(html: str, tag: str, class_name: str) -> _HtmlElement | None:
    expression = _CT_ANCHOR if tag.casefold() == "a" else _CT_SPAN if tag.casefold() == "span" else None
    if expression is None:
        return None
    for match in expression.finditer(html):
        if class_name in _class_tokens(match.group(1)):
            return _HtmlElement(match.group(1), match.group(2), match.start(), match.end())
    return None


def _class_tokens(attributes: str) -> set[str]:
    return set((_html_attribute(attributes, "class") or "").split())


def _html_attribute(attributes: str, name: str) -> str | None:
    quoted = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*(['\"])(.*?)\1",
        attributes,
        re.IGNORECASE | re.DOTALL,
    )
    if quoted:
        return decode_html_entities(quoted.group(2)).strip()
    unquoted = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*([^\s\"'=<>`]+)",
        attributes,
        re.IGNORECASE | re.DOTALL,
    )
    return decode_html_entities(unquoted.group(1)).strip() if unquoted else None


@dataclass(frozen=True, slots=True)
class _CtCacheEntry:
    created_at: float
    entries: tuple[GuideProgramEntry, ...]


@dataclass(frozen=True, slots=True)
class _CtWebCacheEntry:
    created_at: float
    schedules: Mapping[str, tuple[GuideProgramEntry, ...]]


class GuideRepository:
    """Station catalogs and source-specific fallback policy."""

    _ct_lock = threading.RLock()
    _ct_program_cache: dict[str, _CtCacheEntry] = {}
    _ct_web_program_cache: dict[str, _CtWebCacheEntry] = {}
    _ct_last_attempt: float | None = None
    CT_CACHE_SECONDS = 60.0
    CT_WEB_CACHE_SECONDS = 10.0 * 60.0

    def __init__(
        self,
        data_source: GuideDataSource | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.data_source = data_source or GuideHttpClient()
        self.clock = clock

    @classmethod
    def clear_process_cache_for_tests(cls) -> None:
        with cls._ct_lock:
            cls._ct_program_cache.clear()
            cls._ct_web_program_cache.clear()
            cls._ct_last_attempt = None

    def fallback_stations(self, medium: GuideMedium) -> list[GuideStation]:
        if medium is GuideMedium.TELEVISION:
            return order_guide_stations(_fallback_stations(medium), medium)
        if medium is GuideMedium.RADIO:
            return order_guide_stations(_fallback_stations(medium), medium)
        raise ValueError(f"Unsupported medium: {medium}")

    def close(self) -> None:
        close = getattr(self.data_source, "close", None)
        if close is not None:
            close()

    def refresh_stations(self, medium: GuideMedium) -> list[GuideStation]:
        try:
            if medium is GuideMedium.TELEVISION:
                live = parse_centrum_stations(
                    self.data_source.get(CENTRUM_CHANNELS_URL, JSON_ACCEPT)
                )
            elif medium is GuideMedium.RADIO:
                live = parse_rozhlas_stations(
                    self.data_source.get(ROZHLAS_STATIONS_URL, JSON_ACCEPT)
                )
            else:
                raise ValueError(f"Unsupported medium: {medium}")
        except GuideException:
            live = []
        if not live:
            return self.fallback_stations(medium)
        canonical_live = [_canonical_live_station(station) for station in live]
        merged = {station.id: station for station in canonical_live}
        for station in self.fallback_stations(medium):
            merged.setdefault(station.id, station)
        return order_guide_stations(merged.values(), medium)

    def load_program(
        self, station: GuideStation, date: GuideDate
    ) -> list[GuideProgramEntry]:
        if station.medium is GuideMedium.TELEVISION:
            return self._load_television(station, date)
        if station.medium is GuideMedium.RADIO:
            return self._load_radio(station, date)
        raise ValueError(f"Unsupported medium: {station.medium}")

    def _load_television(
        self, station: GuideStation, date: GuideDate
    ) -> list[GuideProgramEntry]:
        contract_station = _contract_station(station.id)
        providers = station.providers or (
            contract_station.providers if contract_station is not None else {}
        )
        if providers:
            return self._load_contract_television(providers, date)
        if station.id.startswith("centrum:"):
            channel = station.id.removeprefix("centrum:")
            if not channel.isdigit():
                raise ValueError(f"Invalid Centrum station id: {station.id}")
            return self._load_centrum(channel, date)
        if station.id.startswith("sms:"):
            sms_name = station.id.removeprefix("sms:").strip()
            if not sms_name:
                raise ValueError(f"Invalid SMS station id: {station.id}")
            return self._load_sms(sms_name, date)
        raise ValueError(f"Unsupported television station id: {station.id}")

    def _load_contract_television(
        self,
        providers: Mapping[str, Mapping[str, str]],
        date: GuideDate,
    ) -> list[GuideProgramEntry]:
        last_failure: GuideException | None = None
        ct = providers.get("ct")
        if ct is not None:
            channel = ct["channel"]
            for loader in (self._load_ct_xml, self._load_ct_web):
                try:
                    entries = loader(channel, date)
                    if entries:
                        return entries
                except GuideException as exception:
                    last_failure = exception
        centrum = providers.get("centrum")
        if centrum is not None:
            try:
                entries = self._load_centrum(centrum["id"], date)
                if entries:
                    return entries
            except GuideException as exception:
                last_failure = exception
        sms = providers.get("sms")
        if sms is not None:
            try:
                return self._load_sms(sms["name"], date)
            except GuideException as exception:
                last_failure = exception
        if last_failure is not None:
            raise last_failure
        return []

    def _load_radio(
        self, station: GuideStation, date: GuideDate
    ) -> list[GuideProgramEntry]:
        contract_station = _contract_station(station.id)
        providers = station.providers or (
            contract_station.providers if contract_station is not None else {}
        )
        if providers:
            return self._load_contract_radio(providers, date)
        if station.id.startswith("sms:"):
            sms_name = station.id.removeprefix("sms:").strip()
            if not sms_name:
                raise ValueError(f"Invalid SMS station id: {station.id}")
            return self._load_sms(sms_name, date)
        station_id = station.id.removeprefix("rozhlas:")
        if not station.id.startswith("rozhlas:") or not re.fullmatch(
            r"[a-z0-9-]+", station_id
        ):
            raise ValueError(f"Unsupported radio station id: {station.id}")
        primary_failure: GuideException | None = None
        try:
            official = parse_rozhlas_program(
                self.data_source.get(self._rozhlas_url(station_id, date), JSON_ACCEPT),
                station_id,
            )
            if official:
                return official
        except GuideException as exception:
            primary_failure = exception
        sms_name = station.name
        try:
            return self._load_sms(sms_name, date)
        except GuideException as fallback_failure:
            if primary_failure is not None:
                raise fallback_failure from primary_failure
            raise

    def _load_contract_radio(
        self,
        providers: Mapping[str, Mapping[str, str]],
        date: GuideDate,
    ) -> list[GuideProgramEntry]:
        primary_failure: GuideException | None = None
        rozhlas = providers.get("rozhlas")
        if rozhlas is not None:
            station_id = rozhlas["id"]
            try:
                official = parse_rozhlas_program(
                    self.data_source.get(
                        self._rozhlas_url(station_id, date),
                        JSON_ACCEPT,
                    ),
                    station_id,
                )
                if official:
                    return official
            except GuideException as exception:
                primary_failure = exception
        sms = providers.get("sms")
        if sms is not None:
            try:
                return self._load_sms(sms["name"], date)
            except GuideException as fallback_failure:
                if primary_failure is not None:
                    raise fallback_failure from primary_failure
                raise
        if primary_failure is not None:
            raise primary_failure
        return []

    def _load_ct_web(self, channel: str, date: GuideDate) -> list[GuideProgramEntry]:
        url = f"{CT_WEB_SCHEDULE_BASE}/{date.ct()}/"
        now = self.clock()
        cls = type(self)
        with cls._ct_lock:
            cls._ct_web_program_cache = {
                key: cached
                for key, cached in cls._ct_web_program_cache.items()
                if now - cached.created_at < cls.CT_WEB_CACHE_SECONDS
            }
            cached = cls._ct_web_program_cache.get(url)
            if cached is None:
                parsed = parse_ct_web_program(
                    self.data_source.get(url, HTML_ACCEPT), date
                )
                if not any(parsed.values()):
                    raise GuideParseException(
                        "The public Czech Television schedule contains no programmes."
                    )
                cached = _CtWebCacheEntry(
                    now,
                    {key: tuple(entries) for key, entries in parsed.items()},
                )
                cls._ct_web_program_cache[url] = cached
            return list(cached.schedules.get(channel, ()))

    def _load_ct_xml(self, channel: str, date: GuideDate) -> list[GuideProgramEntry]:
        url = (
            f"{CT_SCHEDULE_URL}?user=test&date={date.ct()}&channel={channel}"
        )
        now = self.clock()
        cls = type(self)
        with cls._ct_lock:
            cls._ct_program_cache = {
                key: cached
                for key, cached in cls._ct_program_cache.items()
                if now - cached.created_at < cls.CT_CACHE_SECONDS
            }
            cached = cls._ct_program_cache.get(url)
            if cached is not None:
                return list(cached.entries)
            if (
                cls._ct_last_attempt is not None
                and now - cls._ct_last_attempt < cls.CT_CACHE_SECONDS
            ):
                raise GuideNetworkException(
                    "Czech Television can be queried only once per minute; using the fallback."
                )
            cls._ct_last_attempt = now
            parsed = parse_ct_program(
                self.data_source.get(url, XML_ACCEPT), date, channel
            )
            if parsed:
                cls._ct_program_cache[url] = _CtCacheEntry(now, tuple(parsed))
            return parsed

    def _load_centrum(
        self, channel: str, date: GuideDate
    ) -> list[GuideProgramEntry]:
        url = f"{CENTRUM_SCHEDULE_BASE}/{date.iso()}?channels%5B%5D={channel}"
        return parse_centrum_program(
            self.data_source.get(url, JSON_ACCEPT), channel
        )

    def _load_sms(self, station_name: str, date: GuideDate) -> list[GuideProgramEntry]:
        encoded = quote_plus(station_name, encoding="cp1250", errors="strict")
        url = f"{SMS_SCHEDULE_URL}?cas=0&den={date.iso()}&stanice={encoded}"
        return parse_sms_program(
            self.data_source.get(url, HTML_ACCEPT), date, station_name
        )

    @staticmethod
    def _rozhlas_url(station_id: str, date: GuideDate) -> str:
        return (
            f"{ROZHLAS_SCHEDULE_BASE}/{date.year:04d}/{date.month:02d}/"
            f"{date.day:02d}/{station_id}.json"
        )


__all__ = [
    "GuideDataSource",
    "GuideDate",
    "GuideException",
    "GuideHttpClient",
    "GuideHttpException",
    "GuideMedium",
    "GuideNetworkException",
    "GuideParseException",
    "GuideProgramEntry",
    "GuideRepository",
    "GuideStation",
    "GuideTime",
    "canonical_station_id",
    "clean_html",
    "clean_text",
    "guide_date_at",
    "guide_contract",
    "https_url",
    "infer_missing_ends",
    "local_millis",
    "order_guide_stations",
    "parse_centrum_program",
    "parse_centrum_stations",
    "parse_clock",
    "parse_ct_program",
    "parse_ct_web_program",
    "parse_iso_millis",
    "parse_rozhlas_program",
    "parse_rozhlas_stations",
    "station_has_provider",
    "station_matches_search",
    "parse_sms_program",
    "stable_id",
]
