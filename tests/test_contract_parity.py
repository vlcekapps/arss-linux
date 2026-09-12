from __future__ import annotations

import json
from pathlib import Path
import unittest

from arss.contract import DEFAULT_CONTRACT_DIRECTORY, load_embedded_contract
from arss.directory import DEFAULT_DIRECTORY_PATH
from arss.feed import parse_feed
from arss.guide import (
    CENTRUM_CHANNELS_URL,
    CENTRUM_SCHEDULE_BASE,
    CT_SCHEDULE_URL,
    CT_WEB_SCHEDULE_BASE,
    GuideDate,
    ROZHLAS_SCHEDULE_BASE,
    ROZHLAS_STATIONS_URL,
    SMS_SCHEDULE_URL,
    parse_centrum_program,
    parse_ct_program,
    parse_ct_web_program,
    parse_rozhlas_program,
    parse_sms_program,
)
from arss.guide_catalog import parse_centrum_catalog, parse_rozhlas_catalog
from arss.storage import CZECH_INITIAL_FEEDS, ENGLISH_INITIAL_FEEDS


CONTRACT = DEFAULT_CONTRACT_DIRECTORY


def payload(relative: str) -> bytes:
    return CONTRACT.joinpath(*relative.split("/")).read_bytes()


def json_value(relative: str) -> dict[str, object]:
    value = json.loads(payload(relative))
    assert isinstance(value, dict)
    return value


def normalized_programs(
    entries: object,
    *,
    source: str,
    station_id: str,
    date: str,
) -> dict[str, object]:
    return {
        "$schema": "../../schemas/normalized-guide-program.schema.json",
        "schemaVersion": "1.0.0",
        "source": source,
        "stationId": station_id,
        "date": date,
        "programs": [
            {
                "id": entry.id,
                "startMillis": entry.start_millis,
                "endMillis": entry.end_millis,
                "title": entry.title,
                "description": entry.description,
                "audioDescription": entry.audio_description,
                "audioDescriptionKnown": entry.audio_description_known,
                "programUrl": entry.program_url,
                "archiveUrl": entry.archive_url,
            }
            for entry in entries
        ],
    }


