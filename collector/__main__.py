"""Hold both Bluetooth sessions and append one NDJSON sample per device.

The F3800 has to be advertising. Press its IoT button until the icon is on
the screen, and disconnect the phone app from both batteries. A silent
handshake is closed and the next cycle scans again. The collector prints
that button reminder while the station is down. It does not wait for a key.

    python -m collector
    python -m collector --interval 60
    python -m collector --interval 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from anker_monitor.advert import anker_address, anker_host, anker_name
from anker_monitor.client import MonitorError as AnkerError
from anker_monitor.client import _open_station, _wait_until_available
from collector.log import DayLog
from collector.records import anker_record, error_record, husky_record
from husky_monitor.client import BatteryLink, husky_address, husky_name
from husky_monitor.client import MonitorError as HuskyError
from husky_monitor.client import connect_battery


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description=(
            "Read the Husky 2 and the Solix F3800 on open Bluetooth sessions "
            "and append compact NDJSON. Output switches are not sent."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between samples. 60 is one minute, 300 is five minutes",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="root of the year/month/day log folders",
    )
    parser.add_argument("--husky-name", default=husky_name(), help="Husky Bluetooth name (HUSKY_BT_NAME)")
    parser.add_argument("--husky-address", default=husky_address(), help="Husky MAC (HUSKY_BT_ADDRESS)")
    parser.add_argument("--anker-name", default=anker_name(), help="F3800 Bluetooth name (ANKER_BT_NAME)")
    parser.add_argument("--anker-address", default=anker_address(), help="F3800 label MAC (ANKER_BT_ADDRESS)")
    parser.add_argument("--anker-host", default=anker_host(), help="F3800 LAN address (ANKER_HOST)")
    parser.add_argument("--husky-timeout", type=float, default=8.0, help="Husky scan time")
    parser.add_argument("--anker-timeout", type=float, default=30.0, help="F3800 scan time")
    parser.add_argument("--anker-wait", type=float, default=20.0, help="seconds to wait for the first F3800 frame")
    return parser


def _now() -> datetime:
    return datetime.now().astimezone()


def _outcome(row: dict[str, object]) -> str:
    if "error" not in row:
        return "ok"
    return "error: " + str(row["error"])


def _write(handle, record: dict[str, object]) -> None:
    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    handle.flush()


async def _sample_husky(link: BatteryLink | None, when: str) -> dict[str, object]:
    if link is None or not link.connected:
        return error_record("husky", when, "not connected")
    try:
        return husky_record(await link.poll(), when)
    except (HuskyError, TimeoutError, OSError) as exc:
        return error_record("husky", when, str(exc))


_IOT_BUTTON = (
    "Press the IoT button on the F3800 until the Bluetooth icon appears. "
    "A short press is enough. Holding the button more than 7 seconds clears "
    "the network pairing. Disconnect the phone app. The station does not "
    "advertise while it is on Wi-Fi."
)


def _short_anker_error(message: str) -> str:
    """One short log line for an F3800 failure."""

    text = message.casefold()
    if "no advertisement" in text or "not advertising" in text:
        return "not advertising"
    if "no status frame" in text:
        return "handshake finished, no status frame"
    if "did not finish the bluetooth handshake" in text or "handshake did not finish" in text:
        return "handshake did not finish"
    if "bluetooth radio may be off" in text or "radio is off" in text:
        return "computer bluetooth radio is off"
    if "solixble is not installed" in text:
        return "SolixBLE is not installed"
    return message.split(". ", 1)[0]


def _needs_iot_button(message: str) -> bool:
    """True when a short press of the station button can bring the radio back."""

    text = message.casefold()
    if "radio is off" in text or "solixble is not installed" in text:
        return False
    return any(
        phrase in text
        for phrase in (
            "not advertising",
            "no advertisement",
            "no status frame",
            "not connected",
            "handshake",
        )
    )


class _ButtonPrompt:
    """Print the IoT-button reminder, at most once per interval."""

    def __init__(self, every: float = 60.0) -> None:
        self.every = every
        self._last: float | None = None

    def maybe(self, reason: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if self._last is not None and moment - self._last < self.every:
            return False
        self._last = moment
        print(f"f3800: {reason}", file=sys.stderr)
        print(_IOT_BUTTON, file=sys.stderr, flush=True)
        return True


def _anker_ready(session: object | None) -> bool:
    """True when the station is connected and a status frame has arrived."""

    station = getattr(session, "station", None) if session is not None else None
    if station is None:
        return False
    return bool(getattr(station, "connected", False) and getattr(station, "available", False))


def _sample_anker(session: object | None, when: str) -> dict[str, object]:
    if not _anker_ready(session):
        station = getattr(session, "station", None) if session is not None else None
        connected = bool(station is not None and getattr(station, "connected", False))
        if connected:
            return error_record("f3800", when, "handshake finished, no status frame")
        return error_record("f3800", when, "not connected")
    record = anker_record(session.result(), when)
    if "error" in record:
        return error_record("f3800", when, _short_anker_error(str(record["error"])))
    return record


async def _open_husky(args: argparse.Namespace) -> BatteryLink | None:
    try:
        return await connect_battery(
            name=args.husky_name,
            address=args.husky_address,
            scan_timeout=args.husky_timeout,
        )
    except HuskyError as exc:
        print(f"husky: {exc}", file=sys.stderr)
        return None


async def _open_anker(args: argparse.Namespace) -> tuple[object | None, str]:
    """Scan, handshake, and keep the session only after a status frame.

    A connected station that sends no frame is closed so the next attempt
    scans again. The return message is empty when the session is usable.
    """

    try:
        session = await _open_station(
            name=args.anker_name,
            address=args.anker_address,
            timeout=args.anker_timeout,
            host=args.anker_host,
        )
    except AnkerError as exc:
        short = _short_anker_error(str(exc))
        if not _needs_iot_button(short):
            print(f"f3800: {exc}", file=sys.stderr)
        return None, short
    try:
        await _wait_until_available(session.station, args.anker_wait)
    except BaseException:
        await session.close()
        raise
    if not getattr(session.station, "available", False):
        await session.close()
        return None, "handshake finished, no status frame"
    return session, ""


async def _run(args: argparse.Namespace) -> int:
    if args.interval <= 0:
        print("interval must be positive", file=sys.stderr)
        return 2
    log = DayLog(Path(args.data_dir))
    logged: Path | None = None
    husky: BatteryLink | None = None
    anker = None
    prompt = _ButtonPrompt(every=max(args.interval, 60.0))
    try:
        while True:
            moment = _now()
            when = moment.isoformat(timespec="seconds")
            if husky is None or not husky.connected:
                if husky is not None:
                    await husky.close()
                husky = await _open_husky(args)
            husky_row = await _sample_husky(husky, when)
            trouble = ""
            if not _anker_ready(anker):
                if anker is not None:
                    await anker.close()
                    anker = None
                anker, trouble = await _open_anker(args)
            if trouble:
                anker_row = error_record("f3800", when, trouble)
            else:
                anker_row = _sample_anker(anker, when)
                if "error" in anker_row and anker is not None:
                    await anker.close()
                    anker = None
            if "error" in anker_row and _needs_iot_button(str(anker_row["error"])):
                prompt.maybe(str(anker_row["error"]))
            path = log.write(moment, husky_row)
            path = log.write(moment, anker_row)
            if path != logged:
                print(f"appending to {path}", file=sys.stderr, flush=True)
                logged = path
            print(
                f"{when}  husky {_outcome(husky_row)}"
                f"  f3800 {_outcome(anker_row)}",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(args.interval)
    finally:
        log.close()
        if husky is not None:
            await husky.close()
        if anker is not None:
            await anker.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
