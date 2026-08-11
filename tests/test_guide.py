from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import unittest
from urllib.parse import quote_plus

from arss.guide import (
    GuideDate,
    GuideHttpClient,
    GuideMedium,
    GuideNetworkException,
    GuideParseException,
    GuideRepository,
    GuideStation,
    GuideTime,
    RADIO_SMS_NAMES,
    TELEVISION_SMS_NAMES,
    order_guide_stations,
    parse_centrum_program,
    parse_centrum_stations,
    parse_ct_program,
    parse_ct_web_program,
    parse_rozhlas_program,
    parse_rozhlas_stations,
    parse_sms_program,
)


FIXTURES = Path(__file__).parent / "fixtures" / "guide"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def instant(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)

EXPECTED_TELEVISION_STATIONS = (
    ('centrum:1', 'ČT1'),
    ('centrum:2', 'ČT2'),
    ('centrum:24', 'ČT24'),
    ('centrum:18', 'ČT sport'),
    ('centrum:357', 'ČT :D'),
    ('centrum:358', 'ČT art'),
    ('centrum:3', 'Nova'),
    ('centrum:78', 'Nova Cinema'),
    ('centrum:558', 'Nova Action'),
    ('centrum:560', 'Nova Fun'),
    ('centrum:559', 'Nova Krimi'),
    ('sms:Nova Lady', 'Nova Lady'),
    ('centrum:17', 'Nova Sport 1'),
    ('centrum:465', 'Nova Sport 2'),
    ('sms:Nova Sport 3', 'Nova Sport 3'),
    ('sms:Nova Sport 4', 'Nova Sport 4'),
    ('sms:Nova Sport 5', 'Nova Sport 5'),
    ('sms:Nova Sport 6', 'Nova Sport 6'),
    ('centrum:4', 'Prima'),
    ('centrum:92', 'Prima Cool'),
    ('centrum:474', 'Prima MAX'),
    ('centrum:608', 'Prima Krimi'),
    ('centrum:226', 'Prima Love'),
    ('centrum:556', 'Prima PLUS'),
    ('sms:Prima Show', 'Prima Show'),
    ('sms:Prima sport', 'Prima Sport'),
    ('sms:Prima Star', 'Prima Star'),
    ('centrum:333', 'Prima ZOOM'),
    ('centrum:818', 'CNN Prima News'),
    ('centrum:89', 'Barrandov'),
    ('sms:Barrandov Krimi', 'Barrandov Krimi'),
    ('sms:Barrandov Kino', 'Kino Barrandov'),
    ('centrum:5', 'HBO'),
    ('centrum:6', 'HBO 2'),
    ('sms:HBO3', 'HBO 3'),
    ('centrum:7', 'Cinemax'),
    ('centrum:112', 'Cinemax 2'),
    ('centrum:11', 'Animal Planet'),
    ('centrum:12', 'Discovery Channel'),
    ('centrum:16', 'Eurosport'),
    ('centrum:25', 'Eurosport 2'),
    ('sms:Oneplay Sport 1', 'Oneplay Sport 1'),
    ('sms:Oneplay Sport 2', 'Oneplay Sport 2'),
    ('sms:Oneplay Sport 3', 'Oneplay Sport 3'),
    ('sms:Oneplay Sport 4', 'Oneplay Sport 4'),
    ('centrum:181', 'Jednotka'),
    ('centrum:180', 'Dvojka'),
    ('centrum:183', 'Markíza'),
    ('sms:Markíza International', 'Markíza International'),
    ('centrum:182', 'TV Doma'),
    ('centrum:185', 'JOJ'),
    ('centrum:184', 'JOJ+'),
    ('sms:JOJ Cinema', 'JOJ Cinema'),
    ('sms:JOJ Family', 'JOJ Family'),
    ('sms:Jojko', 'Jojko'),
    ('centrum:68', 'TA3'),
    ('centrum:394', 'AMC'),
    ('centrum:9', 'AXN'),
    ('centrum:369', 'AXN Black'),
    ('centrum:370', 'AXN White'),
    ('centrum:108', 'Baby TV'),
    ('centrum:97', 'BBC World News'),
    ('centrum:73', 'Boomerang'),
    ('centrum:110', 'C Music TV'),
    ('sms:Canal+ Sport', 'Canal+ Sport'),
    ('sms:Canal+ Sport 2', 'Canal+ Sport 2'),
    ('sms:Canal+ Sport 3', 'Canal+ Sport 3'),
    ('sms:Canal+ Sport 4', 'Canal+ Sport 4'),
    ('sms:Canal+ Sport 5', 'Canal+ Sport 5'),
    ('sms:Canal+ Sport 6', 'Canal+ Sport 6'),
    ('sms:Canal+ Sport 7', 'Canal+ Sport 7'),
    ('sms:Canal+ Sport 8', 'Canal+ Sport 8'),
    ('centrum:55', 'Cartoon+TCM'),
    ('centrum:152', 'CNBC Europe'),
    ('centrum:23', 'CNN'),
    ('centrum:15', 'CS Film'),
    ('sms:CS History', 'CS History'),
    ('sms:CS Horror', 'CS Horror'),
    ('centrum:31', 'Disney Channel'),
    ('centrum:117', 'Euronews'),
    ('centrum:85', 'Extreme Sports'),
    ('centrum:63', 'Film+'),
    ('centrum:64', 'Filmbox'),
    ('centrum:65', 'Filmbox Stars'),
    ('sms:France24', 'France 24'),
    ('centrum:125', 'History Channel'),
    ('centrum:72', 'Hustler'),
    ('centrum:77', 'JimJam'),
    ('centrum:127', 'Leo TV'),
    ('centrum:67', 'Mezzo'),
    ('centrum:20', 'MTV'),
    ('centrum:132', 'Music Box'),
    ('centrum:21', 'National Geographic'),
    ('centrum:82', 'National Geographic Wild'),
    ('centrum:19', 'Óčko'),
    ('sms:Seznam.cz TV', 'Seznam TV'),
    ('centrum:10', 'Spektrum'),
    ('centrum:30', 'Spektrum Home'),
    ('centrum:61', 'Sport1'),
    ('centrum:93', 'Sport2'),
    ('sms:TLC', 'TLC'),
    ('centrum:105', 'Travel Channel'),
    ('centrum:75', 'TV Noe'),
    ('centrum:29', 'TV Paprika'),
    ('centrum:141', 'TV5 Monde'),
    ('centrum:13', 'Viasat Explorer'),
    ('centrum:14', 'Viasat History'),
)