class EmbeddedContractParityTest(unittest.TestCase):
    def test_linux_parity_suite_enumerates_every_contract_golden(self) -> None:
        actual = {
            path.relative_to(CONTRACT).as_posix()
            for path in (CONTRACT / "golden").rglob("*.json")
        }
        self.assertEqual(
            {
                "golden/feeds/atom1.json",
                "golden/feeds/rss2.json",
                "golden/feeds/tn_nova_atom.json",
                "golden/guide/centrum_channels.json",
                "golden/guide/centrum_program.json",
                "golden/guide/ct_program.json",
                "golden/guide/ct_web_program_ct1.json",
                "golden/guide/rozhlas_program.json",
                "golden/guide/rozhlas_stations.json",
                "golden/guide/sms_program.json",
                "golden/guide/stations.normalized.json",
            },
            actual,
        )

    def test_embedded_manifest_and_station_golden_are_exact(self) -> None:
        bundle = load_embedded_contract()
        actual = {
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
        self.assertEqual(
            json_value("golden/guide/stations.normalized.json"),
            actual,
        )

    def test_runtime_catalogues_and_source_priority_come_from_contract(self) -> None:
        bundle = load_embedded_contract()
        sources = bundle.guide_source_by_id
        self.assertEqual(sources["centrum.channels"].base_url, CENTRUM_CHANNELS_URL)
        self.assertEqual(sources["centrum.schedule"].base_url, CENTRUM_SCHEDULE_BASE)
        self.assertEqual(sources["ct.schedule"].base_url, CT_SCHEDULE_URL)
        self.assertEqual(sources["ct.web-schedule"].base_url, CT_WEB_SCHEDULE_BASE)
        self.assertEqual(sources["rozhlas.stations"].base_url, ROZHLAS_STATIONS_URL)
        self.assertEqual(sources["rozhlas.schedule"].base_url, ROZHLAS_SCHEDULE_BASE)
        self.assertEqual(
            sources["sms.television-schedule"].base_url,
            SMS_SCHEDULE_URL,
        )
        self.assertEqual(
            [10, 15, 20, 90],
            [
                sources[source_id].priority
                for source_id in (
                    "ct.schedule",
                    "ct.web-schedule",
                    "centrum.schedule",
                    "sms.television-schedule",
                )
            ],
        )
        self.assertEqual(bundle.rss_directory_path, DEFAULT_DIRECTORY_PATH)
        self.assertEqual(
            tuple((feed.title, feed.url) for feed in bundle.default_feeds_by_locale["cs"]),
            tuple((feed.title, feed.url) for feed in CZECH_INITIAL_FEEDS),
        )
        self.assertEqual(
            tuple((feed.title, feed.url) for feed in bundle.default_feeds_by_locale["en"]),
            tuple((feed.title, feed.url) for feed in ENGLISH_INITIAL_FEEDS),
        )
        for station in bundle.stations:
            for legacy_id in station.legacy_ids:
                with self.subTest(legacy_id=legacy_id):
                    self.assertEqual(station.id, bundle.resolve_station_id(legacy_id))

    def test_all_shared_station_catalogue_fixtures_match_exact_goldens(self) -> None:
        bundle = load_embedded_contract()
        cases = (
            (
                "centrum_channels",
                "television",
                parse_centrum_catalog(payload("fixtures/guide/centrum_channels.json")),
            ),
            (
                "rozhlas_stations",
                "radio",
                parse_rozhlas_catalog(payload("fixtures/guide/rozhlas_stations.json")),
            ),
        )
        for name, medium, parsed in cases:
            with self.subTest(name=name):
                actual = {
                    "$schema": "../../schemas/normalized-guide.schema.json",
                    "schemaVersion": "1.0.0",
                    "stations": [
                        {
                            "id": stable.id,
                            "medium": medium,
                            "displayName": station.display_name,
                            "sortOrder": stable.sort_order,
                        }
                        for station in parsed
                        for stable in (
                            bundle.station_by_id[
                                bundle.resolve_station_id(station.legacy_id)
                            ],
                        )
                    ],
                }
                self.assertEqual(json_value(f"golden/guide/{name}.json"), actual)

    def test_all_shared_feed_fixtures_match_exact_golden_output(self) -> None:
        for name in ("atom1", "rss2", "tn_nova_atom"):
            with self.subTest(name=name):
                golden = json_value(f"golden/feeds/{name}.json")
                source_url = golden["sourceUrl"]
                self.assertIsInstance(source_url, str)
                parsed = parse_feed(
                    payload(f"fixtures/feeds/{name}.xml"),
                    source_url,
                )
                actual = {
                    "$schema": "../../schemas/normalized-feed.schema.json",
                    "schemaVersion": "1.0.0",
                    "sourceUrl": source_url,
                    "title": parsed.title,
                    "items": [
                        {
                            "title": item.title,
                            "url": item.url,
                            "sourceId": item.source_id,
                            "publishedText": item.published_text,
                            "publishedAtMillis": item.published_at_millis,
                            "mediaUrl": item.media_url,
                            "mediaType": item.media_type,
                            "durationText": item.duration_text,
                        }
                        for item in parsed.articles
                    ],
                }
                self.assertEqual(golden, actual)

    def test_all_shared_program_fixtures_match_exact_golden_output(self) -> None:
        guide_date = GuideDate(2026, 7, 22)
        cases = (
            (
                "centrum_program",
                "centrum.schedule",
                "tv.nova",
                parse_centrum_program(
                    payload("fixtures/guide/centrum_program.json"), "3"
                ),
            ),
            (
                "rozhlas_program",
                "rozhlas.schedule",
                "radio.radiozurnal",
                parse_rozhlas_program(
                    payload("fixtures/guide/rozhlas_program.json"),
                    "radiozurnal",
                ),
            ),
            (
                "ct_program",
                "ct.schedule",
                "tv.ct1",
                parse_ct_program(
                    payload("fixtures/guide/ct_program.xml"),
                    guide_date,
                    "ct1",
                ),
            ),
            (
                "ct_web_program_ct1",
                "ct.web-schedule",
                "tv.ct1",
                parse_ct_web_program(
                    payload("fixtures/guide/ct_web_program.html"),
                    guide_date,
                )["ct1"],
            ),
            (
                "sms_program",
                "sms.television-schedule",
                "tv.nova-sport-3",
                parse_sms_program(
                    payload("fixtures/guide/sms_program.html"),
                    guide_date,
                    "Nova Sport 3",
                ),
            ),
        )
        for name, source, station_id, entries in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    json_value(f"golden/guide/{name}.json"),
                    normalized_programs(
                        entries,
                        source=source,
                        station_id=station_id,
                        date="2026-07-22",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
