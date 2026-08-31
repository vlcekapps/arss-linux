from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contractlib import (  # noqa: E402
    ContractValidationError,
    load_json,
    validate_contract,
    validate_manifest,
    write_manifest,
)
from live_drift import compare_provider, parse_centrum, parse_rozhlas  # noqa: E402


class ContractTest(unittest.TestCase):
    def test_rss_directory_retains_its_cc0_source_notice(self) -> None:
        notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Awesome RSS Feeds", notice)
        self.assertIn("3a7a9e28943d28b8acb6d9197fb168a8be5267f6", notice)
        self.assertIn("CC0 1.0 Universal", notice)

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_json(ROOT / "catalogs" / "guide_stations.json")
        cls.stations = cls.catalog["stations"]

    def test_complete_contract_validates(self) -> None:
        stats = validate_contract(ROOT)
        self.assertEqual("1.0.1", stats["contractVersion"])
        self.assertEqual(192, stats["stations"])
        self.assertEqual(128, stats["televisionStations"])
        self.assertEqual(64, stats["radioStations"])
        self.assertEqual(10, stats["defaultFeeds"])
        self.assertEqual(8, stats["guideSources"])

    def test_directory_contains_canonical_118_feeds(self) -> None:
        root = ET.parse(ROOT / "catalogs" / "rss_directory.opml").getroot()
        entries = [node for node in root.iter("outline") if node.attrib.get("xmlUrl")]
        self.assertEqual(118, len(entries))
        self.assertEqual(118, len({node.attrib["xmlUrl"] for node in entries}))

    def test_sort_order_is_contiguous_per_medium(self) -> None:
        for medium, expected_count in (("television", 128), ("radio", 64)):
            orders = [station["sortOrder"] for station in self.stations if station["medium"] == medium]
            self.assertEqual(list(range(expected_count)), orders)

    def test_nova_krimi_rebrand_preserves_centrum_559(self) -> None:
        station = next(item for item in self.stations if item["id"] == "tv.nova-krimi")
        self.assertEqual("Nova Krimi", station["displayName"])
        self.assertIn("Nova Gold", station["aliases"])
        self.assertEqual("559", station["providers"]["centrum"]["id"])

    def test_oneplay_multidimension_is_complete(self) -> None:
        ids = {station["id"] for station in self.stations}
        self.assertTrue({f"tv.oneplay-sport-md{number}" for number in range(1, 11)}.issubset(ids))

    def test_live_centrum_union_is_mapped(self) -> None:
        centrum_ids = {
            station["providers"]["centrum"]["id"]
            for station in self.stations
            if "centrum" in station["providers"]
        }
        self.assertTrue({"66", "91", "106", "119", "131", "137", "158", "323", "475"}.issubset(centrum_ids))

    def test_legacy_provider_ids_have_no_collisions(self) -> None:
        legacy: dict[str, str] = {}
        for station in self.stations:
            providers = station["providers"]
            values = []
            if "centrum" in providers:
                values.append(f"centrum:{providers['centrum']['id']}")
            if "rozhlas" in providers:
                values.append(f"rozhlas:{providers['rozhlas']['id']}")
            if "sms" in providers:
                values.append(f"sms:{providers['sms']['name']}")
            for value in values:
                self.assertNotIn(value, legacy, value)
                legacy[value] = station["id"]
        self.assertEqual("tv.ct1", legacy["centrum:1"])
        self.assertEqual("tv.nova-krimi", legacy["centrum:559"])
        self.assertEqual("radio.radiozurnal", legacy["rozhlas:radiozurnal"])
        self.assertEqual("tv.oneplay-sport-md10", legacy["sms:Oneplay Sport MD10"])

    def test_provider_fixtures_normalize_to_goldens(self) -> None:
        centrum = parse_centrum(load_json(ROOT / "fixtures" / "guide" / "centrum_channels.json"))
        rozhlas = parse_rozhlas(load_json(ROOT / "fixtures" / "guide" / "rozhlas_stations.json"))
        self.assertEqual({"1": "ČT1", "3": "Nova"}, centrum)
        self.assertEqual({"radiozurnal": "Radiožurnál", "dvojka": "Dvojka"}, rozhlas)
        centrum_result = compare_provider("centrum", centrum, self.stations)
        rozhlas_result = compare_provider("rozhlas", rozhlas, self.stations)
        self.assertEqual("ok", centrum_result["status"])
        self.assertEqual("ok", rozhlas_result["status"])
        self.assertFalse(centrum_result["unmapped"])
        self.assertFalse(rozhlas_result["unmapped"])

    def test_tn_atom_golden_regression(self) -> None:
        golden = load_json(ROOT / "golden" / "feeds" / "tn_nova_atom.json")
        self.assertEqual("Zpravodajství", golden["title"])
        self.assertEqual(2, len(golden["items"]))
        self.assertEqual("Testovací článek & zprávy", golden["items"][0]["title"])
        self.assertTrue(all(item["url"].startswith("https://tn.nova.cz/") for item in golden["items"]))
        self.assertTrue(all(item["mediaUrl"] is None for item in golden["items"]))

    def test_audio_description_golden_is_explicit(self) -> None:
        golden = load_json(ROOT / "golden" / "guide" / "ct_program.json")
        self.assertTrue(golden["programs"][0]["audioDescription"])
        self.assertTrue(golden["programs"][0]["audioDescriptionKnown"])
        self.assertFalse(golden["programs"][1]["audioDescription"])
        self.assertTrue(golden["programs"][1]["audioDescriptionKnown"])

    def test_manifest_detects_tampering_and_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            write_manifest(root)
            validate_manifest(root)
            (root / "one.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaises(ContractValidationError):
                validate_manifest(root)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            with self.assertRaises(ContractValidationError):
                validate_manifest(root)

    def test_manifest_hashes_are_lowercase_sha256(self) -> None:
        for line in (ROOT / "manifest.sha256").read_text(encoding="ascii").splitlines():
            digest, relative = line.split("  ", 1)
            self.assertEqual(64, len(digest))
            self.assertEqual(digest, digest.lower())
            self.assertEqual(digest, hashlib.sha256((ROOT / relative).read_bytes()).hexdigest())

    def test_contract_has_no_private_or_audio_material(self) -> None:
        forbidden = {".wav", ".mp3", ".m4a", ".jks", ".keystore", ".apk", ".aab", ".p12", ".pfx", ".key"}
        paths = [path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]
        self.assertFalse([path for path in paths if path.suffix.casefold() in forbidden])
        joined = "\n".join(path.relative_to(ROOT).as_posix() for path in paths)
        self.assertNotIn("C:/Users/", joined)


if __name__ == "__main__":
    unittest.main()