EXPECTED_RADIO_STATIONS = (
    ('rozhlas:radiozurnal', 'Radiožurnál'),
    ('rozhlas:dvojka', 'Dvojka'),
    ('rozhlas:vltava', 'Vltava'),
    ('rozhlas:plus', 'Plus'),
    ('rozhlas:radiozurnal-sport', 'Radiožurnál Sport'),
    ('rozhlas:radiowave', 'Radio Wave'),
    ('rozhlas:radiojunior', 'Rádio Junior'),
    ('rozhlas:d-dur', 'D-dur'),
    ('rozhlas:jazz', 'Jazz'),
    ('rozhlas:pohoda', 'Český rozhlas Pohoda'),
    ('rozhlas:cro7', 'Radio Prague International'),
    ('rozhlas:brno', 'Brno'),
    ('rozhlas:cb', 'České Budějovice'),
    ('rozhlas:hradec', 'Hradec Králové'),
    ('rozhlas:kv', 'Karlovy Vary'),
    ('rozhlas:liberec', 'Liberec'),
    ('rozhlas:olomouc', 'Olomouc'),
    ('rozhlas:ostrava', 'Ostrava'),
    ('rozhlas:pardubice', 'Pardubice'),
    ('rozhlas:plzen', 'Plzeň'),
    ('rozhlas:regina', 'Rádio Praha'),
    ('rozhlas:strednicechy', 'Střední Čechy'),
    ('rozhlas:sever', 'Sever'),
    ('rozhlas:vysocina', 'Vysočina'),
    ('rozhlas:zlin', 'Zlín'),
    ('sms:SRO1 - Slovensko', 'SRO1 - Slovensko'),
    ('sms:SRO2 - Regina Stred', 'SRO2 - Regina Stred'),
    ('sms:SRO2 - Regina Východ', 'SRO2 - Regina Východ'),
    ('sms:SRO2 - Regina Západ', 'SRO2 - Regina Západ'),
    ('sms:SRO3 - Devín', 'SRO3 - Devín'),
    ('sms:SRO4 - Radio FM', 'SRO4 - Radio FM'),
    ('sms:SRO5 - Patria', 'SRO5 - Patria'),
    ('sms:SRO6 - Slovakia International', 'SRO6 - Slovakia International'),
    ('sms:SRO8 - Litera', 'SRO8 - Litera'),
    ('sms:BBC Czech', 'BBC Czech'),
    ('sms:BBC Radio', 'BBC Radio'),
    ('sms:Classic FM', 'Classic FM'),
    ('sms:Country Radio', 'Country Radio'),
    ('sms:Dance Radio', 'Dance Radio'),
    ('sms:Evropa 2', 'Evropa 2'),
    ('sms:Fajn radio', 'Fajn radio'),
    ('sms:Frekvence 1', 'Frekvence 1'),
    ('sms:Impuls', 'Impuls'),
    ('sms:Kiss 98', 'Kiss 98'),
    ('sms:Kiss Morava', 'Kiss Morava'),
    ('sms:Radio 1', 'Radio 1'),
    ('sms:Radio7', 'Radio7'),
    ('sms:Radio Beat', 'Radio Beat'),
    ('sms:Rádio Blaník', 'Rádio Blaník'),
    ('sms:Radio Čas', 'Radio Čas'),
    ('sms:Radio Proglas', 'Radio Proglas'),
    ('sms:Europa 2', 'Europa 2'),
    ('sms:Fun rádio', 'Fun rádio'),
    ('sms:Rádio Melody', 'Rádio Melody'),
    ('sms:Lumen', 'Lumen'),
    ('sms:Rádio ROCK', 'Rádio ROCK'),
    ('sms:Radio Expres', 'Radio Expres'),
    ('sms:Radio Junior (sk)', 'Radio Junior (sk)'),
    ('sms:Rádio Liptov', 'Rádio Liptov'),
    ('sms:Rádio SiTy', 'Rádio SiTy'),
    ('sms:Rádio VIVA', 'Rádio VIVA'),
    ('sms:Rádio Vlna', 'Rádio Vlna'),
    ('sms:Radio WOW', 'Radio WOW'),
)
EXPECTED_TELEVISION_SMS_NAMES = {
    station_id.removeprefix("centrum:"): name
    for station_id, name in EXPECTED_TELEVISION_STATIONS
    if station_id.startswith("centrum:")
    and station_id.removeprefix("centrum:") not in {"12", "55", "73"}
} | {
    "6": "HBO2",
    "13": "Viasat Explore",
    "16": "Eurosport 1",
    "21": "National Geographic HD",
    "23": "CNN International",
    "29": "Paprika",
    "64": "Filmbox+ one",
    "65": "Filmbox+ hits",
    "72": "Hustler TV",
    "75": "Noe",
    "77": "Jim Jam",
    "127": "Leo",
    "141": "TV5MONDE",
    "182": "Doma",
    "184": "JOJ Plus",
    "226": "Prima LOVE",
    "556": "Prima SK",
}

