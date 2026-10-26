"""Read-only Seplos BMS V2 frames.

The Husky 2 radio does not expose this service. It uses
the Zeta Modbus profile in ``husky_monitor.zeta``. These frames stay for a
radio that advertises service ``ff00``.

BigBattery's Husky 2 manual tells owners to use Zetarapower's Battery Monitor BL
app. That app reads pack voltage, current, state of charge, cell voltages, and
temperatures, and it can select a module id plus a CAN or RS485 inverter
protocol. Those are the same operations as the published Seplos / EMU1101
Bluetooth protocol (YD/T 1363.3, binary form on BLE):

* Service ``0000ff00``, notify ``ff01``, write ``ff02``
* ``0x61`` returns one pack, including every cell voltage and temperature
* ``0x51`` returns the manufacturer string
* ``0x63`` / ``0x64`` would switch CAN / RS485 protocols

This module builds and parses the read frames only. It does not build the
frames that change module id, inverter protocol, or protection settings.

The binary request below is the one captured from the Seplos BLE app and
checked against CRC-16/XMODEM:

    7E 10 00 46 61 00 01 00 F7 C1 0D

Open implementations of the same frames:

* https://github.com/patman15/aiobmsble (``seplos_v2_bms.py``)
* https://github.com/syssi/esphome-seplos-bms
* https://github.com/syssi/esphome-seplos-bms/issues/52
"""

from __future__ import annotations

from dataclasses import dataclass, field


SOI = 0x7E
EOI = 0x0D
CID1_BATTERY = 0x46
CMD_TELEMETRY = 0x61
CMD_MANUFACTURER = 0x51
CMD_ASCII_TELEMETRY = 0x42

# Seplos documents cell temperature as tenths of a Kelvin, offset 2731.
_KELVIN_TENTH_OFFSET = 2731
_IDLE_AMPS = 0.05
_MAX_INFO_LEN = 512
_MAX_CELLS = 24


class FrameError(ValueError):
    """A frame is too short, has a bad checksum, or is not telemetry."""


@dataclass
class Probe:
    name: str
    celsius: float
    plausible: bool


@dataclass
class Cell:
    index: int
    volts: float


@dataclass
class PackReading:
    """One decoded pack. Fields the BMS omitted stay None."""

    address: int
    cells: list[Cell]
    probes: list[Probe]
    voltage_v: float | None = None
    current_a: float | None = None
    soc_percent: float | None = None
    soh_percent: float | None = None
    residual_ah: float | None = None
    capacity_ah: float | None = None
    rated_ah: float | None = None
    cycles: int | None = None
    port_voltage_v: float | None = None
    charge_state: str = "unknown"
    source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def cell_sum_v(self) -> float | None:
        if not self.cells:
            return None
        return sum(cell.volts for cell in self.cells)

    @property
    def min_cell_v(self) -> float | None:
        live = [cell.volts for cell in self.cells if cell.volts > 0.5]
        return min(live) if live else None

    @property
    def max_cell_v(self) -> float | None:
        live = [cell.volts for cell in self.cells if cell.volts > 0.5]
        return max(live) if live else None

    @property
    def delta_cell_v(self) -> float | None:
        low = self.min_cell_v
        high = self.max_cell_v
        if low is None or high is None:
            return None
        return high - low


def crc_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM, the checksum on the binary BLE frames."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _ascii_length(info_chars: int) -> str:
    """YD/T 1363 length field: one checksum nibble plus a 12-bit length."""
    nibbles = (
        (info_chars >> 8) & 0xF,
        (info_chars >> 4) & 0xF,
        info_chars & 0xF,
    )
    checksum_nibble = ((~sum(nibbles)) + 1) & 0xF
    return f"{checksum_nibble:X}{info_chars:03X}"


def _ascii_checksum(body: str) -> str:
    return f"{((-sum(body.encode('ascii'))) & 0xFFFF):04X}"


def build_binary_command(cid2: int, address: int = 0, info: bytes = b"") -> bytes:
    """Build one read command. ``cid2`` must be a query, not a setting write."""
    if cid2 not in (CMD_TELEMETRY, CMD_MANUFACTURER):
        raise ValueError(f"refusing to build non-read command 0x{cid2:02X}")
    if not 0 <= address <= 0x0F:
        raise ValueError(f"module address out of range: {address}")
    frame = bytearray([SOI, 0x10, address, CID1_BATTERY, cid2])
    frame += len(info).to_bytes(2, "big")
    frame += info
    frame += crc_xmodem(frame[1:]).to_bytes(2, "big")
    frame.append(EOI)
    return bytes(frame)


