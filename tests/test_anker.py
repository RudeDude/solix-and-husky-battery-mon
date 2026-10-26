"""Advertisement, status formatting, and LAN text for the F3800."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from anker_monitor.advert import (
    address_aliases,
    advertisement_matches,
    normalize_address,
    parse_manufacturer,
)
from anker_monitor.client import (
    _LiveSession,
    format_result,
    read_station,
    watch_station,
)
from anker_monitor.cloud import fields_from_mqtt, select_station
from anker_monitor.lan import LanProbe, describe_lan
from anker_monitor.reading import format_reading, reading_from_fields


class AdvertTests(unittest.TestCase):
    def test_live_record_carries_the_identity_mac(self) -> None:
        identity = parse_manufacturer(bytes.fromhex("0111223344556700"))
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.version, 1)
        self.assertEqual(identity.mac, "11:22:33:44:55:67")
        self.assertEqual(identity.bind_type, 0)

    def test_short_or_unknown_records_are_ignored(self) -> None:
        self.assertIsNone(parse_manufacturer(b"\x01\x02"))
        self.assertIsNone(parse_manufacturer(bytes.fromhex("0311223344556700")))

    def test_label_mac_matches_the_advertised_identity(self) -> None:
        self.assertEqual(
            address_aliases("11-22-33-44-55-66"),
            {"11:22:33:44:55:66", "11:22:33:44:55:67"},
        )
        self.assertEqual(normalize_address("aa:bb:cc:dd:ee:ff"), "AA:BB:CC:DD:EE:FF")

    def test_name_or_identity_selects_the_station(self) -> None:
        record = bytes.fromhex("0111223344556700")
        self.assertTrue(
            advertisement_matches(
                name="AnkerAdv",
                address="22:33:44:55:66:77",
                manufacturer=record,
                wanted_name="ankeradv",
                wanted_address="11:22:33:44:55:66",
            )
        )
        self.assertFalse(
            advertisement_matches(
                name="BB-GOLF-BT1128",
                address="01:02:03:04:05:06",
                manufacturer=None,
                wanted_name="AnkerAdv",
                wanted_address="11:22:33:44:55:66",
            )
        )


class ReadingTests(unittest.TestCase):
    def test_sentinels_stay_blank_and_a_real_zero_is_kept(self) -> None:
        reading = reading_from_fields(
            {
                "soc_percent": 91,
                "soc_other_percent": 95,
                "temperature_c": 0,
                "ac_power_out_w": 0,
                "ac_power_in_w": -1,
                "hours_remaining": -1.0,
                "serial_number": "Unknown",
                "charging": "solar",
            }
        )
        self.assertEqual(reading.soc_percent, 91.0)
        self.assertEqual(reading.soc_other_percent, 95.0)
        self.assertEqual(reading.temperature_c, 0.0)
        self.assertEqual(reading.ac_power_out_w, 0)
        self.assertIsNone(reading.ac_power_in_w)
        self.assertIsNone(reading.hours_remaining)
        self.assertIsNone(reading.serial_number)
        text = format_reading(reading)
        self.assertIn("state of charge: 91.0%", text)
        self.assertIn("temperature: 0.0 C", text)
        self.assertIn("expansion pack", text)
        self.assertNotIn("AC in", text)


class CloudMapTests(unittest.TestCase):
    def test_published_f3800_tags_keep_the_app_charge(self) -> None:
        reading = reading_from_fields(
            fields_from_mqtt(
                {
                    "battery_soc": 91,
                    "main_battery_soc": 95,
                    "temperature": 28,
                    "ac_output_power": 63,
                    "ac_input_power": 0,
                    "photovoltaic_power": 223,
                    "pv_1_power": 141,
                    "pv_2_power": 82,
                    "bat_charge_power": 200,
                    "output_power_total": 63,
                    "bat_discharge_power?": 0,
                    "usbc_1_power": 0,
                    "ac_output_power_switch": 1,
                    "charging_status": 1,
                    "remaining_time_hours": 24.0,
                    "device_sn": "SN1",
                }
            ),
            source="solix-mqtt",
        )
        self.assertEqual(reading.soc_percent, 91.0)
        self.assertEqual(reading.soc_other_percent, 95.0)
        self.assertEqual(reading.ac_power_out_w, 63)
        self.assertEqual(reading.discharge_power_w, 0)
        self.assertEqual(reading.ac_output, "on")
        self.assertEqual(reading.charging, "solar")
        self.assertEqual(reading.serial_number, "SN1")
        self.assertEqual(reading.source, "solix-mqtt")

    def test_cloud_cache_strings_keep_charge_and_watts(self) -> None:
        reading = reading_from_fields(
            fields_from_mqtt(
                {
                    "battery_soc": "91",
                    "main_battery_soc": "91",
                    "temperature": "21",
                    "ac_input_power": "232",
                    "ac_output_power": "233",
                    "output_power_total": "233",
                    "usbc_1_power": "0.000",
                    "ac_output_power_switch": 1,
                    "charging_status": 2,
                    "device_sn": "SN1",
                }
            ),
            source="solix-mqtt",
        )
        self.assertEqual(reading.soc_percent, 91.0)
        self.assertIsNone(reading.soc_other_percent)
        self.assertNotIn("other charge", format_reading(reading))
        self.assertEqual(reading.ac_power_in_w, 232)
        self.assertEqual(reading.ac_power_out_w, 233)
        self.assertEqual(reading.output_power_w, 233)
        self.assertEqual(reading.usb_c1_w, 0)
        self.assertEqual(reading.temperature_c, 21.0)
        self.assertEqual(reading.charging, "ac")
        self.assertIsNone(
            reading_from_fields(fields_from_mqtt({"battery_soc": "-1"})).soc_percent
        )

    def test_station_selection_prefers_an_online_f3800(self) -> None:
        chosen = select_station(
            {
                "other": {"device_pn": "A17C0", "device_sn": "SOLARBANK"},
                "offline": {
                    "device_pn": "A1790",
                    "device_sn": "OFFLINE",
                    "wifi_online": False,
                },
                "online": {
                    "device_pn": "A1790P",
                    "device_sn": "ONLINE",
                    "wifi_online": True,
                },
            }
        )
        assert chosen is not None
        self.assertEqual(chosen["device_sn"], "ONLINE")
        self.assertEqual(
            select_station(
                {"one": {"device_pn": "A1790", "device_sn": "SN1"}},
                serial="SN1",
            )["device_sn"],
            "SN1",
        )
        self.assertIsNone(
            select_station({"one": {"device_pn": "A17C0", "device_sn": "X"}})
        )


class _FakeStation:
    def __init__(self) -> None:
        self.connected = True
        self.available = True
        self.battery_percentage = 91
        self.ac_power_out = 213
        self.closed = False

    async def disconnect(self) -> None:
        self.closed = True
        self.connected = False


class _TimeoutModule:
    NEGOTIATION_TIMEOUT = 25


class WatchSessionTests(unittest.TestCase):
    def _session(self, station: _FakeStation) -> _LiveSession:
        return _LiveSession(
            station=station,
            name="AnkerAdv",
            address="22:33:44:55:66:77",
            identity_mac="11:22:33:44:55:67",
            lan=LanProbe(
                host="192.168.1.42",
                reachable=True,
                modbus_open=False,
                detail="lan probe once",
            ),
            timeout_module=_TimeoutModule(),
            previous_timeout=90,
        )

    def test_one_shot_closes_the_radio(self) -> None:
        station = _FakeStation()
        session = self._session(station)

        async def opener(**_kwargs: object) -> _LiveSession:
            return session

        async def run() -> None:
            with patch("anker_monitor.client._open_station", opener):
                result = await read_station(address="11:22:33:44:55:66", wait=0)
            self.assertEqual(result.reading.soc_percent, 91.0)
            self.assertIn("lan probe once", format_result(result))
            self.assertTrue(station.closed)
            self.assertEqual(session.timeout_module.NEGOTIATION_TIMEOUT, 90)

        asyncio.run(run())

    def test_watch_reprints_without_disconnecting(self) -> None:
        station = _FakeStation()
        session = self._session(station)

        async def opener(**_kwargs: object) -> _LiveSession:
            return session

        async def run() -> None:
            with patch("anker_monitor.client._open_station", opener):
                samples = watch_station(
                    address="11:22:33:44:55:66",
                    wait=0,
                    interval=0.01,
                )
                first = await anext(samples)
                self.assertFalse(station.closed)
                self.assertEqual(first.reading.ac_power_out_w, 213)
                self.assertIn("lan probe once", format_result(first))
                station.ac_power_out = 40
                second = await anext(samples)
                self.assertFalse(station.closed)
                self.assertEqual(second.reading.ac_power_out_w, 40)
                held = format_result(second, held=True)
                self.assertIn("AC out: 40 W", held)
                self.assertNotIn("lan probe once", held)
                station.connected = False
                station.available = False
                down = session.result()
                self.assertIsNone(down.reading)
                self.assertIn("link dropped", down.detail)
                await samples.aclose()
            self.assertTrue(station.closed)

        asyncio.run(run())


class LanTests(unittest.TestCase):
    def test_unreachable_host_does_not_invent_a_register_map(self) -> None:
        text = describe_lan("192.168.1.42", reachable=False, modbus_open=False)
        self.assertIn("192.168.1.42", text)
        self.assertIn("no F3800 register map", text)

    def test_open_modbus_port_is_left_unread(self) -> None:
        text = describe_lan("192.168.1.42", reachable=True, modbus_open=True)
        self.assertIn("left unread", text)


if __name__ == "__main__":
    unittest.main()
