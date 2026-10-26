"""The Husky watch holds one radio and polls it."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from husky_monitor.client import ReadResult, read_battery, watch_battery
from husky_monitor.protocol import Cell, PackReading, Probe


def _reading(current: float) -> ReadResult:
    return ReadResult(
        name="BB-GOLF-BT1128",
        address="01:02:03:04:05:06",
        reading=PackReading(
            address=1,
            cells=[Cell(1, 3.325)],
            probes=[Probe("Temperature 1", 21.0, True)],
            voltage_v=53.22,
            current_a=current,
            soc_percent=99.0,
            charge_state="idle",
            source="zeta-modbus",
        ),
    )


class _Link:
    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    @property
    def connected(self) -> bool:
        return not self.closed

    async def poll(self) -> ReadResult:
        self.calls += 1
        return _reading(0.0 if self.calls == 1 else 1.2)

    async def close(self) -> None:
        self.closed = True


class HuskyWatchTests(unittest.TestCase):
    def test_one_shot_closes_the_radio(self) -> None:
        link = _Link()

        async def opener(**_kwargs: object) -> _Link:
            return link

        async def run() -> None:
            with patch("husky_monitor.client.connect_battery", opener):
                result = await read_battery()
            self.assertEqual(result.reading.voltage_v, 53.22)
            self.assertTrue(link.closed)

        asyncio.run(run())

    def test_watch_polls_without_disconnecting(self) -> None:
        link = _Link()

        async def opener(**_kwargs: object) -> _Link:
            return link

        async def run() -> None:
            with patch("husky_monitor.client.connect_battery", opener):
                samples = watch_battery(interval=0.01)
                first = await anext(samples)
                self.assertFalse(link.closed)
                self.assertEqual(first.reading.current_a, 0.0)
                second = await anext(samples)
                self.assertFalse(link.closed)
                self.assertEqual(second.reading.current_a, 1.2)
                self.assertEqual(link.calls, 2)
                await samples.aclose()
            self.assertTrue(link.closed)

        asyncio.run(run())