def build_telemetry_request(address: int = 0, pack: int = 0) -> bytes:
    """Ask one module for its own pack telemetry (command 0x61)."""
    if not 0 <= pack <= 0x0F:
        raise ValueError(f"pack out of range: {pack}")
    return build_binary_command(CMD_TELEMETRY, address, bytes([pack]))


def build_manufacturer_request(address: int = 0) -> bytes:
    """Ask one module for its manufacturer string (command 0x51)."""
    return build_binary_command(CMD_MANUFACTURER, address, b"")


def build_ascii_telemetry(address: int = 0, pack: int = 0) -> bytes:
    """RS485-style telemetry query, used when the radio is a plain UART bridge.

    The published pack-0 request is ``~20004642E00200FD37\\r``.
    """
    if not 0 <= address <= 0x0F or not 0 <= pack <= 0x0F:
        raise ValueError("address or pack out of range")
    info = f"{pack:02X}"
    body = f"20{address:02X}46{CMD_ASCII_TELEMETRY:02X}{_ascii_length(len(info))}{info}"
    return f"~{body}{_ascii_checksum(body)}\r".encode("ascii")


def _charge_state(current_a: float | None) -> str:
    if current_a is None:
        return "unknown"
    if current_a > _IDLE_AMPS:
        return "charging"
    if current_a < -_IDLE_AMPS:
        return "discharging"
    return "idle"


def _probe_names(count: int) -> list[str]:
    """Seplos puts ambient and the MOSFET probe last when more than one exists."""
    if count <= 0:
        return []
    if count == 1:
        return ["Temperature"]
    names = [f"Cell probe {index}" for index in range(1, count - 1)]
    names.append("Ambient")
    names.append("MOSFET")
    return names


def _u16(info: bytes, offset: int, signed: bool = False) -> tuple[int | None, int]:
    if offset + 2 > len(info):
        return None, offset
    return int.from_bytes(info[offset : offset + 2], "big", signed=signed), offset + 2


def parse_telemetry_info(info: bytes, address: int = 0, source: str = "") -> PackReading:
    """Parse the INFO payload of a 0x61 or ASCII 0x42 telemetry reply.

    Layout, after the data-flag and command-group bytes:

    * cell count, then each cell voltage as big-endian millivolts
    * temperature count, then each temperature as big-endian tenths of a Kelvin
    * current (signed, 0.01 A, positive means charging), pack voltage (0.01 V),
      residual capacity (0.01 Ah)
    * one count byte, then capacity, state of charge (0.1 %), rated capacity,
      cycles, state of health (0.1 %), and port voltage
    """
    if len(info) < 3:
        raise FrameError("telemetry payload is too short")
    cell_count = info[2]
    if not 1 <= cell_count <= _MAX_CELLS:
        raise FrameError(f"cell count {cell_count} is outside 1..{_MAX_CELLS}")
    offset = 3
    need = offset + cell_count * 2
    if need > len(info):
        raise FrameError("telemetry payload ended inside the cell voltages")

    cells = []
    for index in range(cell_count):
        raw, offset = _u16(info, offset)
        cells.append(Cell(index + 1, (raw or 0) / 1000))

    if offset >= len(info):
        raise FrameError("telemetry payload has no temperature count")
    temp_count = info[offset]
    offset += 1
    if temp_count > 16 or offset + temp_count * 2 > len(info):
        raise FrameError("telemetry payload ended inside the temperatures")

    probes = []
    for name in _probe_names(temp_count):
        raw, offset = _u16(info, offset)
        celsius = ((raw or 0) - _KELVIN_TENTH_OFFSET) / 10
        probes.append(Probe(name, celsius, -40.0 <= celsius <= 120.0))

    current_raw, offset = _u16(info, offset, signed=True)
    voltage_raw, offset = _u16(info, offset)
    residual_raw, offset = _u16(info, offset)
    if offset < len(info):
        offset += 1  # count of the following fixed fields; not a byte skip
    capacity_raw, offset = _u16(info, offset)
    soc_raw, offset = _u16(info, offset)
    rated_raw, offset = _u16(info, offset)
    cycles_raw, offset = _u16(info, offset)
    soh_raw, offset = _u16(info, offset)
    port_raw, offset = _u16(info, offset)

    reading = PackReading(
        address=address,
        cells=cells,
        probes=probes,
        voltage_v=None if voltage_raw is None else voltage_raw / 100,
        current_a=None if current_raw is None else current_raw / 100,
        soc_percent=None if soc_raw is None else soc_raw / 10,
        soh_percent=None if soh_raw is None else soh_raw / 10,
        residual_ah=None if residual_raw is None else residual_raw / 100,
        capacity_ah=None if capacity_raw is None else capacity_raw / 100,
        rated_ah=None if rated_raw is None else rated_raw / 100,
        cycles=cycles_raw,
        port_voltage_v=None if port_raw is None else port_raw / 100,
        source=source,
    )
    reading.charge_state = _charge_state(reading.current_a)
    _add_sanity_notes(reading)
    return reading