EXPECTED_RADIO_SMS_NAMES = {
    "radiozurnal": "ČRo Radiožurnál",
    "dvojka": "ČRo Dvojka",
    "vltava": "ČRo Vltava",
    "plus": "ČRo Plus",
    "radiozurnal-sport": "Radiožurnál Sport",
    "radiowave": "ČRo Radio Wave",
    "radiojunior": "ČRo Rádio Junior",
    "d-dur": "ČRo D-dur",
    "jazz": "ČRo Jazz",
    "pohoda": "ČRo Pohoda",
    "cro7": "ČRo Radio Praha",
    "brno": "ČRo Brno",
    "cb": "ČRo České Budějovice",
    "hradec": "ČRo Hradec Králové",
    "kv": "ČRo Karlovy Vary",
    "liberec": "ČRo Liberec",
    "olomouc": "ČRo Olomouc",
    "ostrava": "ČRo Ostrava",
    "pardubice": "ČRo Pardubice",
    "plzen": "ČRo Plzeň",
    "regina": "ČRo Regina DAB Praha",
    "strednicechy": "ČRo Region",
    "sever": "ČRo Sever",
    "vysocina": "ČRo Vysočina",
    "zlin": "ČRo Zlín",
}




class GuideParserTests(unittest.TestCase):
    def test_models_and_prague_time_are_strict_and_host_independent(self) -> None:
        with self.assertRaises(ValueError):
            GuideDate(2026, 2, 29)
        self.assertEqual(GuideDate(2024, 2, 29), GuideDate(2024, 2, 29))
        self.assertEqual(
            GuideDate(2026, 1, 2),
            GuideTime.date_at(instant("2026-01-01T23:30:00+00:00")),
        )
        self.assertEqual(
            "2026-07-02 00:30 +0200",
            GuideTime.format_instant(
                instant("2026-07-01T22:30:00+00:00"), "%Y-%m-%d %H:%M %z"
            ),
        )

    def test_centrum_catalog_and_program(self) -> None:
        stations = parse_centrum_stations(fixture("centrum_channels.json"))
        self.assertEqual(["centrum:1", "centrum:3"], [station.id for station in stations])
        entries = parse_centrum_program(fixture("centrum_program.json"), "3")
        self.assertEqual(2, len(entries))
        self.assertEqual("centrum:3:9001", entries[0].id)
        self.assertEqual(instant("2026-07-22T18:00:00+00:00"), entries[0].start_millis)
        self.assertEqual(instant("2026-07-22T18:30:00+00:00"), entries[0].end_millis)
        self.assertEqual("Film & zábava", entries[1].title)
        self.assertFalse(entries[0].audio_description_known)

    def test_rozhlas_catalog_program_links_and_offsets(self) -> None:
        stations = parse_rozhlas_stations(fixture("rozhlas_stations.json"))
        self.assertEqual(
            ["rozhlas:radiozurnal", "rozhlas:dvojka"],
            [station.id for station in stations],
        )
        entries = parse_rozhlas_program(fixture("rozhlas_program.json"), "radiozurnal")
        self.assertEqual(2, len(entries))
        self.assertEqual(instant("2026-07-21T22:00:00+00:00"), entries[0].start_millis)
        self.assertEqual("https://radiozurnal.rozhlas.cz/zpravy", entries[0].program_url)
        self.assertIsNone(entries[0].archive_url)
        self.assertEqual(
            "https://api.mujrozhlas.cz/show-redirect/5997743",
            entries[1].archive_url,
        )

    def test_sms_windows_1250_and_midnight_rollover(self) -> None:
        entries = parse_sms_program(
            fixture("sms_program.html"), GuideDate(2026, 7, 22), "Test"
        )
        self.assertEqual(2, len(entries))
        self.assertEqual("Noční pořad", entries[0].title)
        self.assertEqual("První & zajímavý popis", entries[0].description)
        self.assertEqual("sms:2140925363", entries[0].id)
        self.assertEqual(instant("2026-07-22T21:50:00+00:00"), entries[0].start_millis)
        self.assertEqual(instant("2026-07-22T22:15:00+00:00"), entries[1].start_millis)
        self.assertEqual(entries[1].start_millis, entries[0].end_millis)

    def test_ct_xml_dates_durations_links_audio_description_and_security(self) -> None:
        entries = parse_ct_program(
            fixture("ct_program.xml"), GuideDate(2026, 7, 22), "ct1"
        )
        self.assertEqual(2, len(entries))
        self.assertEqual("Noční dokument (2/4) – První část", entries[0].title)
        self.assertEqual(instant("2026-07-22T21:50:00+00:00"), entries[0].start_millis)
        self.assertEqual(entries[0].start_millis + 25 * 60_000 + 30_000, entries[0].end_millis)
        self.assertTrue(entries[0].audio_description)
        self.assertTrue(entries[0].audio_description_known)
        self.assertFalse(entries[1].audio_description)
        self.assertTrue(entries[1].audio_description_known)
        self.assertEqual(
            "https://www.ceskatelevize.cz/porady/100/prvni/",
            entries[0].program_url,
        )
        with self.assertRaises(GuideParseException):
            parse_ct_program(
                b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><program>&e;</program>',
                GuideDate(2026, 7, 22),
                "ct1",
            )

    def test_ct_web_all_channels_rollover_durations_and_ad(self) -> None:
        schedules = parse_ct_web_program(
            fixture("ct_web_program.html"), GuideDate(2026, 7, 22)
        )
        self.assertEqual(["ct1", "ct2", "ct24", "ct4", "ct5", "ct6"], list(schedules))
        self.assertEqual(3, len(schedules["ct1"]))
        first = schedules["ct1"][0]
        self.assertEqual("Noční dokument (2/4) – První část", first.title)
        self.assertEqual("První & zajímavý popis pořadu.", first.description)
        self.assertTrue(first.audio_description)
        self.assertTrue(all(entry.audio_description_known for values in schedules.values() for entry in values))
        self.assertEqual(first.start_millis + 25 * 60_000, first.end_millis)
        self.assertEqual(instant("2026-07-22T23:00:00+00:00"), schedules["ct1"][2].start_millis)
        self.assertEqual("Večerníček", schedules["ct5"][0].title)


class GuideRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        GuideRepository.clear_process_cache_for_tests()

    def tearDown(self) -> None:
        GuideRepository.clear_process_cache_for_tests()

    def test_stable_catalog_and_live_merge(self) -> None:
        def unexpected(_url: str, _accept: str) -> bytes:
            raise AssertionError("The curated television catalog must be local.")

        source = _FakeSource(unexpected)
        repository = GuideRepository(source)
        television = repository.fallback_stations(GuideMedium.TELEVISION)
        radio = repository.fallback_stations(GuideMedium.RADIO)
        self.assertEqual(
            list(EXPECTED_TELEVISION_STATIONS),
            [(station.id, station.name) for station in television],
        )
        self.assertEqual(
            list(EXPECTED_RADIO_STATIONS),
            [(station.id, station.name) for station in radio],
        )

        refreshed_tv = repository.refresh_stations(GuideMedium.TELEVISION)
        self.assertEqual(
            list(EXPECTED_TELEVISION_STATIONS),
            [(station.id, station.name) for station in refreshed_tv],
        )
        self.assertEqual([], source.urls)

        radio_source = _FakeSource(
            lambda _url, _accept: fixture("rozhlas_stations.json")
        )
        refreshed_radio = GuideRepository(radio_source).refresh_stations(
            GuideMedium.RADIO
        )
        self.assertEqual(
            list(EXPECTED_RADIO_STATIONS),
            [(station.id, station.name) for station in refreshed_radio],
        )

    def test_catalog_ids_and_provider_keys_are_safe(self) -> None:
        television = GuideRepository().fallback_stations(
            GuideMedium.TELEVISION
        )
        radio = GuideRepository().fallback_stations(GuideMedium.RADIO)
        self.assertEqual(
            EXPECTED_TELEVISION_SMS_NAMES, TELEVISION_SMS_NAMES
        )
        self.assertEqual(EXPECTED_RADIO_SMS_NAMES, RADIO_SMS_NAMES)
        self.assertEqual(107, len(television))
        self.assertEqual(63, len(radio))
        for stations in (television, radio):
            ids = [station.id for station in stations]
            names = [station.name for station in stations]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(name == name.strip() for name in names))

        television_centrum_ids: set[str] = set()
        for station in television:
            if station.id.startswith("centrum:"):
                provider_id = station.id.removeprefix("centrum:")
                self.assertRegex(provider_id, r"^[0-9]+$")
                television_centrum_ids.add(provider_id)
            else:
                self.assertTrue(station.id.startswith("sms:"))
                self.assertTrue(station.id.removeprefix("sms:").strip())
        self.assertLessEqual(
            set(TELEVISION_SMS_NAMES), television_centrum_ids
        )

        official_radio_ids: set[str] = set()
        for station in radio:
            if station.id.startswith("rozhlas:"):
                provider_id = station.id.removeprefix("rozhlas:")
                self.assertIsNotNone(re.fullmatch(r"[a-z0-9-]+", provider_id))
                official_radio_ids.add(provider_id)
            else:
                self.assertTrue(station.id.startswith("sms:"))
                self.assertTrue(station.id.removeprefix("sms:").strip())
        self.assertEqual(set(RADIO_SMS_NAMES), official_radio_ids)

        sms_names = [
            station.id.removeprefix("sms:")
            for station in (*television, *radio)
            if station.id.startswith("sms:")
        ]
        sms_names.extend(TELEVISION_SMS_NAMES.values())
        sms_names.extend(RADIO_SMS_NAMES.values())
        for sms_name in sms_names:
            sms_name.encode("cp1250", errors="strict")
            self.assertTrue(quote_plus(sms_name, encoding="cp1250"))

    def test_every_television_sms_fallback_uses_exact_provider_key(self) -> None:
        guide_date = GuideDate(2026, 7, 22)
        television_names = {
            station_id.removeprefix("centrum:"): name
            for station_id, name in EXPECTED_TELEVISION_STATIONS
            if station_id.startswith("centrum:")
        }

        for channel, sms_name in EXPECTED_TELEVISION_SMS_NAMES.items():
            with self.subTest(channel=channel, sms_name=sms_name):
                GuideRepository.clear_process_cache_for_tests()

                def response(url: str, _accept: str) -> bytes:
                    if "/tv-program/" in url or "/services-old/" in url:
                        raise GuideNetworkException("offline")
                    if "api/broadcasting" in url:
                        return f'{{"{channel}":[]}}'.encode("ascii")
                    if "m.tv.sms.cz" in url:
                        return fixture("sms_program.html")
                    raise AssertionError(url)

                source = _FakeSource(response)
                entries = GuideRepository(source).load_program(
                    GuideStation(
                        f"centrum:{channel}",
                        television_names[channel],
                        GuideMedium.TELEVISION,
                    ),
                    guide_date,
                )
                expected_centrum_url = (
                    "https://tvprogram.centrum.cz/api/broadcasting/"
                    f"2026-07-22?channels%5B%5D={channel}"
                )
                expected_sms_url = (
                    "https://m.tv.sms.cz/?cas=0&den=2026-07-22"
                    f"&stanice={quote_plus(sms_name, encoding='cp1250')}"
                )
                self.assertEqual(2, len(entries))
                self.assertIn(expected_centrum_url, source.urls)
                self.assertEqual(expected_sms_url, source.urls[-1])

    def test_every_direct_sms_station_routes_its_persisted_key(self) -> None:
        guide_date = GuideDate(2026, 7, 22)
        catalogs = (
            (GuideMedium.TELEVISION, EXPECTED_TELEVISION_STATIONS),
            (GuideMedium.RADIO, EXPECTED_RADIO_STATIONS),
        )
        for medium, catalog in catalogs:
            for station_id, name in catalog:
                if not station_id.startswith("sms:"):
                    continue
                provider_key = station_id.removeprefix("sms:")
                with self.subTest(medium=medium, provider_key=provider_key):
                    source = _FakeSource(
                        lambda url, _accept: fixture("sms_program.html")
                        if "m.tv.sms.cz" in url
                        else (_ for _ in ()).throw(AssertionError(url))
                    )
                    entries = GuideRepository(source).load_program(
                        GuideStation(station_id, name, medium),
                        guide_date,
                    )
                    expected_url = (
                        "https://m.tv.sms.cz/?cas=0&den=2026-07-22"
                        f"&stanice={quote_plus(provider_key, encoding='cp1250')}"
                    )
                    self.assertEqual(2, len(entries))
                    self.assertEqual([expected_url], source.urls)

    def test_every_official_radio_station_uses_exact_sms_fallback(self) -> None:
        guide_date = GuideDate(2026, 7, 22)
        radio_names = {
            station_id.removeprefix("rozhlas:"): name
            for station_id, name in EXPECTED_RADIO_STATIONS
            if station_id.startswith("rozhlas:")
        }
        for station_id, sms_name in EXPECTED_RADIO_SMS_NAMES.items():
            with self.subTest(station_id=station_id, sms_name=sms_name):
                def response(url: str, _accept: str) -> bytes:
                    if "api.rozhlas.cz" in url:
                        raise GuideNetworkException("offline")
                    if "m.tv.sms.cz" in url:
                        return fixture("sms_program.html")
                    raise AssertionError(url)

                source = _FakeSource(response)
                entries = GuideRepository(source).load_program(
                    GuideStation(
                        f"rozhlas:{station_id}",
                        radio_names[station_id],
                        GuideMedium.RADIO,
                    ),
                    guide_date,
                )
                expected_urls = [
                    "https://api.rozhlas.cz/data/v2/schedule/day/"
                    f"2026/07/22/{station_id}.json",
                    "https://m.tv.sms.cz/?cas=0&den=2026-07-22"
                    f"&stanice={quote_plus(sms_name, encoding='cp1250')}",
                ]
                self.assertEqual(2, len(entries))
                self.assertEqual(expected_urls, source.urls)


    def test_television_sort_keeps_station_names_attached_to_ids(self) -> None:
        ct = GuideStation("centrum:1", "Živé ČT1", GuideMedium.TELEVISION)
        nova_sport = GuideStation(
            "centrum:465",
            "Živá Nova Sport 2",
            GuideMedium.TELEVISION,
        )
        nova_sport_6 = GuideStation(
            "sms:Nova Sport 6",
            "Živá Nova Sport 6",
            GuideMedium.TELEVISION,
        )
        nova_2 = GuideStation(
            "future:nova-2",
            "Nova 2",
            GuideMedium.TELEVISION,
        )
        nova_10 = GuideStation(
            "future:nova-10",
            "Nova 10",
            GuideMedium.TELEVISION,
        )
        prima = GuideStation(
            "centrum:4",
            "Živá Prima",
            GuideMedium.TELEVISION,
        )
        prima_show = GuideStation(
            "future:prima-show",
            "Prima Show",
            GuideMedium.TELEVISION,
        )
        unknown = GuideStation(
            "future:alpha",
            "Alpha",
            GuideMedium.TELEVISION,
        )
        scrambled = [
            unknown,
            nova_10,
            prima_show,
            nova_sport,
            nova_sport_6,
            nova_2,
            prima,
            ct,
        ]

        ordered = order_guide_stations(
            scrambled,
            GuideMedium.TELEVISION,
        )

        expected = [
            ct,
            nova_sport,
            nova_sport_6,
            nova_2,
            nova_10,
            prima,
            prima_show,
            unknown,
        ]
        self.assertEqual(expected, ordered)
        for expected_station, actual_station in zip(
            expected,
            ordered,
            strict=True,
        ):
            self.assertIs(expected_station, actual_station)

    def test_public_ct_page_is_preferred_and_cached_across_instances(self) -> None:
        def response(url: str, _accept: str) -> bytes:
            if "/tv-program/" in url:
                return fixture("ct_web_program.html")
            raise AssertionError(f"Unexpected fallback request: {url}")

        source = _FakeSource(response)
        guide_date = GuideDate(2026, 7, 22)
        ct1 = GuideRepository(source).load_program(
            GuideStation("centrum:1", "ČT1", GuideMedium.TELEVISION), guide_date
        )
        ct2 = GuideRepository(source).load_program(
            GuideStation("centrum:2", "ČT2", GuideMedium.TELEVISION), guide_date
        )
        self.assertTrue(ct1 and ct2)
        self.assertEqual(1, sum("/tv-program/" in url for url in source.urls))
        self.assertFalse(any("services-old" in url for url in source.urls))

    def test_total_ct_fallback_throttles_xml_and_marks_ad_unknown(self) -> None:
        centrum = fixture("centrum_program.json").replace(b'"3":', b'"1":', 1)

        def response(url: str, _accept: str) -> bytes:
            if "/tv-program/" in url:
                return b"<html><body></body></html>"
            if "/services-old/" in url:
                return b"<errors><error>rate limit</error></errors>"
            if "tvprogram.centrum.cz/api/broadcasting" in url:
                return centrum
            raise AssertionError(url)

        source = _FakeSource(response)
        repository = GuideRepository(source)
        station = GuideStation("centrum:1", "ČT1", GuideMedium.TELEVISION)
        first = repository.load_program(station, GuideDate(2026, 7, 22))
        second = repository.load_program(station, GuideDate(2026, 7, 22))
        self.assertTrue(first and second)
        self.assertTrue(all(not entry.audio_description_known for entry in first + second))
        self.assertEqual(1, sum("/services-old/" in url for url in source.urls))
        self.assertEqual(2, sum("api/broadcasting" in url for url in source.urls))

    def test_non_ct_uses_centrum_and_radio_falls_back_to_encoded_sms(self) -> None:
        centrum_source = _FakeSource(
            lambda url, _accept: fixture("centrum_program.json")
            if "channels%5B%5D=3" in url
            else (_ for _ in ()).throw(AssertionError(url))
        )
        entries = GuideRepository(centrum_source).load_program(
            GuideStation("centrum:3", "Nova", GuideMedium.TELEVISION),
            GuideDate(2026, 7, 22),
        )
        self.assertEqual(2, len(entries))
        self.assertFalse(any("ceskatelevize" in url for url in centrum_source.urls))

        television_source = _FakeSource(
            lambda url, _accept: fixture("sms_program.html")
            if "m.tv.sms.cz" in url
            else (_ for _ in ()).throw(AssertionError(url))
        )
        television_entries = GuideRepository(television_source).load_program(
            GuideStation(
                "sms:Nova Sport 6",
                "Nova Sport 6",
                GuideMedium.TELEVISION,
            ),
            GuideDate(2026, 7, 22),
        )
        self.assertEqual(2, len(television_entries))
        self.assertEqual(
            [
                "https://m.tv.sms.cz/?cas=0&den=2026-07-22"
                "&stanice=Nova+Sport+6"
            ],
            television_source.urls,
        )

        def radio_response(url: str, _accept: str) -> bytes:
            if "api.rozhlas.cz" in url:
                raise GuideNetworkException("offline")
            if "m.tv.sms.cz" in url:
                return fixture("sms_program.html")
            raise AssertionError(url)

        radio_source = _FakeSource(radio_response)
        radio_entries = GuideRepository(radio_source).load_program(
            GuideStation("rozhlas:radiozurnal", "Radiožurnál", GuideMedium.RADIO),
            GuideDate(2026, 7, 22),
        )
        self.assertEqual(2, len(radio_entries))
        self.assertIn("stanice=%C8Ro+Radio%9Eurn%E1l", radio_source.urls[1])

    def test_empty_secondary_source_preserves_primary_failure(self) -> None:
        guide_date = GuideDate(2026, 7, 22)
        stations = (
            GuideStation(
                "centrum:3",
                "Nova",
                GuideMedium.TELEVISION,
            ),
            GuideStation(
                "rozhlas:radiozurnal",
                "Radiožurnál",
                GuideMedium.RADIO,
            ),
        )
        for station in stations:
            with self.subTest(station=station.id):
                def response(url: str, _accept: str) -> bytes:
                    if "m.tv.sms.cz" in url:
                        return b"<html><body></body></html>"
                    raise GuideNetworkException("primary offline")

                source = _FakeSource(response)
                with self.assertRaisesRegex(
                    GuideNetworkException,
                    "primary offline",
                ):
                    GuideRepository(source).load_program(station, guide_date)
                self.assertEqual(2, len(source.urls))
                self.assertIn("m.tv.sms.cz", source.urls[-1])

    def test_unsupported_provider_is_rejected_before_network(self) -> None:
        source = _FakeSource(lambda _url, _accept: b"")
        with self.assertRaises(ValueError):
            GuideRepository(source).load_program(
                GuideStation("other:1", "Other", GuideMedium.TELEVISION),
                GuideDate(2026, 7, 22),
            )
        self.assertEqual([], source.urls)


