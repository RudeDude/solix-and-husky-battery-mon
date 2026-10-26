"""Status values taken from an F3800 telemetry map.

Field names follow the F3800 class in flip-dots/SolixBLE, which copies the
A1790 ``0405`` tags published by thomluther/anker-solix-api. Tag ``ad`` is
the state of charge the Anker app showed in a published capture. Tag ``c0``
is a second percentage and can differ when an expansion pack is attached.
Missing values use the library sentinel ``-1``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class StationReading:
    soc_percent: float | None = None
    soc_other_percent: float | None = None
    temperature_c: float | None = None
    ac_power_in_w: int | None = None
    ac_power_out_w: int | None = None
    solar_power_w: int | None = None
    solar_pv1_w: int | None = None
    solar_pv2_w: int | None = None
    charge_power_w: int | None = None
    output_power_w: int | None = None
    discharge_power_w: int | None = None
    usb_c1_w: int | None = None
    usb_c2_w: int | None = None
    usb_c3_w: int | None = None
    usb_a1_w: int | None = None
    usb_a2_w: int | None = None
    ac_output: str | None = None
    dc_output: str | None = None
    charging: str | None = None
    hours_remaining: float | None = None
    serial_number: str | None = None
    source: str = "solix-ble"
    notes: list[str] = field(default_factory=list)


def _number(value: object) -> int | float | None:
    """Return a number, including the decimal strings the cloud cache stores."""

    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text) if "." in text else int(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if value == -1 or value == -1.0:
        return None
    return value


def _watts(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "name") and not isinstance(value, str):
        name = str(value.name)
        if name == "UNKNOWN":
            return None
        return name.lower()
    text = str(value).strip()
    if not text or text.casefold() == "unknown":
        return None
    return text


def reading_from_fields(
    fields: dict[str, object],
    *,
    source: str = "solix-ble",
) -> StationReading:
    soc = _number(fields.get("soc_percent"))
    other = _number(fields.get("soc_other_percent"))
    notes: list[str] = []
    if soc is not None and other is not None and soc == other:
        other = None
    elif soc is not None and other is not None:
        notes.append(
            "the second percentage is a separate register and can differ "
            "from the charge shown in the Anker app when an expansion pack is installed"
        )
    return StationReading(
        soc_percent=None if soc is None else float(soc),
        soc_other_percent=None if other is None else float(other),
        temperature_c=None if _number(fields.get("temperature_c")) is None else float(fields["temperature_c"]),
        ac_power_in_w=_watts(fields.get("ac_power_in_w")),
        ac_power_out_w=_watts(fields.get("ac_power_out_w")),
        solar_power_w=_watts(fields.get("solar_power_w")),
        solar_pv1_w=_watts(fields.get("solar_pv1_w")),
        solar_pv2_w=_watts(fields.get("solar_pv2_w")),
        charge_power_w=_watts(fields.get("charge_power_w")),
        output_power_w=_watts(fields.get("output_power_w")),
        discharge_power_w=_watts(fields.get("discharge_power_w")),
        usb_c1_w=_watts(fields.get("usb_c1_w")),
        usb_c2_w=_watts(fields.get("usb_c2_w")),
        usb_c3_w=_watts(fields.get("usb_c3_w")),
        usb_a1_w=_watts(fields.get("usb_a1_w")),
        usb_a2_w=_watts(fields.get("usb_a2_w")),
        ac_output=_text(fields.get("ac_output")),
        dc_output=_text(fields.get("dc_output")),
        charging=_text(fields.get("charging")),
        hours_remaining=None
        if _number(fields.get("hours_remaining")) is None
        else float(fields["hours_remaining"]),
        serial_number=_text(fields.get("serial_number")),
        source=source,
        notes=notes,
    )


def _line(label: str, value: object, unit: str = "") -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        rendered = f"{value:.1f}"
    else:
        rendered = str(value)
    return f"{label}: {rendered}{unit}"


def format_reading(reading: StationReading) -> str:
    rows = [
        _line("state of charge", reading.soc_percent, "%"),
        _line("other charge register", reading.soc_other_percent, "%"),
        _line("temperature", reading.temperature_c, " C"),
        _line("AC in", reading.ac_power_in_w, " W"),
        _line("AC out", reading.ac_power_out_w, " W"),
        _line("solar", reading.solar_power_w, " W"),
        _line("solar port 1", reading.solar_pv1_w, " W"),
        _line("solar port 2", reading.solar_pv2_w, " W"),
        _line("charging power", reading.charge_power_w, " W"),
        _line("output power", reading.output_power_w, " W"),
        _line("discharge power", reading.discharge_power_w, " W"),
        _line("USB-C 1", reading.usb_c1_w, " W"),
        _line("USB-C 2", reading.usb_c2_w, " W"),
        _line("USB-C 3", reading.usb_c3_w, " W"),
        _line("USB-A 1", reading.usb_a1_w, " W"),
        _line("USB-A 2", reading.usb_a2_w, " W"),
        _line("AC output", reading.ac_output),
        _line("DC output", reading.dc_output),
        _line("charging", reading.charging),
        _line("hours remaining", reading.hours_remaining, " h"),
        _line("serial", reading.serial_number),
    ]
    lines = [row for row in rows if row is not None]
    lines.extend(reading.notes)
    lines.append(f"source: {reading.source}")
    return "\n".join(lines)


def reading_to_json(reading: StationReading) -> str:
    return json.dumps(asdict(reading), indent=2)
