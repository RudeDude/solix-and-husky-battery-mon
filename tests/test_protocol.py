"""Checks against the published Seplos V2 request bytes and a synthetic reply."""

from __future__ import annotations

import unittest

from husky_monitor.protocol import (
    FrameBuffer,
    FrameError,
    build_ascii_telemetry,
    build_manufacturer_request,
    build_telemetry_request,
    crc_xmodem,
    parse_ascii_frame,
    parse_binary_frame,
    parse_telemetry_info,
)


def _info_for_husky() -> bytes:
    """16 cells at 3.300 V, four cell probes, ambient, MOSFET, 80% of 100 Ah."""
    info = bytearray([0x00, 0x00, 16])
    for _ in range(16):
        info += (3300).to_bytes(2, "big")
    temps = (
        2731 + 250,  # 25.0 C cell
        2731 + 255,  # 25.5 C cell
        2731 + 248,  # 24.8 C cell
        2731 + 252,  # 25.2 C cell
        2731 + 260,  # 26.0 C ambient
        2731 + 310,  # 31.0 C MOSFET
    )
    info.append(len(temps))
    for raw in temps:
        info += raw.to_bytes(2, "big")
    info += (-150).to_bytes(2, "big", signed=True)  # -1.50 A
    info += (5280).to_bytes(2, "big")  # 52.80 V
    info += (8000).to_bytes(2, "big")  # 80.00 Ah remaining
    info.append(10)
    info += (10000).to_bytes(2, "big")  # 100.00 Ah full
    info += (800).to_bytes(2, "big")  # 80.0 %
    info += (10000).to_bytes(2, "big")  # 100.00 Ah rated
    info += (12).to_bytes(2, "big")
    info += (1000).to_bytes(2, "big")  # 100.0 % SOH
    info += (5275).to_bytes(2, "big")  # 52.75 V at the posts
    return bytes(info)


def _wrap_binary(function: int, info: bytes, address: int = 0, version: int = 0x14) -> bytes:
    frame = bytearray([0x7E, version, address, function, 0x00])
    frame += len(info).to_bytes(2, "big")
    frame += info
    frame += crc_xmodem(frame[1:]).to_bytes(2, "big")
    frame.append(0x0D)
    return bytes(frame)


class CommandBytesTest(unittest.TestCase):
    def test_telemetry_request_matches_the_published_frame(self) -> None:
        self.assertEqual(
            build_telemetry_request().hex(),
            "7e10004661000100f7c10d",
        )

    def test_manufacturer_request_matches_the_published_frame(self) -> None:
        # Captured in syssi/esphome-seplos-bms issue 52.
        self.assertEqual(
            build_manufacturer_request().hex(),
            "7e1000465100003a7f0d",
        )

    def test_ascii_request_matches_the_published_rs485_query(self) -> None:
        self.assertEqual(build_ascii_telemetry(), b"~20004642E00200FD37\r")

    def test_setting_commands_are_not_built(self) -> None:
        from husky_monitor.protocol import build_binary_command

        with self.assertRaises(ValueError):
            build_binary_command(0x63)  # switch CAN protocol
        with self.assertRaises(ValueError):
            build_binary_command(0x49)  # write a parameter


class TelemetryParseTest(unittest.TestCase):
    def test_binary_reply_round_trip(self) -> None:
        info = _info_for_husky()
        frame = _wrap_binary(0x61, info)
        address, function, status, parsed = parse_binary_frame(frame)
        self.assertEqual((address, function, status), (0, 0x61, 0))
        reading = parse_telemetry_info(parsed, address, source="seplos-v2-binary")

        self.assertEqual(len(reading.cells), 16)
        self.assertAlmostEqual(reading.cells[0].volts, 3.3)
        self.assertAlmostEqual(reading.voltage_v or 0, 52.80)
        self.assertAlmostEqual(reading.current_a or 0, -1.50)
        self.assertEqual(reading.charge_state, "discharging")
        self.assertAlmostEqual(reading.soc_percent or 0, 80.0)
        self.assertAlmostEqual(reading.soh_percent or 0, 100.0)
        self.assertAlmostEqual(reading.residual_ah or 0, 80.0)
        self.assertAlmostEqual(reading.rated_ah or 0, 100.0)
        self.assertEqual(reading.cycles, 12)
        self.assertEqual(
            [probe.name for probe in reading.probes],
            [
                "Cell probe 1",
                "Cell probe 2",
                "Cell probe 3",
                "Cell probe 4",
                "Ambient",
                "MOSFET",
            ],
        )
        self.assertAlmostEqual(reading.probes[0].celsius, 25.0)
        self.assertAlmostEqual(reading.probes[-1].celsius, 31.0)
        self.assertAlmostEqual(reading.delta_cell_v or 0, 0.0)
        self.assertEqual(reading.notes, [])

    def test_notifications_can_arrive_in_20_byte_chunks(self) -> None:
        frame = _wrap_binary(0x61, _info_for_husky())
        buffer = FrameBuffer()
        found = []
        for start in range(0, len(frame), 20):
            found.extend(buffer.feed(frame[start : start + 20]))
        self.assertEqual(found, [frame])

    def test_bad_checksum_is_rejected(self) -> None:
        frame = bytearray(_wrap_binary(0x61, _info_for_husky()))
        frame[-4] ^= 0xFF
        with self.assertRaises(FrameError):
            parse_binary_frame(bytes(frame))
        self.assertEqual(FrameBuffer().feed(bytes(frame)), [])

    def test_ascii_reply_uses_the_same_payload(self) -> None:
        info = _info_for_husky()
        info_ascii = info.hex().upper()
        from husky_monitor.protocol import _ascii_checksum, _ascii_length

        body = f"20004600{_ascii_length(len(info_ascii))}{info_ascii}"
        frame = f"~{body}{_ascii_checksum(body)}\r".encode("ascii")
        address, status, parsed = parse_ascii_frame(frame)
        self.assertEqual((address, status), (0, 0))
        reading = parse_telemetry_info(parsed, source="seplos-v2-ascii")
        self.assertAlmostEqual(reading.soc_percent or 0, 80.0)
        self.assertEqual(reading.charge_state, "discharging")

    def test_idle_when_current_is_near_zero(self) -> None:
        info = bytearray(_info_for_husky())
        # Current sits just after the temperatures: flag, group, count, 16 cells, temp count, 6 temps.
        current_at = 3 + 16 * 2 + 1 + 6 * 2
        info[current_at : current_at + 2] = (0).to_bytes(2, "big", signed=True)
        reading = parse_telemetry_info(bytes(info))
        self.assertEqual(reading.charge_state, "idle")


if __name__ == "__main__":
    unittest.main()