def _add_sanity_notes(reading: PackReading) -> None:
    if reading.voltage_v is not None and reading.cell_sum_v is not None:
        if abs(reading.voltage_v - reading.cell_sum_v) > 1.5:
            reading.notes.append(
                "pack voltage and the sum of the cell voltages differ by more than 1.5 V"
            )
    if any(not probe.plausible for probe in reading.probes):
        reading.notes.append(
            "a temperature is outside -40..120 C, so the Kelvin scaling may not match this firmware"
        )
    if reading.soc_percent is not None and not 0 <= reading.soc_percent <= 100:
        reading.notes.append("state of charge is outside 0..100%")


def manufacturer_text(info: bytes) -> str:
    """Printable part of a 0x51 reply, usually a protocol name and a model."""
    text = info.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    cleaned = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(cleaned.split())


def parse_binary_frame(frame: bytes) -> tuple[int, int, int, bytes]:
    """Return address, function, status, and INFO from a CRC-checked binary frame."""
    if len(frame) < 10 or frame[0] != SOI or frame[-1] != EOI:
        raise FrameError("not a binary frame")
    info_len = int.from_bytes(frame[5:7], "big")
    if info_len > _MAX_INFO_LEN or len(frame) != 10 + info_len:
        raise FrameError("binary length does not match the frame")
    expected = int.from_bytes(frame[-3:-1], "big")
    if crc_xmodem(frame[1:-3]) != expected:
        raise FrameError("binary checksum does not match")
    return frame[2], frame[3], frame[4], bytes(frame[7:-3])


def parse_ascii_frame(frame: bytes) -> tuple[int, int, bytes]:
    """Return address, return-code, and INFO from a checksum-checked ASCII frame."""
    if len(frame) < 18 or frame[0] != ord("~") or frame[-1] != EOI:
        raise FrameError("not an ASCII frame")
    try:
        text = frame[1:-1].decode("ascii")
    except UnicodeError as exc:
        raise FrameError("ASCII frame is not text") from exc
    body, checksum = text[:-4], text[-4:]
    if _ascii_checksum(body).upper() != checksum.upper():
        raise FrameError("ASCII checksum does not match")
    if len(text) < 16:
        raise FrameError("ASCII frame is too short")
    address = int(text[2:4], 16)
    status = int(text[6:8], 16)
    length_field = text[8:12]
    info_chars = int(length_field[1:], 16)
    info_text = text[12 : 12 + info_chars]
    if len(info_text) != info_chars:
        raise FrameError("ASCII length does not match the frame")
    try:
        info = bytes.fromhex(info_text)
    except ValueError as exc:
        raise FrameError("ASCII info is not hex") from exc
    return address, status, info


class FrameBuffer:
    """Reassemble notifications, which BLE often splits into 20-byte chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.rejected = 0

    def clear(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 2:
                break
            if chr(self._buf[1]) in "0123456789abcdefABCDEF":
                end = self._buf.find(EOI, 1)
                if end < 0:
                    if len(self._buf) > 1200:
                        self.rejected += 1
                        del self._buf[0]
                        continue
                    break
                candidate = bytes(self._buf[: end + 1])
                del self._buf[: end + 1]
                try:
                    parse_ascii_frame(candidate)
                except FrameError:
                    self.rejected += 1
                    continue
                frames.append(candidate)
                continue
            if len(self._buf) < 7:
                break
            info_len = int.from_bytes(self._buf[5:7], "big")
            if info_len > _MAX_INFO_LEN:
                self.rejected += 1
                del self._buf[0]
                continue
            total = 10 + info_len
            if len(self._buf) < total:
                break
            candidate = bytes(self._buf[:total])
            try:
                parse_binary_frame(candidate)
            except FrameError:
                self.rejected += 1
                del self._buf[0]
                continue
            del self._buf[:total]
            frames.append(candidate)
        return frames
