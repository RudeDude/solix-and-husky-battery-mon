"""Command line for the Husky 2 Bluetooth monitor.

Examples:

    python -m husky_monitor scan
    python -m husky_monitor read
    python -m husky_monitor read --watch 5 --json
    python -m husky_monitor inspect
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from husky_monitor.client import (
    MonitorError,
    _protocol_of,
    device_matches,
    format_reading,
    husky_address,
    husky_name,
    read_battery,
    result_to_json,
    scan,
    watch_battery,
)
from husky_monitor.protocol import FrameBuffer, build_telemetry_request
from husky_monitor.zeta import ModbusBuffer, build_status_request, parse_read_response, parse_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="husky_monitor",
        description=(
            "Read voltage, state of charge, cell voltages, and temperatures "
            "from a BigBattery Husky 2 over Bluetooth."
        ),
    )
    parser.add_argument("--name", default=husky_name(), help="advertised Bluetooth name (HUSKY_BT_NAME)")
    parser.add_argument(
        "--address",
        default=husky_address(),
        help="Bluetooth MAC from HUSKY_BT_ADDRESS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_cmd = sub.add_parser("scan", help="list nearby Bluetooth radios")
    scan_cmd.add_argument("--timeout", type=float, default=8.0)

    read_cmd = sub.add_parser("read", help="print one pack reading")
    read_cmd.add_argument("--pack", type=int, default=0, help="pack index inside the module")
    read_cmd.add_argument("--timeout", type=float, default=8.0, help="scan time in seconds")
    read_cmd.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help=(
            "poll every N seconds on the same Bluetooth session. "
            "The radio stays connected until interrupted"
        ),
    )
    read_cmd.add_argument("--json", action="store_true", help="print JSON instead of text")

    inspect_cmd = sub.add_parser(
        "inspect",
        help="connect, list GATT services, and print raw replies",
    )
    inspect_cmd.add_argument("--timeout", type=float, default=8.0)
    inspect_cmd.add_argument(
        "--listen",
        type=float,
        default=8.0,
        help="seconds to collect notifications after the read command",
    )
    return parser


async def _scan(args: argparse.Namespace) -> int:
    devices = await scan(timeout=args.timeout)
    if not devices:
        print("No Bluetooth advertisements seen.")
        return 1
    print(f"{'RSSI':>5}  {'Address':<17}  {'Name':<24}  Services")
    for device in devices:
        mark = "*" if device_matches(device, args.name, args.address) else " "
        rssi = "" if device.rssi is None else str(device.rssi)
        services = ",".join(device.services)
        print(f"{mark}{rssi:>4}  {device.address:<17}  {device.name or '(no name)':<24}  {services}")
    found = any(device_matches(device, args.name, args.address) for device in devices)
    if not found:
        print(f"\n{args.name} ({args.address}) was not in this scan.")
        return 1
    return 0


def _print_result(args: argparse.Namespace, result: object) -> None:
    if args.json:
        print(json.dumps(result_to_json(result), indent=2), flush=True)
    else:
        print(format_reading(result), flush=True)


async def _read(args: argparse.Namespace) -> int:
    if args.watch <= 0:
        result = await read_battery(
            name=args.name,
            address=args.address,
            pack=args.pack,
            scan_timeout=args.timeout,
        )
        _print_result(args, result)
        return 0
    async for result in watch_battery(
        name=args.name,
        address=args.address,
        pack=args.pack,
        scan_timeout=args.timeout,
        interval=args.watch,
    ):
        _print_result(args, result)
        print("", flush=True)


async def _inspect(args: argparse.Namespace) -> int:
    """Print services and raw notifications without hiding a failed decode."""
    from bleak import BleakClient
    from bleak.exc import BleakError

    from husky_monitor.client import (
        _channel,
        _dump_services,
        _find,
        _radio_hint,
        _write,
    )

    device = await _find(args.name, args.address, args.timeout)
    print(f"Connecting to {device.name or '(no name)'} {device.address}")
    client = BleakClient(device, timeout=25.0)
    try:
        await client.connect()
    except (BleakError, TimeoutError, OSError) as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    try:
        for line in _dump_services(client):
            print(line)
        try:
            notify, write = _channel(client)
        except MonitorError as exc:
            print(exc)
            return 1
        kind = _protocol_of(notify, write)
        buffer = ModbusBuffer() if kind == "zeta" else FrameBuffer()
        print(f"\nNotify {_short(notify.uuid)}  write {_short(write.uuid)}  ({kind})")

        def on_notify(_char: object, data: bytearray) -> None:
            print(f"notify {bytes(data).hex()}")
            for frame in buffer.feed(bytes(data)):
                print(f"frame  {frame.hex()}")
                if kind == "zeta":
                    _print_zeta_frame(device.name or "", device.address, frame)

        await client.start_notify(notify, on_notify)
        payload = build_status_request() if kind == "zeta" else build_telemetry_request()
        print(f"write  {payload.hex()}")
        await _write(client, write, payload)
        await asyncio.sleep(args.listen)
        await client.stop_notify(notify)
    except (BleakError, TimeoutError, OSError) as exc:
        print(exc)
        return 1
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass
    if buffer.rejected:
        print(f"{buffer.rejected} notification chunk(s) were not a complete frame.")
    return 0


def _print_zeta_frame(name: str, address: str, frame: bytes) -> None:
    from husky_monitor.client import ReadResult
    from husky_monitor.zeta import STATUS_COUNT

    try:
        registers = parse_read_response(frame)
    except Exception as exc:
        print(f"parse  {exc}")
        return
    if len(registers) < STATUS_COUNT:
        print(f"parse  {len(registers)} registers")
        return
    print(format_reading(ReadResult(name, address.upper(), parse_status(registers))))


def _short(uuid: str) -> str:
    text = uuid.lower()
    if text.startswith("0000") and text.endswith("-0000-1000-8000-00805f9b34fb"):
        return text[4:8]
    return text


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan":
            return asyncio.run(_scan(args))
        if args.command == "read":
            return asyncio.run(_read(args))
        if args.command == "inspect":
            return asyncio.run(_inspect(args))
    except MonitorError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
