"""Turn a pack reading into one NDJSON object with blank sensors removed."""

from __future__ import annotations

from anker_monitor.client import ReadResult as AnkerResult
from husky_monitor.client import ReadResult as HuskyResult

_HUSKY_TEMP_NOTE = (
    "this radio reports a few pack temperature sensors, not one sensor on every cell"
)


def compact(value: object) -> object:
    """Drop missing values. Zero watts and zero amps stay."""

    if isinstance(value, dict):
        kept: dict[str, object] = {}
        for key, item in value.items():
            cleaned = compact(item)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            kept[key] = cleaned
        return kept
    if isinstance(value, list):
        kept_items = []
        for item in value:
            cleaned = compact(item)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            kept_items.append(cleaned)
        return kept_items
    return value


def error_record(device: str, when: str, message: str) -> dict[str, object]:
    return {"time": when, "device": device, "error": message}


def husky_record(result: HuskyResult, when: str) -> dict[str, object]:
    """One Husky sample. A 0.0 C probe is an unused register and is left out."""

    reading = result.reading
    temperatures = [
        {"name": probe.name, "celsius": probe.celsius}
        for probe in reading.probes
        if probe.plausible and probe.celsius != 0.0
    ]
    cells = [
        {"index": cell.index, "volts": cell.volts}
        for cell in reading.cells
        if cell.volts > 0.5
    ]
    notes = [note for note in reading.notes if note != _HUSKY_TEMP_NOTE]
    payload: dict[str, object] = {
        "time": when,
        "device": "husky",
        "name": result.name,
        "address": result.address,
        "manufacturer": result.manufacturer,
        "voltage_v": reading.voltage_v,
        "current_a": reading.current_a,
        "charge_state": None if reading.charge_state == "unknown" else reading.charge_state,
        "soc_percent": reading.soc_percent,
        "soh_percent": reading.soh_percent,
        "capacity_ah": reading.capacity_ah,
        "temperatures_c": temperatures,
        "cells_v": cells,
        "min_cell_v": reading.min_cell_v,
        "max_cell_v": reading.max_cell_v,
        "delta_cell_v": reading.delta_cell_v,
        "cell_sum_v": reading.cell_sum_v,
        "source": reading.source,
        "notes": notes,
    }
    return _record(payload)


def anker_record(result: AnkerResult, when: str) -> dict[str, object]:
    """One F3800 sample. Fields the station did not send are left out."""

    if result.reading is None:
        return error_record("f3800", when, result.detail or "no status frame")
    reading = result.reading
    payload: dict[str, object] = {
        "time": when,
        "device": "f3800",
        "name": result.name,
        "address": result.address,
        "identity_mac": result.identity_mac,
        "soc_percent": reading.soc_percent,
        "soc_other_percent": reading.soc_other_percent,
        "temperature_c": reading.temperature_c,
        "ac_power_in_w": reading.ac_power_in_w,
        "ac_power_out_w": reading.ac_power_out_w,
        "solar_power_w": reading.solar_power_w,
        "solar_pv1_w": reading.solar_pv1_w,
        "solar_pv2_w": reading.solar_pv2_w,
        "charge_power_w": reading.charge_power_w,
        "output_power_w": reading.output_power_w,
        "discharge_power_w": reading.discharge_power_w,
        "usb_c1_w": reading.usb_c1_w,
        "usb_c2_w": reading.usb_c2_w,
        "usb_c3_w": reading.usb_c3_w,
        "usb_a1_w": reading.usb_a1_w,
        "usb_a2_w": reading.usb_a2_w,
        "ac_output": reading.ac_output,
        "dc_output": reading.dc_output,
        "charging": reading.charging,
        "hours_remaining": reading.hours_remaining,
        "serial_number": reading.serial_number,
        "source": reading.source,
        "notes": reading.notes,
    }
    return _record(payload)


def _record(payload: dict[str, object]) -> dict[str, object]:
    cleaned = compact(payload)
    if not isinstance(cleaned, dict):
        return {}
    return cleaned
