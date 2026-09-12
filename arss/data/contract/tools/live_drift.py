#!/usr/bin/env python3
"""Read live guide discovery endpoints and report drift without changing data."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import socket
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from contractlib import ContractValidationError, load_json, validate_contract


MAXIMUM_BYTES = 4 * 1024 * 1024


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, file_pointer: Any, code: int, message: str, headers: Any, new_url: str) -> Request | None:
        if urlsplit(new_url).scheme != "https":
            raise URLError("HTTPS downgrade redirect rejected")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/json;q=0.9, */*;q=0.1",
            "User-Agent": "ARSS-Contract-Live-Drift/1.0",
        },
    )
    with build_opener(HTTPSRedirectHandler()).open(request, timeout=timeout) as response:
        final_url = response.geturl()
        if urlsplit(final_url).scheme != "https":
            raise URLError("Non-HTTPS final response rejected")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAXIMUM_BYTES:
            raise ValueError("response exceeds size limit")
        payload = response.read(MAXIMUM_BYTES + 1)
        if len(payload) > MAXIMUM_BYTES:
            raise ValueError("response exceeds size limit")
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def parse_centrum(value: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.values():
        if not isinstance(item, dict):
            continue
        station_id = item.get("id")
        name = item.get("name")
        if isinstance(station_id, (str, int)) and not isinstance(station_id, bool) and isinstance(name, str):
            station_id = str(station_id).strip()
            name = " ".join(name.split())
            if station_id and name:
                result.setdefault(station_id, name)
    return result


def parse_rozhlas(value: dict[str, Any]) -> dict[str, str]:
    data = value.get("data")
    if not isinstance(data, list):
        raise ValueError("Czech Radio response has no data array")
    result: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        station_id = item.get("id")
        name = item.get("name")
        if isinstance(station_id, str) and isinstance(name, str):
            station_id = station_id.strip()
            name = " ".join(name.split())
            if station_id and name:
                result.setdefault(station_id, name)
    return result


def folded(value: str) -> str:
    import re
    import unicodedata

    value = "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def compare_provider(provider: str, live: dict[str, str], stations: list[dict[str, Any]]) -> dict[str, Any]:
    known: dict[str, dict[str, Any]] = {
        station["providers"][provider]["id"]: station
        for station in stations
        if provider in station["providers"]
    }
    unmapped = [
        {"providerId": station_id, "liveName": live[station_id]}
        for station_id in sorted(set(live) - set(known))
    ]
    possibly_removed = [
        {"providerId": station_id, "stationId": known[station_id]["id"], "displayName": known[station_id]["displayName"]}
        for station_id in sorted(set(known) - set(live))
    ]
    possibly_renamed: list[dict[str, str]] = []
    for provider_id in sorted(set(live) & set(known)):
        station = known[provider_id]
        accepted = {folded(station["displayName"]), *(folded(alias) for alias in station["aliases"])}
        if folded(live[provider_id]) not in accepted:
            possibly_renamed.append({
                "providerId": provider_id,
                "stationId": station["id"],
                "catalogName": station["displayName"],
                "liveName": live[provider_id],
            })
    return {
        "status": "ok",
        "liveCount": len(live),
        "mappedCount": len(known),
        "unmapped": unmapped,
        "possiblyRemoved": possibly_removed,
        "possiblyRenamed": possibly_renamed,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ARSS live guide drift report",
        "",
        f"Checked: `{report['checkedAt']}`  ",
        f"Contract: `{report['contractVersion']}`  ",
        f"Drift observed: `{'yes' if report['hasDrift'] else 'no'}`  ",
        f"Provider unavailable: `{'yes' if report['hasUnavailable'] else 'no'}`",
        "",
        "This report is read-only. Every change requires human review; nothing was committed or merged.",
        "",
    ]
    for result in report["results"]:
        lines.extend((f"## {result['sourceId']}", ""))
        if result["status"] != "ok":
            lines.extend((f"Status: **unavailable** — {result['error']}", ""))
            continue
        lines.extend((
            f"Live: {result['liveCount']}; mapped: {result['mappedCount']}; "
            f"unmapped: {len(result['unmapped'])}; possibly removed: {len(result['possiblyRemoved'])}; "
            f"possibly renamed: {len(result['possiblyRenamed'])}.",
            "",
        ))
        for heading, key in (("Unmapped", "unmapped"), ("Possibly removed", "possiblyRemoved"), ("Possibly renamed", "possiblyRenamed")):
            if result[key]:
                lines.extend((f"### {heading}", "", "```json", json.dumps(result[key], ensure_ascii=False, indent=2), "```", ""))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path, default=Path("live-drift-report.md"))
    parser.add_argument("--json", dest="json_path", type=Path, default=Path("live-drift-report.json"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    try:
        stats = validate_contract(args.root)
        catalog = load_json(args.root / "catalogs" / "guide_stations.json")
        sources = load_json(args.root / "catalogs" / "guide_sources.json")["sources"]
    except (ContractValidationError, OSError) as error:
        print(f"Contract validation failed before live check: {error}", file=sys.stderr)
        return 1

    discovery = {source["id"]: source for source in sources if source["role"] == "discovery" and source["enabled"]}
    checks = (
        ("centrum.channels", "centrum", parse_centrum),
        ("rozhlas.stations", "rozhlas", parse_rozhlas),
    )
    results: list[dict[str, Any]] = []
    for source_id, provider, parse in checks:
        source = discovery.get(source_id)
        if source is None:
            results.append({"sourceId": source_id, "status": "unavailable", "error": "source is not enabled in guide_sources.json"})
            continue
        try:
            live = parse(fetch_json(source["baseUrl"], args.timeout))
            result = compare_provider(provider, live, catalog["stations"])
            result["sourceId"] = source_id
            results.append(result)
        except (HTTPError, URLError, TimeoutError, socket.timeout, UnicodeError, ValueError, json.JSONDecodeError) as error:
            results.append({"sourceId": source_id, "status": "unavailable", "error": f"{type(error).__name__}: {error}"})

    has_drift = any(
        result["status"] == "ok" and any(result[key] for key in ("unmapped", "possiblyRemoved", "possiblyRenamed"))
        for result in results
    )
    has_unavailable = any(result["status"] != "ok" for result in results)
    report = {
        "schemaVersion": "1.0.0",
        "checkedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "contractVersion": stats["contractVersion"],
        "hasDrift": has_drift,
        "hasUnavailable": has_unavailable,
        "results": results,
    }
    args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    args.report.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"has_drift={str(has_drift).lower()}\n")
            output.write(f"has_unavailable={str(has_unavailable).lower()}\n")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
