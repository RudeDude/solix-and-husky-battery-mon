"""Capacity estimates use continuous stretches and ignore surface charge."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from capacity.__main__ import main
from capacity.estimate import (
    INVERTER_EFF,
    NOMINAL_V,
    SOLIX_AH,
    Sample,
    analyze,
    battery_bus_watts,
    load_logs,
    render,
)


def _at(minute: float, **kwargs: float) -> Sample:
    base = datetime.fromisoformat("2026-10-25T12:00:00-04:00")
    return Sample(time=base + timedelta(minutes=minute), **kwargs)


class BusTests(unittest.TestCase):
    def test_passthrough_puts_nothing_on_the_bus(self) -> None:
        self.assertEqual(battery_bus_watts(32, 32, 0, 0), 0.0)

    def test_discharge_splits_inverter_input_from_the_husky(self) -> None:
        # 831.6 W AC at 90% is 924 W from the bus.
        self.assertAlmostEqual(battery_bus_watts(0, 831.6, 0, 0), -924.0)


class MeasureTests(unittest.TestCase):
    def _discharge(self) -> list[Sample]:
        # 2 h at 10 A from the Husky and 7.5 A from the Solix. Both fall 20 points.
        voltage = 52.8
        ac_out = 924.0 * INVERTER_EFF
        samples = []
        steps = 120
        for index in range(steps + 1):
            done = index / steps
            samples.append(
                _at(
                    index,
                    voltage_v=voltage,
                    current_a=-10.0,
                    husky_soc=80.0 - 20.0 * done,
                    solix_soc=80.0 - 20.0 * done,
                    ac_in_w=0.0,
                    ac_out_w=ac_out,
                    solar_w=0.0,
                    usb_w=0.0,
                )
            )
        return samples

    def test_continuous_discharge_recovers_the_solix_nameplate(self) -> None:
        report = analyze(self._discharge(), logged_load=False)
        self.assertIsNone(report.husky_ah)
        self.assertAlmostEqual(report.solix_ah or 0.0, 75.0, delta=0.5)
        self.assertIsNotNone(report.remaining)
        assert report.remaining is not None
        self.assertIsNone(report.remaining.husky_wh)
        solix_wh = 75.0 * NOMINAL_V * 0.60
        self.assertAlmostEqual(report.remaining.solix_wh or 0.0, solix_wh, delta=5.0)
        row = next(item for item in report.runtimes if item.load_w == 500)
        self.assertIsNone(row.combined_h)
        self.assertAlmostEqual(row.solix_h or 0.0, solix_wh * INVERTER_EFF / 500.0, delta=0.1)

    def test_a_low_inferred_solix_capacity_does_not_replace_the_nameplate(self) -> None:
        # 2 h at 10 A from the Husky and 4 A inferred from the Solix. SOC falls 20 points.
        voltage = 52.8
        ac_out = (10.0 + 4.0) * voltage * INVERTER_EFF
        samples = []
        steps = 120
        for index in range(steps + 1):
            done = index / steps
            samples.append(
                _at(
                    index,
                    voltage_v=voltage,
                    current_a=-10.0,
                    husky_soc=80.0 - 20.0 * done,
                    solix_soc=80.0 - 20.0 * done,
                    ac_in_w=0.0,
                    ac_out_w=ac_out,
                    solar_w=0.0,
                    usb_w=0.0,
                )
            )
        report = analyze(samples, logged_load=False)
        self.assertAlmostEqual(report.solix_ah or 0.0, 40.0, delta=1.0)
        self.assertIsNotNone(report.remaining)
        assert report.remaining is not None
        solix_wh = SOLIX_AH * NOMINAL_V * 0.60
        self.assertAlmostEqual(report.remaining.solix_wh or 0.0, solix_wh, delta=5.0)
        self.assertFalse(report.remaining.solix_from_log)
        text = render(report)
        self.assertIn("would be a 40 Ah pack", text)
        self.assertIn("75 Ah nameplate", text)

    def test_a_gap_does_not_become_amp_hours(self) -> None:
        early = _at(0, voltage_v=53.0, current_a=-10.0, husky_soc=80.0, solix_soc=80.0, ac_in_w=0.0, ac_out_w=500.0)
        late = _at(40, voltage_v=52.0, current_a=-10.0, husky_soc=60.0, solix_soc=60.0, ac_in_w=0.0, ac_out_w=500.0)
        report = analyze([early, late])
        self.assertIsNone(report.husky_ah)
        self.assertIsNone(report.solix_ah)
        self.assertIsNotNone(report.largest_gap)
        text = render(report)
        self.assertIn("75 Ah nameplate stands", text)
        self.assertIn("not a capacity measurement", text)
        self.assertIn("gap", text)
        self.assertIsNotNone(report.remaining)
        assert report.remaining is not None
        self.assertIsNone(report.remaining.husky_wh)

    def test_surface_charge_is_not_capacity(self) -> None:
        samples = [
            _at(0, voltage_v=55.0, current_a=16.0, husky_soc=99.0, solix_soc=100.0, ac_in_w=1200.0, ac_out_w=200.0),
            _at(1, voltage_v=55.0, current_a=16.0, husky_soc=99.0, solix_soc=100.0, ac_in_w=1200.0, ac_out_w=200.0),
        ]
        for minute in range(10, 70):
            voltage = 53.9 - (minute - 10) * 0.01
            samples.append(
                _at(
                    minute,
                    voltage_v=voltage,
                    current_a=0.0,
                    husky_soc=99.0,
                    solix_soc=100.0,
                    ac_in_w=30.0,
                    ac_out_w=30.0,
                    min_cell_v=3.33,
                    max_cell_v=3.34,
                )
            )
        report = analyze(samples)
        self.assertIsNone(report.husky_ah)
        self.assertIsNotNone(report.relaxation)
        text = render(report)
        self.assertIn("surface charge", text)
        self.assertIn("50.8 V", text)
        self.assertIn("finishing the charge", text)
        self.assertIn("No stretch in the log has the batteries carrying the AC load.", text)

    def test_husky_remaining_is_counted_from_a_rested_full_cell(self) -> None:
        # One rested minute, then 120 minutes at 10 A. Left Riemann skips the rest minute.
        samples = [
            _at(
                0,
                voltage_v=53.3,
                current_a=0.0,
                husky_soc=99.0,
                solix_soc=100.0,
                min_cell_v=3.34,
                max_cell_v=3.35,
                ac_in_w=30.0,
                ac_out_w=30.0,
            )
        ]
        for index in range(1, 122):
            done = (index - 1) / 120.0
            samples.append(
                _at(
                    index,
                    voltage_v=52.8,
                    current_a=-10.0,
                    husky_soc=99.0 - 10.0 * done,
                    solix_soc=100.0,
                    min_cell_v=3.32,
                    max_cell_v=3.33,
                    ac_in_w=0.0,
                    ac_out_w=500.0,
                )
            )
        report = analyze(samples, logged_load=False)
        self.assertIsNone(report.husky_ah)
        count = report.husky_count
        self.assertIsNotNone(count)
        assert count is not None
        self.assertAlmostEqual(count.amp_hours_out or 0.0, 20.0, delta=0.01)
        self.assertAlmostEqual(count.soc or 0.0, 80.0, delta=0.1)
        self.assertAlmostEqual(count.register_ah or 0.0, 200.0, delta=1.0)
        self.assertFalse(count.at_stop)
        self.assertIsNotNone(report.remaining)
        assert report.remaining is not None
        husky_wh = 100.0 * NOMINAL_V * 0.80
        self.assertAlmostEqual(report.remaining.husky_wh or 0.0, husky_wh, delta=5.0)
        text = render(report)
        self.assertIn("not used", text)
        self.assertIn("200 Ah", text)

    def test_a_cell_at_the_stop_zeros_husky_watt_hours(self) -> None:
        samples = [
            _at(
                0,
                voltage_v=53.3,
                current_a=0.0,
                husky_soc=99.0,
                solix_soc=100.0,
                min_cell_v=3.34,
                ac_in_w=30.0,
                ac_out_w=30.0,
            ),
            _at(
                1,
                voltage_v=50.7,
                current_a=-5.0,
                husky_soc=90.0,
                solix_soc=90.0,
                min_cell_v=3.170,
                ac_in_w=0.0,
                ac_out_w=400.0,
            ),
        ]
        report = analyze(samples, logged_load=False)
        count = report.husky_count
        self.assertIsNotNone(count)
        assert count is not None
        self.assertTrue(count.at_stop)
        self.assertGreater(count.soc or 0.0, 50.0)
        self.assertIsNotNone(report.remaining)
        assert report.remaining is not None
        self.assertEqual(report.remaining.husky_wh, 0.0)
        text = render(report)
        self.assertIn("at or below", text)
        self.assertIn("3.175", text)


class LogTests(unittest.TestCase):
    def test_error_lines_stay_out_of_the_solix_counter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "2026" / "10" / "25"
            root.mkdir(parents=True)
            path = root / "readings.ndjson"
            rows = [
                {"time": "2026-10-25T18:36:30-04:00", "device": "husky", "voltage_v": 54.0, "current_a": 9.0, "soc_percent": 95.0},
                {"time": "2026-10-25T18:36:30-04:00", "device": "f3800", "error": "no status frame"},
                {"time": "2026-10-25T18:40:00-04:00", "device": "husky", "voltage_v": 54.2, "current_a": 11.0, "soc_percent": 96.0, "min_cell_v": 3.38, "max_cell_v": 3.39},
                {"time": "2026-10-25T18:40:00-04:00", "device": "f3800", "soc_percent": 90, "ac_power_in_w": 1000, "ac_power_out_w": 200, "hours_remaining": 1.3},
                "not json",
            ]
            path.write_text("\n".join(json.dumps(row) if isinstance(row, dict) else row for row in rows) + "\n", encoding="utf-8")
            samples, skipped = load_logs(Path(directory))
        self.assertEqual(skipped, 1)
        self.assertEqual(len(samples), 2)
        self.assertIsNone(samples[0].solix_soc)
        self.assertEqual(samples[1].solix_soc, 90.0)
        self.assertEqual(samples[1].husky_soc, 96.0)

    def test_cli_prints_a_requested_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "2026" / "10" / "25"
            root.mkdir(parents=True)
            row = {
                "time": "2026-10-25T12:00:00-04:00",
                "device": "husky",
                "voltage_v": 53.3,
                "current_a": 0.0,
                "soc_percent": 40.0,
                "min_cell_v": 3.34,
            }
            solix = {
                "time": "2026-10-25T12:00:00-04:00",
                "device": "f3800",
                "soc_percent": 100,
                "ac_power_in_w": 30,
                "ac_power_out_w": 30,
                "hours_remaining": 100.0,
            }
            path = root / "readings.ndjson"
            path.write_text(json.dumps(row) + "\n" + json.dumps(solix) + "\n", encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--data-dir", directory, "--load", "420"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("420 W", text)
        self.assertIn("Combined", text)
        self.assertIn("not used", text)
