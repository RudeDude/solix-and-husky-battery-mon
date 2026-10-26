"""Checks against the published Zeta/Voltgo status request and a live register block."""

from __future__ import annotations

import unittest

from husky_monitor.zeta import (
    build_identity_request,
    build_read,
    build_status_request,
    identity_text,
    parse_read_response,
    parse_status,
    ModbusBuffer,
)


# Captured by lumberbarons/voltgo from an idle 8S 100 Ah Zeta pack.
_LIVE = [
    2683,
    0,
    3350, 3352, 3353, 3356, 3359, 3356, 3355, 3355,
    0, 0, 0, 0, 0, 0, 0, 0,
    27, 27, 27,
    99, 100, 100, 99,
    0, 0, 0, 0,
    2, 0,
    0x1575, 0x2A00, 0x1B1B,
    0, 0,
    8,
    1000,
    0, 0, 0,
]


def _response(registers: list[int]) -> bytes:
    body = bytearray([0x01, 0x03, len(registers) * 2])
    for value in registers:
        body += (value & 0xFFFF).to_bytes(2, "big")
    from husky_monitor.zeta import crc16_modbus

    crc = crc16_modbus(body)
    body.append(crc & 0xFF)
    body.append(crc >> 8)
    return bytes(body)


class ZetaRequestTest(unittest.TestCase):
    def test_status_request_matches_the_published_frame(self) -> None:
        self.assertEqual(build_status_request().hex(), "0103000000298414")

    def test_only_holding_register_reads_are_built(self) -> None:
        frame = build_read(0, 41)
        self.assertEqual(frame[1], 0x03)
        self.assertEqual(build_identity_request()[:2], bytes([0x01, 0x03]))


class ZetaParseTest(unittest.TestCase):
    def test_live_8s_block(self) -> None:
        frame = _response(_LIVE)
        self.assertEqual(parse_read_response(frame), _LIVE)
        reading = parse_status(_LIVE)
        self.assertEqual(reading.voltage_v, 26.83)
        self.assertEqual(reading.current_a, 0.0)
        self.assertEqual(reading.charge_state, "idle")
        self.assertEqual(len(reading.cells), 8)
        self.assertEqual(reading.cells[0].volts, 3.350)
        self.assertEqual(reading.cells[7].volts, 3.355)
        self.assertEqual([probe.celsius for probe in reading.probes], [27.0, 27.0, 27.0])
        self.assertEqual(reading.soc_percent, 99.0)
        self.assertEqual(reading.soh_percent, 100.0)
        self.assertEqual(reading.rated_ah, 100.0)

    def test_husky_16s_block(self) -> None:
        registers = [0] * 41
        registers[0] = 5280
        registers[1] = (-15) & 0xFFFF
        for index in range(16):
            registers[2 + index] = 3300
        registers[18:21] = [25, 26, 31]
        registers[21] = 80
        registers[22] = 100
        registers[36] = 16
        registers[37] = 1000
        reading = parse_status(parse_read_response(_response(registers)))
        self.assertEqual(len(reading.cells), 16)
        self.assertEqual(reading.voltage_v, 52.80)
        self.assertEqual(reading.current_a, -1.5)
        self.assertEqual(reading.charge_state, "discharging")
        self.assertEqual(reading.soc_percent, 80.0)
        self.assertEqual(reading.rated_ah, 100.0)
        self.assertEqual(reading.delta_cell_v, 0.0)
        self.assertEqual(reading.probes[2].name, "Temperature 3")

    def test_bad_checksum_is_rejected(self) -> None:
        frame = bytearray(_response(_LIVE))
        frame[-1] ^= 0xFF
        with self.assertRaises(Exception):
            parse_read_response(bytes(frame))
        self.assertEqual(ModbusBuffer().feed(bytes(frame)), [])

    def test_split_notification_is_reassembled(self) -> None:
        frame = _response(_LIVE)
        buffer = ModbusBuffer()
        self.assertEqual(buffer.feed(frame[:20]), [])
        self.assertEqual(buffer.feed(frame[20:]), [frame])

    def test_husky_capture_uses_the_soc_copy(self) -> None:
        # Live notify from BB-GOLF-BT1128. Registers 21-23 are 0, 24 is 99,
        # and the capacity register is 0.
        frame = bytes.fromhex(
            "01035214ca00000cfd0cfe0cfe0cfe0cfe0cfe0cff0cfe0cfe0cfe"
            "0cff0cfe0cfe0cfe0cfe0cfd001500000013000000000000006380"
            "000000000000000000000000000000131313130000001000000000"
            "00000010aaa2"
        )
        reading = parse_status(parse_read_response(frame))
        self.assertEqual(len(reading.cells), 16)
        self.assertEqual(reading.voltage_v, 53.22)
        self.assertEqual(reading.current_a, 0.0)
        self.assertEqual(reading.charge_state, "idle")
        self.assertEqual(reading.soc_percent, 99.0)
        self.assertIsNone(reading.soh_percent)
        self.assertIsNone(reading.rated_ah)
        self.assertEqual([probe.celsius for probe in reading.probes], [21.0, 0.0, 19.0])
        self.assertAlmostEqual(reading.cell_sum_v or 0, 53.22, places=2)

    def test_identity_strings(self) -> None:
        registers = [
            0x5443, 0x0000, 0x2D38, 0x5331, 0x3030, 0x2D56, 0x312E, 0x3000,
            0x0000, 0x0000, 0x0000, 0x0000, 0x5A30, 0x3154, 0x3230, 0x3230,
            0x3234, 0x2D30, 0x312D, 0x3131, 0x0000, 0x0000, 0x0000, 0x0000,
            0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
        ]
        self.assertEqual(identity_text(registers), "TC -8S100-V1.0 Z01T202024-01-11")
