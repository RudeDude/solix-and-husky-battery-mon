"""Find the F3800 and ask it for a status frame.

The read path is the F3800 class from SolixBLE. That class subscribes to
telemetry after the encryption handshake. It also knows how to switch the
AC and DC outputs; this module never calls those methods.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.device import BLEDevice

from anker_monitor.advert import (
    accepted_addresses,
    advertisement_matches,
    anker_address,
    anker_host,
    anker_name,
    normalize_address,
    parse_manufacturer,
)
from anker_monitor.lan import LanProbe, probe_lan
from anker_monitor.reading import StationReading, format_reading, reading_from_fields

_SCAN_TIMEOUT = 12.0
_TELEMETRY_WAIT = 20.0
_NEGOTIATION_LIMIT = 25.0
_COMPANY_ID = 0xFFFF


class MonitorError(RuntimeError):
    """The station could not be found, or it did not return a status frame."""


@dataclass
class SeenDevice:
    name: str
    address: str
    rssi: int | None
    services: tuple[str, ...] = ()
    identity_mac: str | None = None


@dataclass
class ReadResult:
    name: str
    address: str
    identity_mac: str | None
    negotiated: bool
    reading: StationReading | None
    lan: LanProbe
    detail: str = ""
    services: list[str] = field(default_factory=list)


def seen_matches(device: SeenDevice, name: str, address: str) -> bool:
    if advertisement_matches(
        name=device.name,
        address=device.address,
        manufacturer=None,
        wanted_name=name,
        wanted_address=address,
    ):
        return True
    if not device.identity_mac:
        return False
    return device.identity_mac in accepted_addresses(address)


def _short_uuid(uuid: str) -> str:
    text = uuid.lower()
    prefix = "0000"
    suffix = "-0000-1000-8000-00805f9b34fb"
    if text.startswith(prefix) and text.endswith(suffix):
        return text[4:8]
    return text


def _radio_hint(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return (
        f"{text}. The computer's Bluetooth radio may be off. "
        "Turn it on with `bluetoothctl power on`, and disconnect the Anker "
        "app so this program can connect."
    )


async def scan(timeout: float = _SCAN_TIMEOUT) -> list[SeenDevice]:
    """Return nearby advertisements, strongest signal first."""

    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    seen: list[SeenDevice] = []
    for address, (device, adv) in discovered.items():
        name = device.name or adv.local_name or ""
        record = adv.manufacturer_data.get(_COMPANY_ID)
        identity = parse_manufacturer(record) if record else None
        uuids = tuple(sorted({_short_uuid(uuid) for uuid in adv.service_uuids}))
        seen.append(
            SeenDevice(
                name=name,
                address=address.upper(),
                rssi=adv.rssi,
                services=uuids,
                identity_mac=None if identity is None else identity.mac,
            )
        )
    seen.sort(key=lambda item: (-(item.rssi or -999), item.address))
    return seen


async def _find(name: str, address: str, timeout: float) -> tuple[BLEDevice, str | None]:
    wanted = normalize_address(address) if address else ""
    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    matches: list[tuple[BLEDevice, str | None, int | None]] = []
    for _address, (device, adv) in discovered.items():
        local_name = device.name or adv.local_name or ""
        record = adv.manufacturer_data.get(_COMPANY_ID)
        identity = parse_manufacturer(record) if record else None
        seen = SeenDevice(
            name=local_name,
            address=device.address,
            rssi=adv.rssi,
            identity_mac=None if identity is None else identity.mac,
        )
        if seen_matches(seen, name, wanted):
            matches.append((device, seen.identity_mac, adv.rssi))
    if not matches:
        raise MonitorError(
            f"no advertisement matched name {name!r} or address {address}. "
            "Set ANKER_BT_NAME and ANKER_BT_ADDRESS in .env. Disconnect the "
            "Anker app and make sure the station is advertising."
        )
    device, identity, _rssi = max(matches, key=lambda item: item[2] or -999)
    return device, identity


def _fields_from_station(station: object) -> dict[str, object]:
    names = {
        "soc_percent": "battery_percentage",
        "soc_other_percent": "battery_percentage_aggregate",
        "temperature_c": "temperature",
        "ac_power_in_w": "ac_power_in",
        "ac_power_out_w": "ac_power_out",
        "solar_power_w": "solar_power_in",
        "solar_pv1_w": "solar_pv_1_power_in",
        "solar_pv2_w": "solar_pv_2_power_in",
        "charge_power_w": "battery_charge_power",
        "output_power_w": "power_out",
        "discharge_power_w": "battery_discharge_power",
        "usb_c1_w": "usb_c1_power",
        "usb_c2_w": "usb_c2_power",
        "usb_c3_w": "usb_c3_power",
        "usb_a1_w": "usb_a1_power",
        "usb_a2_w": "usb_a2_power",
        "ac_output": "ac_output",
        "dc_output": "dc_output",
        "charging": "charging_status",
        "hours_remaining": "time_remaining",
        "serial_number": "serial_number",
    }
    fields: dict[str, object] = {}
    for key, attr in names.items():
        try:
            fields[key] = getattr(station, attr)
        except Exception:
            fields[key] = None
    return fields


def _no_telemetry_detail() -> str:
    return (
        "The encryption handshake finished, and the station sent no status "
        "frame. This firmware answers the Bluetooth session and does not "
        "publish charge or power on it. Bluetooth and Wi-Fi are not used "
        "together. Join the station to Wi-Fi in the Anker app, then run "
        "`python -m anker_monitor cloud`. There is no local F3800 register map."
    )


def _link_down_detail() -> str:
    return (
        "The Bluetooth link dropped. This session stays open and uses the "
        "radio again if the station answers."
    )


@dataclass
class _LiveSession:
    """One Bluetooth session. Closing it is the only disconnect."""

    station: object
    name: str
    address: str
    identity_mac: str | None
    lan: LanProbe
    timeout_module: object
    previous_timeout: object

    def result(self) -> ReadResult:
        connected = bool(getattr(self.station, "connected", False))
        reading = None
        detail = ""
        if connected and getattr(self.station, "available", False):
            reading = reading_from_fields(_fields_from_station(self.station))
        elif connected:
            detail = _no_telemetry_detail()
        else:
            detail = _link_down_detail()
        return ReadResult(
            name=self.name,
            address=self.address,
            identity_mac=self.identity_mac,
            negotiated=connected,
            reading=reading,
            lan=self.lan,
            detail=detail,
        )

    async def close(self) -> None:
        if self.previous_timeout is not None:
            self.timeout_module.NEGOTIATION_TIMEOUT = self.previous_timeout
        try:
            await self.station.disconnect()
        except Exception:
            pass


async def _wait_until_available(station: object, seconds: float) -> None:
    if seconds <= 0:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if getattr(station, "available", False):
            return
        await asyncio.sleep(min(0.5, max(deadline - loop.time(), 0)))


async def _open_station(
    *,
    name: str,
    address: str,
    timeout: float,
    host: str,
) -> _LiveSession:
    """Scan once, handshake once, and leave the radio connected."""

    device, identity = await _find(name, address, timeout)
    lan = probe_lan(host)
    try:
        from SolixBLE import F3800
        import SolixBLE.device as device_mod
    except ImportError as exc:
        raise MonitorError(
            "SolixBLE is not installed. Run pip install -r requirements.txt."
        ) from exc

    previous = getattr(device_mod, "NEGOTIATION_TIMEOUT", None)
    device_mod.NEGOTIATION_TIMEOUT = _NEGOTIATION_LIMIT
    station = F3800(device)
    try:
        try:
            negotiated = await station.connect(max_attempts=1)
        except BleakError as exc:
            raise MonitorError(_radio_hint(exc)) from exc
        if not negotiated:
            raise MonitorError("the station did not finish the Bluetooth handshake")
    except Exception:
        if previous is not None:
            device_mod.NEGOTIATION_TIMEOUT = previous
        try:
            await station.disconnect()
        except Exception:
            pass
        raise
    return _LiveSession(
        station=station,
        name=device.name or name,
        address=device.address.upper(),
        identity_mac=identity,
        lan=lan,
        timeout_module=device_mod,
        previous_timeout=previous,
    )


def _station_target(
    name: str | None,
    address: str | None,
    host: str | None,
) -> tuple[str, str, str]:
    return (
        anker_name() if name is None else name,
        anker_address() if address is None else address,
        anker_host() if host is None else host,
    )


async def read_station(
    *,
    name: str | None = None,
    address: str | None = None,
    timeout: float = _SCAN_TIMEOUT,
    wait: float = _TELEMETRY_WAIT,
    host: str | None = None,
) -> ReadResult:
    """Connect, subscribe to telemetry, print one frame, and disconnect."""

    name, address, host = _station_target(name, address, host)
    session = await _open_station(name=name, address=address, timeout=timeout, host=host)
    try:
        await _wait_until_available(session.station, wait)
        return session.result()
    finally:
        await session.close()


async def watch_station(
    *,
    name: str | None = None,
    address: str | None = None,
    timeout: float = _SCAN_TIMEOUT,
    wait: float = _TELEMETRY_WAIT,
    host: str | None = None,
    interval: float,
) -> AsyncIterator[ReadResult]:
    """Hold one Bluetooth session and yield a reading every ``interval`` seconds.

    The radio stays connected until the caller closes this generator.
    """

    name, address, host = _station_target(name, address, host)
    session = await _open_station(name=name, address=address, timeout=timeout, host=host)
    try:
        await _wait_until_available(session.station, wait)
        yield session.result()
        while True:
            await asyncio.sleep(interval)
            yield session.result()
    finally:
        await session.close()


async def inspect_station(
    *,
    name: str | None = None,
    address: str | None = None,
    timeout: float = _SCAN_TIMEOUT,
    host: str | None = None,
) -> ReadResult:
    """Connect and list GATT services. Nothing is written."""

    name, address, host = _station_target(name, address, host)
    device, identity = await _find(name, address, timeout)
    services: list[str] = []
    client = BleakClient(device, timeout=25)
    try:
        await client.connect()
        for service in client.services:
            services.append(f"service {service.uuid}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                services.append(f"  char {char.uuid} [{props}]")
                if "read" in char.properties:
                    try:
                        value = bytes(await client.read_gatt_char(char))
                    except BleakError as exc:
                        services.append(f"    read failed: {exc}")
                    else:
                        services.append(f"    value {value.hex()}")
    except BleakError as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    finally:
        if client.is_connected:
            await client.disconnect()
    return ReadResult(
        name=device.name or name,
        address=device.address.upper(),
        identity_mac=identity,
        negotiated=False,
        reading=None,
        lan=probe_lan(host),
        services=services,
    )


def format_result(result: ReadResult, *, held: bool = False) -> str:
    """Format one sample. A held session prints only the new reading."""

    if held:
        if result.reading is not None:
            return format_reading(result.reading)
        return result.detail
    lines = [
        f"{result.name}  {result.address}",
        f"identity: {result.identity_mac or 'not in the advertisement'}",
    ]
    if result.reading is not None:
        lines.append(format_reading(result.reading))
    elif result.detail:
        lines.append(result.detail)
    if result.services:
        lines.extend(result.services)
    lines.append(result.lan.detail)
    return "\n".join(lines)


def result_to_json(result: ReadResult) -> str:
    payload = {
        "name": result.name,
        "address": result.address,
        "identity_mac": result.identity_mac,
        "negotiated": result.negotiated,
        "reading": None if result.reading is None else asdict(result.reading),
        "detail": result.detail,
        "lan": {
            "host": result.lan.host,
            "reachable": result.lan.reachable,
            "modbus_open": result.lan.modbus_open,
            "detail": result.lan.detail,
        },
        "services": result.services,
    }
    return json.dumps(payload, indent=2)