class GuideHttpClientTests(unittest.TestCase):
    def test_https_redirect_and_size_limit(self) -> None:
        first = _FakeResponse(302, b"", {"Location": "/final"})
        second = _FakeResponse(200, b"guide-data")
        session = _FakeSession([first, second])
        client = GuideHttpClient(session, maximum_response_bytes=20)
        self.assertEqual(b"guide-data", client.get("https://example.test/start", "text/plain"))
        self.assertEqual(
            ["https://example.test/start", "https://example.test/final"], session.urls
        )
        self.assertTrue(first.closed and second.closed)

        with self.assertRaises(GuideNetworkException):
            GuideHttpClient(_FakeSession([])).get("http://example.test/plain", "text/plain")
        with self.assertRaises(GuideNetworkException):
            GuideHttpClient(
                _FakeSession([_FakeResponse(302, b"", {"Location": "http://bad.test/"})])
            ).get("https://example.test/start", "text/plain")
        with self.assertRaises(GuideNetworkException):
            GuideHttpClient(
                _FakeSession([_FakeResponse(200, b"123456")]),
                maximum_response_bytes=5,
            ).get("https://example.test/data", "text/plain")

    def test_cancelled_client_rejects_new_requests(self) -> None:
        session = _FakeSession([])
        client = GuideHttpClient(session)
        client.cancel()
        with self.assertRaises(GuideNetworkException):
            client.get("https://example.test/data", "text/plain")
        self.assertEqual([], session.urls)


class _FakeSource:
    def __init__(self, response):
        self.response = response
        self.urls: list[str] = []

    def get(self, url: str, accept: str) -> bytes:
        self.urls.append(url)
        return self.response(url, accept)


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, headers=None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.reason = "Test response"
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


if __name__ == "__main__":
    unittest.main()
