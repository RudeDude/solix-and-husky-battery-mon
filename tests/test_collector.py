"""NDJSON records omit blank sensors and keep real zeros."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from anker_monitor.client import MonitorError
from anker_monitor.client import ReadResult as AnkerResult
from anker_monitor.lan import LanProbe
from anker_monitor.reading import StationReading
from collector.__main__ import (
    _ButtonPrompt,
    _anker_ready,
    _needs_iot_button,
    _open_anker,
    _sample_anker,
    _short_anker_error,
    _write,
)
from collector.log import DayLog
from collector.records import anker_record, husky_record
from husky_monitor.client import ReadResult as HuskyResult
from husky_monitor.protocol import Cell, PackReading, Probe


class RecordTests(unittest.TestCase):
    def test_husky_drops_blank_sensors_and_keeps_zero_current(self) -> None:
        reading = PackReading(
            address=1,
            cells=[Cell(1, 3.325), Cell(2, 0.0)],
            probes=[
                Probe("Temperature 1", 21.0, True),
                Probe("Temperature 2", 0.0, True),
                Probe("Temperature 3", 19.0, True),
            ],
            voltage_v=53.22,
            current_a=0.0,
            soc_percent=99.0,
            soh_percent=None,
            capacity_ah=None,
            charge_state="idle",
            source="zeta-modbus",
            notes=[
                "this radio reports a few pack temperature sensors, not one sensor on every cell"
            ],
        )
        record = husky_record(
            HuskyResult("BB-GOLF-BT1128", "01:02:03:04:05:06", reading),
            "2026-10-25T12:00:00-07:00",
        )
        self.assertEqual(record["current_a"], 0.0)
        self.assertEqual(record["soc_percent"], 99.0)
        self.assertNotIn("soh_percent", record)
        self.assertNotIn("capacity_ah", record)
        self.assertNotIn("notes", record)
        self.assertEqual(
            [item["name"] for item in record["temperatures_c"]],
            ["Temperature 1", "Temperature 3"],
        )
        self.assertEqual([item["index"] for item in record["cells_v"]], [1])

    def test_f3800_drops_missing_power_and_keeps_zero_watts(self) -> None:
        reading = StationReading(
            soc_percent=91.0,
            temperature_c=21.0,
            ac_power_in_w=0,
            ac_power_out_w=213,
            solar_power_w=None,
            output_power_w=213,
            charging="ac",
            ac_output="on",
            serial_number="SN1",
            source="solix-ble",
        )
        record = anker_record(
            AnkerResult(
                name="AnkerAdv",
                address="22:33:44:55:66:77",
                identity_mac="11:22:33:44:55:67",
                negotiated=True,
                reading=reading,
                lan=LanProbe("192.168.1.42", True, False, "unused"),
            ),
            "2026-10-25T12:00:00-07:00",
        )
        self.assertEqual(record["soc_percent"], 91.0)
        self.assertEqual(record["ac_power_in_w"], 0)
        self.assertNotIn("solar_power_w", record)
        self.assertNotIn("usb_c1_w", record)
        self.assertNotIn("hours_remaining", record)
        self.assertNotIn("soc_other_percent", record)

    def test_ndjson_is_one_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.ndjson"
            with path.open("a", encoding="utf-8") as handle:
                _write(handle, {"device": "husky", "soc_percent": 99.0})
                _write(handle, {"device": "f3800", "soc_percent": 91.0})
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["device"], "husky")
        self.assertEqual(json.loads(lines[1])["soc_percent"], 91.0)

    def test_day_folders_roll_at_midnight(self) -> None:
        zone = timezone(timedelta(hours=-4))
        evening = datetime(2026, 10, 25, 23, 59, tzinfo=zone)
        morning = datetime(2026, 10, 26, 0, 1, tzinfo=zone)
        with tempfile.TemporaryDirectory() as directory:
            log = DayLog(Path(directory))
            first = log.write(evening, {"device": "husky", "soc_percent": 99.0})
            log.write(evening, {"device": "f3800", "soc_percent": 91.0})
            second = log.write(morning, {"device": "husky", "soc_percent": 98.0})
            log.close()
            self.assertEqual(first, Path(directory) / "2026" / "10" / "25" / "readings.ndjson")
            self.assertEqual(second, Path(directory) / "2026" / "10" / "26" / "readings.ndjson")
            self.assertEqual(len(first.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["soc_percent"], 98.0)
            again = DayLog(Path(directory))
            again.write(evening, {"device": "husky", "soc_percent": 97.0})
            again.close()
            self.assertEqual(len(first.read_text(encoding="utf-8").splitlines()), 3)


class _Station:
    def __init__(self, *, connected: bool, available: bool) -> None:
        self.connected = connected
        self.available = available


class _Session:
    def __init__(self, station: _Station) -> None:
        self.station = station
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.station.connected = False

    def result(self) -> AnkerResult:
        return AnkerResult(
            name="station",
            address="AA:BB:CC:DD:EE:FF",
            identity_mac=None,
            negotiated=True,
            reading=None,
            lan=LanProbe("192.0.2.10", False, False, "unused"),
            detail="The encryption handshake finished, and the station sent no status frame.",
        )


class ReconnectTests(unittest.TestCase):
    def test_silent_session_is_not_ready_and_logs_a_short_error(self) -> None:
        session = _Session(_Station(connected=True, available=False))
        self.assertFalse(_anker_ready(session))
        record = _sample_anker(session, "2026-10-25T19:16:00-04:00")
        self.assertEqual(record["error"], "handshake finished, no status frame")
        self.assertNotIn("firmware", str(record["error"]))

    def test_missing_advertisement_asks_for_the_button(self) -> None:
        long = (
            "no advertisement matched name 'AnkerAdv' or address 'AA'. "
            "Set ANKER_BT_NAME and ANKER_BT_ADDRESS in .env."
        )
        short = _short_anker_error(long)
        self.assertEqual(short, "not advertising")
        self.assertTrue(_needs_iot_button(short))
        self.assertFalse(_needs_iot_button("computer bluetooth radio is off"))

    def test_button_reminder_is_not_repeated_every_sample(self) -> None:
        prompt = _ButtonPrompt(every=60.0)
        first = io.StringIO()
        second = io.StringIO()
        with redirect_stderr(first):
            self.assertTrue(prompt.maybe("not advertising", now=10.0))
        with redirect_stderr(second):
            self.assertFalse(prompt.maybe("not advertising", now=20.0))
            self.assertTrue(prompt.maybe("not advertising", now=70.0))
        self.assertIn("Press the IoT button", first.getvalue())
        self.assertIn("7 seconds", first.getvalue())
        self.assertEqual(second.getvalue().count("Press the IoT button"), 1)

    def test_silent_handshake_is_closed_so_the_next_cycle_scans(self) -> None:
        session = _Session(_Station(connected=True, available=False))
        args = argparse.Namespace(
            anker_name="station",
            anker_address="AA:BB:CC:DD:EE:FF",
            anker_timeout=1.0,
            anker_host="192.0.2.10",
            anker_wait=20.0,
        )

        async def opener(**_kwargs: object) -> _Session:
            return session

        async def wait(_station: object, _seconds: float) -> None:
            return None

        async def run() -> tuple[object | None, str]:
            with (
                patch("collector.__main__._open_station", opener),
                patch("collector.__main__._wait_until_available", wait),
            ):
                return await _open_anker(args)

        got, trouble = asyncio.run(run())
        self.assertIsNone(got)
        self.assertEqual(trouble, "handshake finished, no status frame")
        self.assertTrue(session.closed)

    def test_advertisement_miss_stays_a_short_error(self) -> None:
        args = argparse.Namespace(
            anker_name="station",
            anker_address="AA:BB:CC:DD:EE:FF",
            anker_timeout=1.0,
            anker_host="192.0.2.10",
            anker_wait=20.0,
        )

        async def opener(**_kwargs: object) -> _Session:
            raise MonitorError("no advertisement matched name 'station' or address 'AA'")

        async def run() -> tuple[object | None, str]:
            with patch("collector.__main__._open_station", opener):
                return await _open_anker(args)

        got, trouble = asyncio.run(run())
        self.assertIsNone(got)
        self.assertEqual(trouble, "not advertising")
        self.assertTrue(_needs_iot_button(trouble))
