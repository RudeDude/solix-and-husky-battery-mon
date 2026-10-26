"""Command line for the Anker SOLIX F3800 monitor.

Examples:

    python -m anker_monitor scan
    python -m anker_monitor read
    python -m anker_monitor cloud
    python -m anker_monitor lan
    python -m anker_monitor inspect
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from anker_monitor.advert import anker_address, anker_host, anker_name, anker_serial
from anker_monitor.client import (
    MonitorError,
    format_result,
    inspect_station,
    read_station,
    result_to_json,
    scan,
    seen_matches,
    watch_station,
)
from anker_monitor.cloud import CloudError, cloud_to_json, format_cloud, read_cloud
from anker_monitor.lan import probe_lan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anker_monitor",
        description=(
            "Read state of charge, power, and temperature from an Anker "
            "SOLIX F3800. Output switches are not sent."
        ),
    )
    parser.add_argument("--name", default=anker_name(), help="advertised Bluetooth name (ANKER_BT_NAME)")
    parser.add_argument(
        "--address",
        default=anker_address(),
        help="label MAC from ANKER_BT_ADDRESS, or the advertisement identity",
    )
    parser.add_argument("--host", default=anker_host(), help="LAN address to probe (ANKER_HOST)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_cmd = sub.add_parser("scan", help="list nearby Bluetooth advertisements")
    scan_cmd.add_argument("--timeout", type=float, default=12.0)

    read_cmd = sub.add_parser("read", help="handshake and print one status frame")
    read_cmd.add_argument("--timeout", type=float, default=12.0, help="scan time in seconds")
    read_cmd.add_argument(
        "--wait",
        type=float,
        default=20.0,
        help="seconds to wait for a status frame after the handshake",
    )
    read_cmd.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help=(
            "print every N seconds on the same Bluetooth session. "
            "The radio stays connected until interrupted. 5 matches "
            "the station status interval"
        ),
    )
    read_cmd.add_argument("--json", action="store_true")

    lan_cmd = sub.add_parser("lan", help="probe the station IP and Modbus port")
    lan_cmd.add_argument("--timeout", type=float, default=2.0)

    cloud_cmd = sub.add_parser("cloud", help="read status from the Anker cloud")
    cloud_cmd.add_argument(
        "--wait",
        type=float,
        default=25.0,
        help="seconds to wait for a status frame after login",
    )
    cloud_cmd.add_argument(
        "--serial",
        default=anker_serial(),
        help="station serial (ANKER_SERIAL); empty uses the A1790 on the account",
    )
    cloud_cmd.add_argument("--json", action="store_true")

    inspect_cmd = sub.add_parser("inspect", help="list GATT services without writing")
    inspect_cmd.add_argument("--timeout", type=float, default=12.0)
    return parser


async def _scan(args: argparse.Namespace) -> int:
    devices = await scan(timeout=args.timeout)
    if not devices:
        print("No Bluetooth advertisements seen.")
        return 1
    found = False
    for device in devices:
        mark = "*" if seen_matches(device, args.name, args.address) else " "
        if mark == "*":
            found = True
        rssi = "" if device.rssi is None else f"{device.rssi:4d} "
        services = " ".join(device.services)
        identity = f" identity {device.identity_mac}" if device.identity_mac else ""
        print(f"{mark} {rssi}{device.address}  {device.name}{identity}  {services}".rstrip())
    if not found:
        print(
            f"\n{args.name} / {args.address} was not in the scan. "
            "Check ANKER_BT_NAME and ANKER_BT_ADDRESS in .env.",
            file=sys.stderr,
        )
        return 1
    return 0


def _emit(args: argparse.Namespace, result: object) -> None:
    if args.json:
        print(result_to_json(result))
    else:
        print(format_result(result))


def _emit_held(args: argparse.Namespace, result: object, *, held: bool) -> str:
    if args.json:
        text = result_to_json(result)
    else:
        text = format_result(result, held=held)
    print(text, flush=True)
    return text


async def _read(args: argparse.Namespace) -> int:
    if args.watch <= 0:
        result = await read_station(
            name=args.name,
            address=args.address,
            timeout=args.timeout,
            wait=args.wait,
            host=args.host,
        )
        _emit(args, result)
        return 0 if result.reading is not None else 1
    previous = None
    first = True
    async for result in watch_station(
        name=args.name,
        address=args.address,
        timeout=args.timeout,
        wait=args.wait,
        host=args.host,
        interval=args.watch,
    ):
        if not first and not args.json and result.reading is None:
            if format_result(result, held=True) == previous:
                continue
        if not first and not args.json:
            print(flush=True)
        previous = _emit_held(args, result, held=not first)
        first = False
    return 1


async def _inspect(args: argparse.Namespace) -> int:
    result = await inspect_station(
        name=args.name,
        address=args.address,
        timeout=args.timeout,
        host=args.host,
    )
    print(format_result(result))
    return 0


async def _cloud(args: argparse.Namespace) -> int:
    result = await read_cloud(wait=args.wait, serial=args.serial)
    if args.json:
        print(cloud_to_json(result))
    else:
        print(format_cloud(result))
    return 0 if result.reading is not None else 1


def _lan(args: argparse.Namespace) -> int:
    probe = probe_lan(args.host, timeout=args.timeout)
    print(probe.detail)
    return 0 if probe.reachable else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan":
            return asyncio.run(_scan(args))
        if args.command == "read":
            return asyncio.run(_read(args))
        if args.command == "inspect":
            return asyncio.run(_inspect(args))
        if args.command == "lan":
            return _lan(args)
        if args.command == "cloud":
            return asyncio.run(_cloud(args))
    except (MonitorError, CloudError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
