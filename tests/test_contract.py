from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from arss.contract import ContractError, load_contract


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class ContractFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, bytes] = {
            "LICENSE": b"Synthetic MIT fixture license.\n",
            "THIRD_PARTY_NOTICES.md": b"Synthetic CC0 fixture notice.\n",
            "version.json": _json_bytes(
                {
                    "$schema": "./schemas/version.schema.json",
                    "contractVersion": "1.0.0",
                    "releasedAt": "2026-08-31",
                    "reference": {
                        "repository": "https://github.com/vlcekapps/arss-linux",
                        "tag": "v1.6.12",
                        "commit": "16245c06ca7659712d2b4788b45ee71c5c8cc9c2",
                    },
                    "minimumConsumerVersion": "1.0.0",
                }
            ),
            "catalogs/guide_stations.json": _json_bytes(
                {
                    "$schema": "../schemas/guide-stations.schema.json",
                    "schemaVersion": "1.0.0",
                    "stations": [
                        {
                            "id": "tv.ct.ct1",
                            "medium": "television",
                            "displayName": "ČT1",
                            "sortOrder": 0,
                            "family": "ct",
                            "aliases": ["ČT 1"],
                            "providers": {
                                "centrum": {"id": "1"},
                                "ct": {"channel": "ct1"},
                                "sms": {"name": "ČT1"},
                            },
                        },
                        {
                            "id": "radio.cro.radiozurnal",
                            "medium": "radio",
                            "displayName": "Radiožurnál",
                            "sortOrder": 0,
                            "family": "cro",
                            "aliases": ["ČRo Radiožurnál"],
                            "providers": {
                                "rozhlas": {"id": "radiozurnal"},
                                "sms": {"name": "ČRo Radiožurnál"},
                            },
                        },
                    ],
                }
            ),
            "catalogs/guide_sources.json": _json_bytes(
                {
                    "$schema": "../schemas/guide-sources.schema.json",
                    "schemaVersion": "1.0.0",
                    "sources": [
                        {
                            "id": "test.tv-discovery",
                            "medium": "television",
                            "role": "discovery",
                            "baseUrl": "https://example.test/tv/stations",
                            "format": "json",
                            "priority": 10,
                            "enabled": True,
                        },
                        {
                            "id": "test.tv-schedule",
                            "medium": "television",
                            "role": "schedule",
                            "baseUrl": "https://example.test/tv/program",
                            "format": "json",
                            "priority": 10,
                            "enabled": True,
                        },
                        {
                            "id": "test.radio-discovery",
                            "medium": "radio",
                            "role": "discovery",
                            "baseUrl": "https://example.test/radio/stations",
                            "format": "json",
                            "priority": 10,
                            "enabled": True,
                        },
                        {
                            "id": "test.radio-schedule",
                            "medium": "radio",
                            "role": "schedule",
                            "baseUrl": "https://example.test/radio/program",
                            "format": "json",
                            "priority": 10,
                            "enabled": True,
                        },
                    ],
                }
            ),
            "catalogs/default_feeds.json": _json_bytes(
                {
                    "$schema": "../schemas/default-feeds.schema.json",
                    "schemaVersion": "1.0.0",
                    "locales": {
                        "cs": [
                            {
                                "id": "test-cs",
                                "title": "Test CS",
                                "url": "https://example.test/cs.xml",
                                "format": "rss",
                            }
                        ],
                        "en": [
                            {
                                "id": "test-en",
                                "title": "Test EN",
                                "url": "https://example.test/en.xml",
                                "format": "atom",
                            }
                        ],
                    },
                }
            ),
            "catalogs/rss_directory.opml": (
                b'<?xml version="1.0"?><opml version="2.0"><body>'
                b'<outline type="rss" title="Test" '
                b'xmlUrl="https://example.test/feed.xml"/>'
                b'</body></opml>\n'
            ),
            "schemas/version.schema.json": _json_bytes({"type": "object"}),
            "schemas/default-feeds.schema.json": _json_bytes({"type": "object"}),
            "schemas/guide-sources.schema.json": _json_bytes({"type": "object"}),
            "schemas/guide-stations.schema.json": _json_bytes({"type": "object"}),
            "schemas/normalized-guide.schema.json": _json_bytes({"type": "object"}),
            "fixtures/guide/centrum_channels.json": _json_bytes(
                {
                    "1": {"id": "1", "name": "ČT1", "slug": "ct1"},
                }
            ),
            "fixtures/guide/rozhlas_stations.json": _json_bytes(
                {
                    "data": [
                        {"id": "radiozurnal", "name": "Radiožurnál"},
                    ]
                }
            ),
            "golden/guide/stations.normalized.json": _json_bytes(
                {
                    "$schema": "../../schemas/normalized-guide.schema.json",
                    "schemaVersion": "1.0.0",
                    "stations": [
                        {
                            "id": "tv.ct.ct1",
                            "medium": "television",
                            "displayName": "ČT1",
                            "sortOrder": 0,
                        },
                        {
                            "id": "radio.cro.radiozurnal",
                            "medium": "radio",
                            "displayName": "Radiožurnál",
                            "sortOrder": 0,
                        },
                    ],
                }
            ),
            "golden/guide/centrum_channels.json": _json_bytes(
                {
                    "$schema": "../../schemas/normalized-guide.schema.json",
                    "schemaVersion": "1.0.0",
                    "stations": [
                        {
                            "id": "tv.ct.ct1",
                            "medium": "television",
                            "displayName": "ČT1",
                            "sortOrder": 0,
                        }
                    ],
                }
            ),
            "golden/guide/rozhlas_stations.json": _json_bytes(
                {
                    "$schema": "../../schemas/normalized-guide.schema.json",
                    "schemaVersion": "1.0.0",
                    "stations": [
                        {
                            "id": "radio.cro.radiozurnal",
                            "medium": "radio",
                            "displayName": "Radiožurnál",
                            "sortOrder": 0,
                        }
                    ],
                }
            ),
        }

    def write(self) -> None:
        for relative, payload in self.files.items():
            destination = self.root.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        manifest = "".join(
            f"{hashlib.sha256(payload).hexdigest()}  {relative}\n"
            for relative, payload in sorted(self.files.items())
        ).encode("ascii")
        (self.root / "manifest.sha256").write_bytes(manifest)
        (self.root / "contract.lock.json").write_bytes(
            _json_bytes(
                {
                    "schemaVersion": "1.0.0",
                    "contractVersion": "1.0.0",
                    "sourceRepository": "https://github.com/vlcekapps/arss-contract",
                    "sourceTag": "v1.0.0",
                    "sourceCommit": "0123456789abcdef0123456789abcdef01234567",
                    "manifestSha256": hashlib.sha256(manifest).hexdigest(),
                }
            )
        )


class ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="arss-contract-test-")
        self.root = Path(self.temporary.name)
        self.fixture = ContractFixture(self.root)
        self.fixture.write()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loads_exact_normalized_golden_and_resolves_legacy_ids(self) -> None:
        bundle = load_contract(self.root)
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
        golden = json.loads(
            (self.root / "golden/guide/stations.normalized.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(golden, normalized)
        self.assertEqual("tv.ct.ct1", bundle.resolve_station_id("centrum:1"))
        self.assertEqual("tv.ct.ct1", bundle.resolve_station_id("sms:ČT1"))
        self.assertEqual(
            "radio.cro.radiozurnal",
            bundle.resolve_station_id("rozhlas:radiozurnal"),
        )
        self.assertEqual(
            "radio.cro.radiozurnal",
            bundle.resolve_station_id("sms:ČRo Radiožurnál"),
        )
        self.assertEqual("future:unknown", bundle.resolve_station_id("future:unknown"))
        self.assertEqual(
            "https://example.test/tv/program",
            bundle.guide_source_by_id["test.tv-schedule"].base_url,
        )
        self.assertEqual(
            ("https://example.test/cs.xml",),
            tuple(feed.url for feed in bundle.default_feeds_by_locale["cs"]),
        )
        self.assertEqual(
            self.root / "catalogs" / "rss_directory.opml",
            bundle.rss_directory_path,
        )

    def test_rejects_tampering_before_parsing(self) -> None:
        path = self.root / "catalogs/guide_stations.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ContractError, "checksum mismatch"):
            load_contract(self.root)

    def test_rejects_manifest_traversal(self) -> None:
        manifest = self.root / "manifest.sha256"
        manifest.write_text("0" * 64 + "  ../outside\n", encoding="ascii")
        lock = json.loads((self.root / "contract.lock.json").read_text(encoding="utf-8"))
        lock["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        (self.root / "contract.lock.json").write_bytes(_json_bytes(lock))
        with self.assertRaisesRegex(ContractError, "Unsafe manifest path"):
            load_contract(self.root)

    def test_rejects_paths_excluded_by_the_public_manifest_policy(self) -> None:
        manifest = self.root / "manifest.sha256"
        original_manifest = manifest.read_bytes()
        lock_path = self.root / "contract.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        excluded = (
            ".git/HEAD",
            "nested/__pycache__/module.py",
            "live-drift-report.json",
            "live-drift-report.md",
            "nested/module.pyc",
            "nested/module.pyo",
        )
        for relative in excluded:
            with self.subTest(relative=relative):
                payload = original_manifest + (
                    "0" * 64 + f"  {relative}\n"
                ).encode("ascii")
                manifest.write_bytes(payload)
                lock["manifestSha256"] = hashlib.sha256(payload).hexdigest()
                lock_path.write_bytes(_json_bytes(lock))
                with self.assertRaisesRegex(ContractError, "Unsafe manifest path"):
                    load_contract(self.root)

    def test_rejects_manifest_paths_that_collide_on_portable_filesystems(self) -> None:
        manifest = self.root / "manifest.sha256"
        manifest.write_bytes(
            manifest.read_bytes()
            + ("0" * 64 + "  VERSION.JSON\n").encode("ascii")
        )
        lock_path = self.root / "contract.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        lock_path.write_bytes(_json_bytes(lock))
        with self.assertRaisesRegex(ContractError, "collide on portable"):
            load_contract(self.root)

    def test_rejects_duplicate_provider_specific_ids(self) -> None:
        catalog = json.loads(self.fixture.files["catalogs/guide_stations.json"])
        duplicate = dict(catalog["stations"][0])
        duplicate["id"] = "tv.ct.duplicate"
        duplicate["sortOrder"] = 1
        catalog["stations"].insert(1, duplicate)
        self.fixture.files["catalogs/guide_stations.json"] = _json_bytes(catalog)
        self.fixture.write()
        with self.assertRaisesRegex(ContractError, "Duplicate provider-specific"):
            load_contract(self.root)

    def test_rejects_a_contract_requiring_a_newer_consumer(self) -> None:
        version = json.loads(self.fixture.files["version.json"])
        version["minimumConsumerVersion"] = "2.0.0"
        self.fixture.files["version.json"] = _json_bytes(version)
        self.fixture.write()
        with self.assertRaisesRegex(ContractError, "incompatible"):
            load_contract(self.root)

    def test_rejects_a_tag_that_does_not_name_the_locked_version(self) -> None:
        lock_path = self.root / "contract.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["sourceTag"] = "v1.0.1"
        lock_path.write_bytes(_json_bytes(lock))
        with self.assertRaisesRegex(ContractError, "locked contract version"):
            load_contract(self.root)


if __name__ == "__main__":
    unittest.main()
