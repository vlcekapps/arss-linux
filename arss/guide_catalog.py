"""Dependency-free parsers for live guide station catalogues.

Keeping these parsers separate from HTTP, time-zone and GTK code lets the
cross-platform golden contract exercise the exact production parser offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import Any


_WHITESPACE = re.compile(r"\s+")


class GuideCatalogParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderStation:
    legacy_id: str
    display_name: str
    medium: str


def _json_object(payload: bytes, source: str) -> Mapping[str, Any]:
    try:
        root = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exception:
        raise GuideCatalogParseError(
            f"The {source} response is invalid JSON."
        ) from exception
    if not isinstance(root, Mapping):
        raise GuideCatalogParseError(
            f"The {source} response is not a JSON object."
        )
    return root


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _WHITESPACE.sub(" ", value.replace("\u00a0", " ")).strip()
    return cleaned or None


def parse_centrum_catalog(payload: bytes) -> list[ProviderStation]:
    root = _json_object(payload, "Centrum guide")
    result: list[ProviderStation] = []
    known: set[str] = set()
    for value in root.values():
        if not isinstance(value, Mapping):
            continue
        station_id = _text(value.get("id"))
        name = _text(value.get("name"))
        if station_id is None or name is None:
            continue
        legacy_id = f"centrum:{station_id}"
        if legacy_id not in known:
            known.add(legacy_id)
            result.append(ProviderStation(legacy_id, name, "television"))
    return result


def parse_rozhlas_catalog(payload: bytes) -> list[ProviderStation]:
    root = _json_object(payload, "Czech Radio")
    values = root.get("data")
    if not isinstance(values, list):
        raise GuideCatalogParseError(
            "The Czech Radio station catalog has no data array."
        )
    result: list[ProviderStation] = []
    known: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        station_id = _text(value.get("id"))
        name = _text(value.get("name"))
        if station_id is None or name is None:
            continue
        legacy_id = f"rozhlas:{station_id}"
        if legacy_id not in known:
            known.add(legacy_id)
            result.append(ProviderStation(legacy_id, name, "radio"))
    return result


__all__ = [
    "GuideCatalogParseError",
    "ProviderStation",
    "parse_centrum_catalog",
    "parse_rozhlas_catalog",
]
