from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest

from arss.guide import (
    GuideDate,
    GuideHttpClient,
    GuideMedium,
    GuideNetworkException,
    GuideParseException,
    GuideRepository,
    GuideStation,
    GuideTime,
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
        repository = GuideRepository(_FakeSource(lambda _url, _accept: b""))
        television = repository.fallback_stations(GuideMedium.TELEVISION)
        radio = repository.fallback_stations(GuideMedium.RADIO)
        expected_ids = [
            "centrum:1",
            "centrum:2",
            "centrum:24",
            "centrum:18",
            "centrum:357",
            "centrum:358",
            "centrum:3",
            "centrum:78",
            "centrum:558",
            "centrum:560",
            "centrum:559",
            "centrum:17",
            "centrum:465",
            *(f"sms:Nova Sport {number}" for number in range(3, 7)),
            "centrum:4",
            "centrum:92",
            "centrum:474",
            "centrum:608",
            "centrum:226",
            "centrum:333",
            "centrum:818",
            "centrum:89",
            "centrum:5",
            "centrum:6",
            "centrum:7",
            "centrum:11",
            "centrum:12",
            "centrum:16",
            "centrum:25",
            *(f"sms:Oneplay Sport {number}" for number in range(1, 5)),
            *(f"sms:Oneplay Sport MD{number}" for number in range(1, 11)),
        ]
        self.assertEqual(expected_ids, [station.id for station in television])
        self.assertEqual(
            [
                (f"sms:Nova Sport {number}", f"Nova Sport {number}")
                for number in range(3, 7)
            ],
            [
                (station.id, station.name)
                for station in television
                if station.id.startswith("sms:Nova Sport ")
            ],
        )
        self.assertTrue(
            any(station.id == "rozhlas:radiozurnal" for station in radio)
        )
        self.assertTrue(any(station.id == "sms:Frekvence 1" for station in radio))

        merged = GuideRepository(
            _FakeSource(lambda _url, _accept: fixture("centrum_channels.json"))
        ).refresh_stations(GuideMedium.TELEVISION)
        self.assertEqual(expected_ids, [station.id for station in merged])

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
