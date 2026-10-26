"""Read-only Zeta / Voltgo Modbus frames.

The Husky 2 radio exposes this profile, not the Seplos ``ff00`` service.
Its name and MAC are ``HUSKY_BT_NAME`` and ``HUSKY_BT_ADDRESS`` in ``.env``.

* service ``00001006``, notify ``1007``, write ``1008``
* service ``0000fa00`` is present and is left alone (it is not the data channel)

The same UUIDs, Telink OUI ``A4:C1:37``, and Modbus map are documented for
Zeta batteries that the Voltgo app reads:

https://github.com/lumberbarons/voltgo/blob/main/PROTOCOL.md

A status poll is one Modbus read of 41 holding registers:

    01 03 00 00 00 29 84 14

This module builds that read and the identity read. It does not build
write-multiple-registers frames.
"""

from __future__ import annotations

from husky_monitor.protocol import Cell, FrameError, PackReading, Probe

SLAVE = 0x01
FUNC_READ_HOLDING = 0x03
STATUS_COUNT = 41
IDENTITY_START = 105
IDENTITY_COUNT = 32

_REG_VOLTAGE = 0
_REG_CURRENT = 1
_REG_CELL = 2
_REG_TEMP = 18
_REG_SOC = 21
_REG_SOH = 22
_REG_CELLS = 36
_REG_CAPACITY = 37
_CELL_SLOTS = 16
_TEMP_SLOTS = 3
_IDLE_AMPS = 0.05
_MAX_BYTE_COUNT = 250


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS, low byte first on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_read(start: int, count: int, slave: int = SLAVE) -> bytes:
    """Build a read-holding-registers request. Function code is fixed at 0x03."""
    if not 0 <= slave <= 0xFF:
        raise ValueError(f"slave address out of range: {slave}")
    if not 0 <= start <= 0xFFFF or not 1 <= count <= 125:
        raise ValueError(f"register range out of range: start {start} count {count}")
    frame = bytearray(
        [
            slave,
            FUNC_READ_HOLDING,
            (start >> 8) & 0xFF,
            start & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    crc = crc16_modbus(frame)
    frame.append(crc & 0xFF)
    frame.append(crc >> 8)
    return bytes(frame)


def build_status_request() -> bytes:
    """Read the 41-register status block the Voltgo app polls."""
    return build_read(0, STATUS_COUNT)


def build_identity_request() -> bytes:
    """Read the ASCII model and date block at holding register 105."""
    return build_read(IDENTITY_START, IDENTITY_COUNT)


def parse_read_response(frame: bytes, slave: int = SLAVE) -> list[int]:
    """Return big-endian holding registers from one CRC-checked response."""
    if len(frame) < 5:
        raise FrameError("modbus frame is too short")
    expected = crc16_modbus(frame[:-2])
    actual = frame[-2] | (frame[-1] << 8)
    if expected != actual:
        raise FrameError("modbus checksum does not match")
    if frame[0] != slave:
        raise FrameError(f"modbus slave 0x{frame[0]:02X} is not 0x{slave:02X}")
    if frame[1] & 0x80:
        code = frame[2] if len(frame) > 2 else 0
        raise FrameError(f"modbus exception 0x{code:02X} for function 0x{frame[1] & 0x7F:02X}")
    if frame[1] != FUNC_READ_HOLDING:
        raise FrameError(f"modbus function 0x{frame[1]:02X} is not a holding-register read")
    byte_count = frame[2]
    if byte_count % 2 or len(frame) != 3 + byte_count + 2:
        raise FrameError("modbus byte count does not match the frame")
    return [
        int.from_bytes(frame[3 + index : 5 + index], "big")
        for index in range(0, byte_count, 2)
    ]


def parse_status(registers: list[int]) -> PackReading:
    """Decode registers 0-40 into pack voltage, current, cells, and temperatures.

    The map was verified on 8S Zeta packs. This radio uses the same 16 cell
    slots, so a 16S Husky 2 fills every slot. Temperatures are three pack
    sensors, not one sensor on each cell.
    """
    if len(registers) < _REG_CAPACITY + 1:
        raise FrameError(
            f"status block has {len(registers)} registers, need {_REG_CAPACITY + 1}"
        )
    cell_count = registers[_REG_CELLS]
    if not 1 <= cell_count <= _CELL_SLOTS:
        raise FrameError(f"cell count {cell_count} is outside 1..{_CELL_SLOTS}")

    cells = [
        Cell(index + 1, registers[_REG_CELL + index] / 1000)
        for index in range(cell_count)
    ]
    probes = []
    for index in range(_TEMP_SLOTS):
        raw = registers[_REG_TEMP + index]
        if raw >= 0x8000:
            raw -= 0x10000
        celsius = float(raw)
        probes.append(Probe(f"Temperature {index + 1}", celsius, -40.0 <= celsius <= 120.0))

    current_raw = registers[_REG_CURRENT]
    if current_raw >= 0x8000:
        current_raw -= 0x10000
    current_a = current_raw / 10
    if current_a > _IDLE_AMPS:
        charge_state = "charging"
    elif current_a < -_IDLE_AMPS:
        charge_state = "discharging"
    else:
        charge_state = "idle"

    # 8S Zeta packs store SOC in register 21 and a copy in register 24.
    # This Husky leaves 21-23 at zero and puts the percentage in register 24.
    soc = _percent_register(registers, (_REG_SOC, 24))
    soh = _percent_register(registers, (_REG_SOH, 23), allow_zero=False)
    capacity = registers[_REG_CAPACITY] / 10
    if capacity <= 0:
        capacity = None

    reading = PackReading(
        address=SLAVE,
        cells=cells,
        probes=probes,
        voltage_v=registers[_REG_VOLTAGE] / 100,
        current_a=current_a,
        soc_percent=None if soc is None else float(soc),
        soh_percent=None if soh is None else float(soh),
        capacity_ah=capacity,
        rated_ah=capacity,
        charge_state=charge_state,
        source="zeta-modbus",
    )
    if reading.voltage_v is not None and reading.cell_sum_v is not None:
        if abs(reading.voltage_v - reading.cell_sum_v) > 1.5:
            reading.notes.append(
                "pack voltage and the sum of the cell voltages differ by more than 1.5 V"
            )
    if any(not probe.plausible for probe in reading.probes):
        reading.notes.append("a temperature is outside -40..120 C")
    if reading.soc_percent is not None and not 0 <= reading.soc_percent <= 100:
        reading.notes.append("state of charge is outside 0..100%")
    if len(reading.probes) < len(reading.cells):
        reading.notes.append(
            "this radio reports a few pack temperature sensors, not one sensor on every cell"
        )
    return reading


def _percent_register(
    registers: list[int],
    indexes: tuple[int, ...],
    allow_zero: bool = True,
) -> int | None:
    """Return the first register that looks like a percentage.

    A zero is skipped when a later register holds 1..100, because this Husky
    leaves the primary SOC register blank and fills the copy instead.
    """
    values = []
    for index in indexes:
        value = registers[index]
        if 0 <= value <= 100:
            values.append(value)
    for value in values:
        if value != 0:
            return value
    if allow_zero and values:
        return 0
    return None


def identity_text(registers: list[int]) -> str:
    """Join the printable ASCII fields in the device-info register block."""
    raw = bytearray()
    for value in registers:
        raw.append((value >> 8) & 0xFF)
        raw.append(value & 0xFF)
    fields = []
    for part in raw.split(b"\x00"):
        text = part.decode("ascii", errors="ignore").strip()
        if len(text) > 1 and all(0x20 <= ord(char) <= 0x7E for char in text):
            fields.append(text)
    return " ".join(fields)


class ModbusBuffer:
    """Reassemble notify chunks into CRC-checked Modbus RTU frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.rejected = 0

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while len(self._buffer) >= 5:
            if self._buffer[0] != SLAVE:
                del self._buffer[0]
                self.rejected += 1
                continue
            function = self._buffer[1]
            if function == FUNC_READ_HOLDING | 0x80:
                need = 5
            elif function == FUNC_READ_HOLDING:
                byte_count = self._buffer[2]
                if byte_count > _MAX_BYTE_COUNT or byte_count % 2:
                    del self._buffer[0]
                    self.rejected += 1
                    continue
                need = 3 + byte_count + 2
            else:
                del self._buffer[0]
                self.rejected += 1
                continue
            if len(self._buffer) < need:
                break
            frame = bytes(self._buffer[:need])
            expected = crc16_modbus(frame[:-2])
            actual = frame[-2] | (frame[-1] << 8)
            if expected != actual:
                del self._buffer[0]
                self.rejected += 1
                continue
            frames.append(frame)
            del self._buffer[:need]
        if len(self._buffer) > 512:
            self._buffer.clear()
            self.rejected += 1
        return frames
