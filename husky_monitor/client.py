"""Scan for the Husky 2 radio and read its pack, cells, and temperatures.

The radio name and MAC come from ``HUSKY_BT_NAME`` and ``HUSKY_BT_ADDRESS``.
Its GATT profile is the Zeta Modbus service ``1006`` (notify ``1007``, write
``1008``). A Seplos ``ff00`` radio is still accepted if one is connected
instead. Nothing here writes module id, inverter protocol, or protection
settings.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from device_env import setting
from husky_monitor.protocol import (
    CMD_MANUFACTURER,
    CMD_TELEMETRY,
    FrameBuffer,
    FrameError,
    PackReading,
    build_ascii_telemetry,
    build_manufacturer_request,
    build_telemetry_request,
    manufacturer_text,
    parse_ascii_frame,
    parse_binary_frame,
    parse_telemetry_info,
)
from husky_monitor.zeta import (
    IDENTITY_COUNT,
    STATUS_COUNT,
    ModbusBuffer,
    build_identity_request,
    build_status_request,
    identity_text,
    parse_read_response,
    parse_status,
)

def husky_name() -> str:
    """Advertised Bluetooth name (``HUSKY_BT_NAME``)."""

    return setting("HUSKY_BT_NAME")


def husky_address() -> str:
    """Bluetooth MAC (``HUSKY_BT_ADDRESS``)."""

    return setting("HUSKY_BT_ADDRESS")


# Catalog values from the Husky 2 spec sheet. The BMS report is what we show;
# these are only used to flag a reading that does not look like this model.
HUSKY2_CELLS = 16
HUSKY2_RATED_AH = 100.0

_RESPONSE_TIMEOUT = 8.0
_CONNECT_TIMEOUT = 25.0


class MonitorError(RuntimeError):
    """The radio could not be found, or it did not return a readable pack."""


@dataclass
class SeenDevice:
    name: str
    address: str
    rssi: int | None
    services: tuple[str, ...] = ()


def device_matches(device: SeenDevice, name: str, address: str) -> bool:
    if address and _same_address(device.address, address):
        return True
    return bool(name and device.name and _same_name(device.name, name))


@dataclass
class ReadResult:
    name: str
    address: str
    reading: PackReading
    manufacturer: str = ""
    services: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)


def normalize_address(address: str) -> str:
    digits = "".join(char for char in address if char.isalnum())
    if len(digits) != 12 or any(char not in "0123456789abcdefABCDEF" for char in digits):
        raise MonitorError(f"{address!r} is not a Bluetooth address")
    digits = digits.upper()
    return ":".join(digits[index : index + 2] for index in range(0, 12, 2))


def _same_address(left: str, right: str) -> bool:
    try:
        return normalize_address(left) == normalize_address(right)
    except MonitorError:
        return False


def _same_name(left: str, right: str) -> bool:
    return left.casefold().strip() == right.casefold().strip()


def _short_uuid(uuid: str) -> str:
    text = uuid.lower()
    prefix = "0000"
    suffix = "-0000-1000-8000-00805f9b34fb"
    if text.startswith(prefix) and text.endswith(suffix):
        return text[4:8]
    return text


async def scan(timeout: float = 8.0) -> list[SeenDevice]:
    """Return nearby advertisements, strongest signal first."""
    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    seen: list[SeenDevice] = []
    for address, (device, adv) in discovered.items():
        name = device.name or adv.local_name or ""
        uuids = tuple(sorted({_short_uuid(uuid) for uuid in adv.service_uuids}))
        seen.append(SeenDevice(name, address.upper(), adv.rssi, uuids))
    seen.sort(key=lambda item: (-(item.rssi or -999), item.address))
    return seen


def _radio_hint(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return (
        f"{text}. The computer's Bluetooth radio may be off. "
        "Turn it on with `bluetoothctl power on`, and make sure the phone "
        "app is disconnected so this program can connect."
    )


async def _find(name: str, address: str, timeout: float) -> BLEDevice:
    wanted_address = normalize_address(address) if address else ""
    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:
        raise MonitorError(_radio_hint(exc)) from exc
    by_address: BLEDevice | None = None
    by_name: BLEDevice | None = None
    for device, adv in discovered.values():
        advertised = device.name or adv.local_name or ""
        if wanted_address and _same_address(device.address, wanted_address):
            by_address = device
            break
        if name and _same_name(advertised, name):
            by_name = device
    found = by_address or by_name
    if found is None:
        label = name or wanted_address or "the battery"
        raise MonitorError(
            f"did not see {label}. Turn the battery on, keep the phone app "
            "disconnected, and run `python -m husky_monitor scan`."
        )
    return found


def _channel(
    client: BleakClient,
) -> tuple[BleakGATTCharacteristic, BleakGATTCharacteristic]:
    """Pick the notify and write characteristics the Seplos radio uses."""
    notify: BleakGATTCharacteristic | None = None
    write: BleakGATTCharacteristic | None = None
    ranked: list[tuple[int, BleakGATTCharacteristic]] = []
    for service in client.services:
        service_uuid = _short_uuid(service.uuid)
        for char in service.characteristics:
            char_uuid = _short_uuid(char.uuid)
            rank = 0
            lowered = char.uuid.lower()
            if service_uuid == "1006" and char_uuid in {"1007", "1008"}:
                rank = 4
            elif service_uuid == "ff00" and char_uuid in {"ff01", "ff02"}:
                rank = 3
            elif service_uuid in {"ffe0", "ffe5"} and char_uuid in {"ffe1", "ffe4", "ffe9"}:
                rank = 2
            elif "6e400002" in lowered or "6e400003" in lowered:
                rank = 1
            if rank:
                ranked.append((rank, char))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for _rank, char in ranked:
        props = set(char.properties)
        if notify is None and props & {"notify", "indicate"}:
            notify = char
        if write is None and props & {"write", "write-without-response"}:
            write = char
    if notify is not None and write is None and "write" in set(notify.properties):
        write = notify
    if notify is None or write is None:
        raise MonitorError(
            "connected, but the radio has neither Zeta service 1006 "
            "nor a Seplos notify/write pair. "
            "Run `python -m husky_monitor inspect` and keep the service list."
        )
    return notify, write


def _dump_services(client: BleakClient) -> list[str]:
    lines: list[str] = []
    for service in client.services:
        lines.append(f"service {_short_uuid(service.uuid)}  {service.uuid}")
        for char in service.characteristics:
            props = ",".join(char.properties)
            lines.append(
                f"  char {_short_uuid(char.uuid)}  {char.uuid}  [{props}]"
            )
    return lines


async def _write(client: BleakClient, char: BleakGATTCharacteristic, payload: bytes) -> None:
    prefer_response = "write" in char.properties
    try:
        await client.write_gatt_char(char, payload, response=prefer_response)
    except BleakError:
        await client.write_gatt_char(char, payload, response=not prefer_response)


def _protocol_of(
    notify: BleakGATTCharacteristic,
    write: BleakGATTCharacteristic,
) -> str:
    shorts = {_short_uuid(notify.uuid), _short_uuid(write.uuid)}
    if "1007" in shorts or "1008" in shorts:
        return "zeta"
    return "seplos"


def _reading_from_frame(frame: bytes) -> PackReading | None:
    if frame[1:2] in b"0123456789abcdefABCDEF":
        address, status, info = parse_ascii_frame(frame)
        if status != 0:
            raise FrameError(f"BMS returned status 0x{status:02X}")
        return parse_telemetry_info(info, address, source="seplos-v2-ascii")
    address, function, status, info = parse_binary_frame(frame)
    if status != 0:
        raise FrameError(f"BMS returned status 0x{status:02X}")
    if function != CMD_TELEMETRY:
        return None
    return parse_telemetry_info(info, address, source="seplos-v2-binary")


def _manufacturer_from_frame(frame: bytes) -> str | None:
    if frame[1:2] in b"0123456789abcdefABCDEF":
        return None
    _address, function, status, info = parse_binary_frame(frame)
    if function != CMD_MANUFACTURER or status != 0:
        return None
    return manufacturer_text(info)


class BatteryLink:
    """One open Husky radio. Each poll writes the read-only status request."""

    def __init__(
        self,
        client: BleakClient,
        notify: BleakGATTCharacteristic,
        write: BleakGATTCharacteristic,
        kind: str,
        device: BLEDevice,
        services: list[str],
        pack: int,
        response_timeout: float,
        requested_name: str,
    ) -> None:
        self.client = client
        self.notify = notify
        self.write = write
        self.kind = kind
        self.device = device
        self.services = services
        self.pack = pack
        self.response_timeout = response_timeout
        self.requested_name = requested_name
        self.manufacturer = ""
        self.frames: list[bytes] = []
        self.ready = asyncio.Event()
        self.buffer: FrameBuffer | ModbusBuffer = (
            ModbusBuffer() if kind == "zeta" else FrameBuffer()
        )

    @property
    def connected(self) -> bool:
        return bool(self.client.is_connected)

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        found = self.buffer.feed(bytes(data))
        if found:
            self.frames.extend(found)
            self.ready.set()

    async def poll(self) -> ReadResult:
        """Ask for one fresh pack reading on the open radio."""

        if self.kind == "zeta":
            reading = await _request_zeta(
                self.client,
                self.write,
                self.buffer,
                self.frames,
                self.ready,
                self.response_timeout,
            )
        else:
            reading = await _request_telemetry(
                self.client,
                self.write,
                self.buffer,
                self.frames,
                self.ready,
                self.pack,
                self.response_timeout,
            )
        _note_model_mismatch(reading)
        return ReadResult(
            name=self.device.name or self.requested_name,
            address=self.device.address.upper(),
            reading=reading,
            manufacturer=self.manufacturer,
            services=self.services,
        )

    async def close(self) -> None:
        try:
            if self.client.is_connected:
                await self.client.stop_notify(self.notify)
        except BleakError:
            pass
        try:
            await self.client.disconnect()
        except BleakError:
            pass


def _radio_target(name: str | None, address: str | None) -> tuple[str, str]:
    return (
        husky_name() if name is None else name,
        husky_address() if address is None else address,
    )


async def connect_battery(
    name: str | None = None,
    address: str | None = None,
    pack: int = 0,
    scan_timeout: float = 8.0,
    response_timeout: float = _RESPONSE_TIMEOUT,
) -> BatteryLink:
    """Scan once, connect, and leave the radio open."""

    name, address = _radio_target(name, address)
    device = await _find(name, address, scan_timeout)
    client = BleakClient(device, timeout=_CONNECT_TIMEOUT)
    link: BatteryLink | None = None
    try:
        try:
            await client.connect()
        except (BleakError, TimeoutError, OSError) as exc:
            raise MonitorError(_radio_hint(exc)) from exc
        services = _dump_services(client)
        notify, write = _channel(client)
        kind = _protocol_of(notify, write)
        link = BatteryLink(
            client,
            notify,
            write,
            kind,
            device,
            services,
            pack,
            response_timeout,
            name,
        )
        await client.start_notify(notify, link._on_notify)
        if kind == "zeta":
            link.manufacturer = await _request_zeta_identity(
                client, write, link.buffer, link.frames, link.ready, response_timeout
            )
        else:
            link.manufacturer = await _request_manufacturer(
                client, write, link.buffer, link.frames, link.ready, response_timeout
            )
        return link
    except (BleakError, TimeoutError, OSError) as exc:
        await _drop_link(link, client)
        raise MonitorError(_radio_hint(exc)) from exc
    except Exception:
        await _drop_link(link, client)
        raise
    except BaseException:
        await _drop_link(link, client)
        raise


async def _drop_link(link: BatteryLink | None, client: BleakClient) -> None:
    if link is not None:
        await link.close()
        return
    try:
        await client.disconnect()
    except BleakError:
        pass


async def read_battery(
    name: str | None = None,
    address: str | None = None,
    pack: int = 0,
    scan_timeout: float = 8.0,
    response_timeout: float = _RESPONSE_TIMEOUT,
) -> ReadResult:
    """Connect to one battery, return one pack reading, and disconnect."""

    name, address = _radio_target(name, address)
    link = await connect_battery(
        name=name,
        address=address,
        pack=pack,
        scan_timeout=scan_timeout,
        response_timeout=response_timeout,
    )
    try:
        return await link.poll()
    finally:
        await link.close()


async def watch_battery(
    name: str | None = None,
    address: str | None = None,
    pack: int = 0,
    scan_timeout: float = 8.0,
    response_timeout: float = _RESPONSE_TIMEOUT,
    interval: float = 5.0,
) -> AsyncIterator[ReadResult]:
    """Hold one radio and yield a new status poll every ``interval`` seconds."""

    name, address = _radio_target(name, address)
    link = await connect_battery(
        name=name,
        address=address,
        pack=pack,
        scan_timeout=scan_timeout,
        response_timeout=response_timeout,
    )
    try:
        yield await link.poll()
        while True:
            await asyncio.sleep(interval)
            yield await link.poll()
    finally:
        await link.close()


async def _request_zeta(
    client: BleakClient,
    write: BleakGATTCharacteristic,
    buffer: ModbusBuffer,
    frames: list[bytes],
    ready: asyncio.Event,
    timeout: float,
) -> PackReading:
    ready.clear()
    frames.clear()
    buffer.clear()
    await _write(client, write, build_status_request())
    try:
        await asyncio.wait_for(ready.wait(), timeout)
    except TimeoutError as exc:
        raise MonitorError(
            "the radio did not return a Zeta status frame. "
            "Run `python -m husky_monitor inspect` to capture the raw reply."
        ) from exc
    for frame in frames:
        try:
            registers = parse_read_response(frame)
        except FrameError as exc:
            raise MonitorError(f"the radio answered, but {exc}") from exc
        if len(registers) >= STATUS_COUNT:
            return parse_status(registers)
    raise MonitorError(
        "the radio answered, but the frame was not a 41-register status block. "
        "Run `python -m husky_monitor inspect` to capture the raw reply."
    )


async def _request_zeta_identity(
    client: BleakClient,
    write: BleakGATTCharacteristic,
    buffer: ModbusBuffer,
    frames: list[bytes],
    ready: asyncio.Event,
    timeout: float,
) -> str:
    ready.clear()
    frames.clear()
    buffer.clear()
    try:
        await _write(client, write, build_identity_request())
        await asyncio.wait_for(ready.wait(), min(timeout, 3.0))
    except (BleakError, TimeoutError):
        return ""
    for frame in frames:
        try:
            registers = parse_read_response(frame)
        except FrameError:
            continue
        if len(registers) == IDENTITY_COUNT:
            return identity_text(registers)
    return ""


async def _request_telemetry(
    client: BleakClient,
    write: BleakGATTCharacteristic,
    buffer: FrameBuffer,
    frames: list[bytes],
    ready: asyncio.Event,
    pack: int,
    timeout: float,
) -> PackReading:
    for payload in (
        build_telemetry_request(pack=pack),
        build_ascii_telemetry(pack=pack),
    ):
        ready.clear()
        frames.clear()
        buffer.clear()
        await _write(client, write, payload)
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except TimeoutError:
            continue
        for frame in frames:
            try:
                reading = _reading_from_frame(frame)
            except FrameError as exc:
                raise MonitorError(f"the radio answered, but {exc}") from exc
            if reading is not None:
                return reading
    raise MonitorError(
        "the radio did not return a Seplos V2 telemetry frame. "
        "Run `python -m husky_monitor inspect` to capture the raw services."
    )


async def _request_manufacturer(
    client: BleakClient,
    write: BleakGATTCharacteristic,
    buffer: FrameBuffer,
    frames: list[bytes],
    ready: asyncio.Event,
    timeout: float,
) -> str:
    ready.clear()
    frames.clear()
    buffer.clear()
    try:
        await _write(client, write, build_manufacturer_request())
        await asyncio.wait_for(ready.wait(), min(timeout, 3.0))
    except (BleakError, TimeoutError):
        return ""
    for frame in frames:
        try:
            text = _manufacturer_from_frame(frame)
        except FrameError:
            continue
        if text:
            return text
    return ""


def _note_model_mismatch(reading: PackReading) -> None:
    if len(reading.cells) != HUSKY2_CELLS:
        reading.notes.append(
            f"this report has {len(reading.cells)} cells; a Husky 2 is {HUSKY2_CELLS}S"
        )
    if reading.rated_ah is not None and abs(reading.rated_ah - HUSKY2_RATED_AH) > 5:
        reading.notes.append(
            f"rated capacity is {reading.rated_ah:.0f} Ah; the Husky 2 catalog value is {HUSKY2_RATED_AH:.0f} Ah"
        )


def format_reading(result: ReadResult) -> str:
    reading = result.reading
    lines = [
        f"{result.name or husky_name() or 'Husky 2'}   {result.address}",
    ]
    if result.manufacturer:
        lines.append(f"BMS: {result.manufacturer}")
    lines.append(f"Source: {reading.source or 'unknown'}")
    lines.append("")
    lines.append(_measure("Pack voltage", reading.voltage_v, "V", 2))
    lines.append(_measure("Port voltage", reading.port_voltage_v, "V", 2))
    current = "n/a" if reading.current_a is None else f"{reading.current_a:+.2f} A"
    lines.append(f"Current:         {current}   ({reading.charge_state})")
    lines.append("  positive current means charging")
    soc = "n/a" if reading.soc_percent is None else f"{reading.soc_percent:.1f} %"
    lines.append(f"State of charge: {soc}")
    soh = "n/a" if reading.soh_percent is None else f"{reading.soh_percent:.1f} %"
    lines.append(f"State of health: {soh}")
    if reading.residual_ah is not None or reading.rated_ah is not None:
        residual = "n/a" if reading.residual_ah is None else f"{reading.residual_ah:.2f}"
        rated = "n/a" if reading.rated_ah is None else f"{reading.rated_ah:.2f}"
        lines.append(f"Capacity:        {residual} / {rated} Ah remaining / rated")
    if reading.cycles is not None:
        lines.append(f"Cycles:          {reading.cycles}")
    lines.append("")
    lines.append("Temperatures")
    if reading.probes:
        for probe in reading.probes:
            mark = "" if probe.plausible else "  (outside -40..120 C)"
            lines.append(f"  {probe.name:<16} {probe.celsius:6.1f} C{mark}")
    else:
        lines.append("  none reported")
    lines.append("")
    lines.append("Cells")
    for cell in reading.cells:
        lines.append(f"  {cell.index:>2}   {cell.volts:.3f} V")
    if reading.delta_cell_v is not None:
        lines.append(
            f"  min {reading.min_cell_v:.3f} V   max {reading.max_cell_v:.3f} V"
            f"   delta {reading.delta_cell_v * 1000:.0f} mV"
        )
    if reading.cell_sum_v is not None:
        lines.append(f"  sum of cells {reading.cell_sum_v:.2f} V")
    if reading.notes:
        lines.append("")
        lines.append("Notes")
        lines.extend(f"  {note}" for note in reading.notes)
    return "\n".join(lines)


def _measure(label: str, value: float | None, unit: str, digits: int) -> str:
    shown = "n/a" if value is None else f"{value:.{digits}f} {unit}"
    return f"{label + ':':<17}{shown}"


def result_to_json(result: ReadResult) -> dict[str, object]:
    reading = result.reading
    payload = asdict(reading)
    payload["cell_sum_v"] = reading.cell_sum_v
    payload["min_cell_v"] = reading.min_cell_v
    payload["max_cell_v"] = reading.max_cell_v
    payload["delta_cell_v"] = reading.delta_cell_v
    return {
        "name": result.name,
        "address": result.address,
        "manufacturer": result.manufacturer,
        "reading": payload,
    }
