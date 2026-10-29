"""Capacity estimates use continuous stretches and ignore surface charge."""

from __future__ import annotations

import io
import json
import subprocess
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
    estimate_env,
    load_logs,
    render,
    to_json,
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
        self.assertIn("45.0 Ah", text)
        self.assertIn("2304 Wh", text)
        self.assertIn("80.6 h", text)
        self.assertNotIn("logged", text)
        self.assertNotIn("would be a 40 Ah pack", text)
        self.assertNotIn("counted so far", text)
        env = estimate_env(report, generated_at=datetime.fromisoformat("2026-10-29T12:00:00-04:00"))
        self.assertIn("SOLIX_REMAINING_PERCENT=60", env)
        self.assertIn("HUSKY_REMAINING_PERCENT=\n", env)
        self.assertIn("SHARED_REMAINING_PERCENT=\n", env)
        notes = render(report, verbose=True)
        self.assertIn("would be a 40 Ah pack", notes)
        self.assertIn("75 Ah nameplate", notes)
        self.assertIn("counted so far, in the Solix", notes)

    def test_a_gap_does_not_become_amp_hours(self) -> None:
        early = _at(0, voltage_v=53.0, current_a=-10.0, husky_soc=80.0, solix_soc=80.0, ac_in_w=0.0, ac_out_w=500.0)
        late = _at(40, voltage_v=52.0, current_a=-10.0, husky_soc=60.0, solix_soc=60.0, ac_in_w=0.0, ac_out_w=500.0)
        report = analyze([early, late])
        self.assertIsNone(report.husky_ah)
        self.assertIsNone(report.solix_ah)
        self.assertIsNotNone(report.largest_gap)
        text = render(report)
        self.assertNotIn("gap", text)
        self.assertNotIn("nameplate stands", text)
        notes = render(report, verbose=True)
        self.assertIn("75 Ah nameplate stands", notes)
        self.assertIn("not a capacity measurement", notes)
        self.assertIn("gap", notes)
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
        self.assertNotIn("surface charge", text)
        self.assertNotIn("No stretch in the log", text)
        notes = render(report, verbose=True)
        self.assertIn("surface charge", notes)
        self.assertIn("50.8 V", notes)
        self.assertIn("finishing the charge", notes)
        self.assertIn("No stretch in the log has the batteries carrying the AC load.", notes)

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
        self.assertIn("80.0 Ah", text)
        self.assertIn("4096 Wh", text)
        self.assertIn("8960 Wh", text)
        self.assertIn("Shared runtime", text)
        self.assertIn("16.1 h", text)
        self.assertIn("14.3 h", text)
        self.assertNotIn("logged", text)
        env = estimate_env(report, generated_at=datetime.fromisoformat("2026-10-29T12:00:00-04:00"))
        self.assertIn('GENERATED_AT="2026-10-29T12:00:00-04:00"', env)
        self.assertNotIn("LOG_START", env)
        self.assertNotIn("LOG_END", env)
        self.assertIn("HUSKY_REMAINING_PERCENT=80", env)
        self.assertIn("SOLIX_REMAINING_PERCENT=100", env)
        self.assertIn("SHARED_REMAINING_PERCENT=88.6", env)
        self.assertIn("RUNTIME_3_W=500", env)
        self.assertIn("RUNTIME_3_FULL_H=16.1", env)
        self.assertIn("RUNTIME_3_REMAINING_H=14.3", env)
        self.assertNotIn("not used", text)
        self.assertNotIn("200 Ah", text)
        self.assertNotIn("Combined", text)
        notes = render(report, verbose=True)
        self.assertIn("not used", notes)
        self.assertIn("200 Ah", notes)
        self.assertIn("Total shared 175 Ah   8960 Wh", notes)
        payload = to_json(report)
        shared = payload["shared_pack_wh"]
        assert isinstance(shared, dict)
        self.assertEqual(shared["total"], 8960.0)
        remaining = payload["remaining_wh"]
        assert isinstance(remaining, dict)
        self.assertIsNotNone(remaining["total"])
        self.assertIsNotNone(remaining["fraction_of_shared"])
        self.assertLess(float(remaining["fraction_of_shared"]), 1.0)

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
        self.assertIn("0 Wh", text)
        self.assertNotIn("at or below", text)
        self.assertNotIn("3.175", text)
        notes = render(report, verbose=True)
        self.assertIn("at or below", notes)
        self.assertIn("3.175", notes)


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
            estimate = Path(directory) / "capacity-estimate.env"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--data-dir", directory, "--load", "420", "--estimate-file", str(estimate)])
            verbose = io.StringIO()
            with redirect_stdout(verbose):
                verbose_code = main(
                    ["--data-dir", directory, "--load", "420", "-v", "--estimate-file", str(estimate)]
                )
            saved = estimate.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("SHARED_REMAINING_PERCENT=100", saved)
        self.assertIn("RUNTIME_3_W=420", saved)
        self.assertIn("RUNTIME_3_REMAINING_H=", saved)
        text = buffer.getvalue()
        self.assertIn("420 W", text)
        self.assertIn("8960 Wh", text)
        self.assertIn("Shared runtime", text)
        self.assertIn("full", text)
        self.assertNotIn("logged", text)
        self.assertNotIn("not used", text)
        self.assertNotIn("Measured", text)
        self.assertNotIn("Combined", text)
        self.assertEqual(verbose_code, 0)
        notes = verbose.getvalue()
        self.assertIn("420 W", notes)
        self.assertIn("not used", notes)
        self.assertIn("Bus voltage", notes)

    def test_status_shows_remaining_from_the_estimate_file(self) -> None:
        script = Path(__file__).resolve().parents[1] / "status.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "2026" / "10" / "25"
            root.mkdir(parents=True)
            row = {
                "time": "2026-10-25T12:00:00-04:00",
                "device": "husky",
                "voltage_v": 53.3,
                "current_a": 0.0,
                "soc_percent": 48.0,
            }
            solix = {
                "time": "2026-10-25T12:00:00-04:00",
                "device": "f3800",
                "soc_percent": 13,
                "ac_power_out_w": 300,
                "hours_remaining": 3.8,
            }
            path = root / "readings.ndjson"
            path.write_text(json.dumps(row) + "\n" + json.dumps(solix) + "\n", encoding="utf-8")
            estimate = Path(directory) / "capacity-estimate.env"
            estimate.write_text(
                "\n".join(
                    [
                        'GENERATED_AT="2026-10-29T12:00:00-04:00"',
                        "SOLIX_REMAINING_PERCENT=13",
                        "SOLIX_REMAINING_WH=499",
                        "HUSKY_REMAINING_PERCENT=65.1",
                        "HUSKY_REMAINING_WH=3331",
                        "SHARED_REMAINING_PERCENT=42.7",
                        "SHARED_REMAINING_WH=3830",
                        "RUNTIME_COUNT=2",
                        "RUNTIME_1_W=100",
                        "RUNTIME_1_FULL_H=80.6",
                        "RUNTIME_1_REMAINING_H=34.5",
                        "RUNTIME_2_W=500",
                        "RUNTIME_2_REMAINING_H=6.9",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            shown = subprocess.run(
                [str(script), directory, str(estimate)],
                check=False,
                capture_output=True,
                text=True,
            )
            missing = subprocess.run(
                [str(script), directory, str(Path(directory) / "missing.env")],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("remaining        42.7%   3830 Wh", shown.stdout)
        self.assertNotIn("Solix remaining", shown.stdout)
        self.assertNotIn("Husky remaining", shown.stdout)
        self.assertNotIn("logs through", shown.stdout)
        self.assertIn("34.5 h", shown.stdout)
        self.assertIn("6.9 h", shown.stdout)
        self.assertNotIn("80.6", shown.stdout)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertIn("no estimate yet", missing.stdout)
        self.assertIn("Solix F3800", missing.stdout)
        self.assertNotIn("as of", shown.stdout)

    def test_status_hides_quiet_lines_unless_verbose(self) -> None:
        script = Path(__file__).resolve().parents[1] / "status.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "2026" / "10" / "25"
            root.mkdir(parents=True)
            solix = {
                "time": "2026-10-29T13:00:00-04:00",
                "device": "f3800",
                "name": "Anker SOLIX F3800",
                "soc_percent": 3,
                "ac_power_in_w": 0,
                "ac_power_out_w": 246,
                "charge_power_w": 0,
                "solar_power_w": 0,
                "discharge_power_w": 2,
                "charging": "inactive",
                "ac_output": "output",
                "dc_output": "not_connected",
                "hours_remaining": 0.9,
            }
            path = root / "readings.ndjson"
            path.write_text(json.dumps(solix) + "\n", encoding="utf-8")
            missing = str(Path(directory) / "missing.env")
            plain = subprocess.run(
                [str(script), directory, missing],
                check=False,
                capture_output=True,
                text=True,
            )
            detailed = subprocess.run(
                [str(script), "-v", directory, missing],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("AC out           246 W", plain.stdout)
        self.assertIn("discharge        2 W", plain.stdout)
        self.assertNotIn("as of", plain.stdout)
        self.assertNotIn("0 W", plain.stdout)
        self.assertNotIn("inactive", plain.stdout)
        self.assertNotIn("not_connected", plain.stdout)
        self.assertNotIn("AC output", plain.stdout)
        self.assertEqual(detailed.returncode, 0, detailed.stderr)
        self.assertIn("as of", detailed.stdout)
        self.assertIn("AC in            0 W", detailed.stdout)
        self.assertIn("charging         inactive", detailed.stdout)
        self.assertIn("AC output        output", detailed.stdout)
        self.assertIn("DC output        not_connected", detailed.stdout)
