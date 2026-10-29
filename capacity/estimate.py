"""Estimate capacity and runtime from collector NDJSON.

The Husky is in parallel on the F3800 expansion port. The Solix percentage
is the internal 75 Ah pack. The Husky's 100 Ah never enters that percentage.
The bus voltage in the log is the Husky reading. The Solix reports none.

Nameplate figures:

* Solix F3800, Anker user guide: 51.2 V, 75 Ah, 3840 Wh (80 cells of
  32140FS, 15 Ah, 16S5P). No pack low-voltage cutoff is published.
* Husky 2 ``FHSKY-48051-G2``, BigBattery spec sheet: 51.2 V, 100 Ah,
  5120 Wh, 16S. Operating window 48–57 V. Suggested low cutoff 50.8 V.
  BMS cutoff 42–47 V. Charge window 55.6–57 V.

The Solix percentage is the watt-hours left in the Solix pack. The Husky
percentage is not. On this bus it moves as if the pack were about 60 Ah.
Husky watt-hours are amp-hours counted from the last time the pack was
rested full: Solix near 100% and the lowest cell at or above 3.30 V. That
is where this Solix stops the charge. A generic chart's 3.40 V rested-full
assumes a charge to 3.65 V per cell, which this bus does not reach.

The lowest cell is the empty check, not a percentage. The curve is flat
from about 3.20 V to 3.33 V. The Husky's suggested stop is 50.8 V, which
is 3.175 V per cell. Its operating floor is 48 V, 3.00 V per cell. The
F3800 publishes no pack cutoff.

Amp-hours inferred for the Solix are the bus minus the Husky. They do not
replace the 75 Ah nameplate. Amp-hours are not invented across a gap. A
voltage drop while the Husky percentage holds still, just after charge
current stops, is surface charge leaving the cells. ``discharge_power_w``
is not a battery current: on this station it sits near 70 W through charge
and idle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SOLIX_AH = 75.0
SOLIX_WH = 3840.0
HUSKY_AH = 100.0
HUSKY_WH = 5120.0
NOMINAL_V = 51.2

# Husky 2 published limits. The shared stop on this bus is the suggested
# cutoff, because the F3800 does not publish a pack voltage floor.
HUSKY_OPERATING_MIN_V = 48.0
HUSKY_OPERATING_MAX_V = 57.0
HUSKY_SUGGESTED_STOP_V = 50.8
HUSKY_BMS_CUTOFF_MIN_V = 42.0
HUSKY_BMS_CUTOFF_MAX_V = 47.0
HUSKY_CHARGE_MIN_V = 55.6
HUSKY_CHARGE_MAX_V = 57.0
# Rested lowest cell when the Solix counter is still full on this bus.
HUSKY_FULL_CELL_V = 3.30
HUSKY_STOP_CELL_V = HUSKY_SUGGESTED_STOP_V / 16
HUSKY_FLOOR_CELL_V = HUSKY_OPERATING_MIN_V / 16
REST_AMPS = 0.5
SOLIX_FULL_SOC = 98.0

CHARGER_EFF = 0.90
INVERTER_EFF = 0.90
SOLAR_EFF = 0.95

# A shorter hole is still one stretch. The collector's usual gap is 60 s.
MAX_GAP_S = 15 * 60
MIN_SOC_POINTS = 10.0
# One sample at 1 A is the current register twitching, not a session.
ACTIVE_AMPS = 1.0
ACTIVE_RUN = 2
# A leg outside this band is a bad integral, not a new nameplate.
CAPACITY_AH_MIN = 20.0
CAPACITY_AH_MAX = 300.0
REST_DROP_V = 0.15
LOADS_W = (100, 250, 500, 1000, 2000)
BATTERY_LOAD_W = 50.0

_USB_KEYS = ("usb_c1_w", "usb_c2_w", "usb_c3_w", "usb_a1_w", "usb_a2_w")


@dataclass(frozen=True)
class Sample:
    """One moment. Solix fields are empty when that line was an error."""

    time: datetime
    voltage_v: float | None = None
    current_a: float | None = None
    husky_soc: float | None = None
    min_cell_v: float | None = None
    max_cell_v: float | None = None
    solix_soc: float | None = None
    ac_in_w: float | None = None
    ac_out_w: float | None = None
    solar_w: float | None = None
    usb_w: float | None = None
    solix_hours: float | None = None


@dataclass(frozen=True)
class Leg:
    pack: str
    start: datetime
    end: datetime
    soc_start: float
    soc_end: float
    amp_hours: float
    capacity_ah: float


@dataclass(frozen=True)
class Relaxation:
    charge_voltage_v: float
    charge_current_a: float
    charge_time: datetime
    first_rest_v: float
    first_rest_time: datetime
    last_rest_v: float
    last_rest_time: datetime
    husky_soc: float
    solix_soc: float | None


@dataclass(frozen=True)
class Gap:
    seconds: float
    husky_before: float | None
    husky_after: float | None
    solix_before: float | None
    solix_after: float | None


@dataclass(frozen=True)
class HuskyCount:
    """Husky state from cell voltage and counted amp-hours, not its percentage."""

    soc: float | None
    amp_hours_out: float | None
    anchor_soc: float | None
    anchor_time: datetime | None
    anchor_cell_v: float | None
    register_soc: float | None
    register_ah: float | None
    min_cell_v: float | None
    at_stop: bool


@dataclass(frozen=True)
class Remaining:
    husky_wh: float | None
    solix_wh: float | None
    husky_ah: float
    solix_ah: float
    husky_soc: float | None
    solix_soc: float | None
    husky_from_log: bool
    solix_from_log: bool
    husky_time: datetime | None
    solix_time: datetime | None


@dataclass(frozen=True)
class Runtime:
    load_w: float
    combined_h: float | None
    solix_h: float | None
    logged: bool


@dataclass(frozen=True)
class Analysis:
    samples: int
    husky_samples: int
    solix_samples: int
    start: datetime | None
    end: datetime | None
    voltage_min: float | None
    voltage_max: float | None
    charge_peak_v: float | None
    charge_peak_a: float | None
    charge_peak_soc: float | None
    husky_soc_min: float | None
    husky_soc_max: float | None
    solix_soc_min: float | None
    solix_soc_max: float | None
    husky_span: float
    solix_span: float
    legs: tuple[Leg, ...]
    husky_ah: float | None
    solix_ah: float | None
    husky_count: HuskyCount | None
    relaxation: Relaxation | None
    largest_gap: Gap | None
    skipped_lines: int
    latest: Sample | None
    remaining: Remaining | None
    runtimes: tuple[Runtime, ...]
    logged_load_w: float | None


def battery_bus_watts(
    ac_in_w: float,
    ac_out_w: float,
    solar_w: float = 0.0,
    usb_w: float = 0.0,
    *,
    charger_eff: float = CHARGER_EFF,
    inverter_eff: float = INVERTER_EFF,
    solar_eff: float = SOLAR_EFF,
) -> float:
    """Watts into the shared battery bus. Positive charges the packs.

    AC input above the AC load is charger output. AC load above the AC
    input is inverter input. USB is a bus load either way.
    """

    surplus = ac_in_w - ac_out_w
    solar = solar_eff * solar_w
    if surplus >= 0:
        return charger_eff * surplus + solar - usb_w
    return surplus / inverter_eff + solar - usb_w


def load_logs(root: Path) -> tuple[list[Sample], int]:
    """Read every ``readings.ndjson`` under ``root``. Return samples and skip count."""

    grouped: dict[str, dict[str, object]] = {}
    skipped = 0
    files = sorted(root.rglob("readings.ndjson")) if root.is_dir() else []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(record, dict) or "time" not in record or "device" not in record:
                skipped += 1
                continue
            stamp = str(record["time"])
            slot = grouped.setdefault(stamp, {})
            slot[str(record["device"])] = record
    samples = []
    for stamp, devices in grouped.items():
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            skipped += 1
            continue
        sample = _sample(moment, devices.get("husky"), devices.get("f3800"))
        if sample.voltage_v is None and sample.solix_soc is None:
            continue
        samples.append(sample)
    samples.sort(key=lambda item: item.time)
    return samples, skipped


def analyze(
    samples: list[Sample],
    *,
    skipped_lines: int = 0,
    loads_w: tuple[float, ...] = LOADS_W,
    min_soc_points: float = MIN_SOC_POINTS,
    logged_load: bool = True,
) -> Analysis:
    """Turn paired samples into nameplate, measured legs, and runtimes."""

    husky_n = sum(1 for sample in samples if sample.voltage_v is not None)
    solix_n = sum(1 for sample in samples if sample.solix_soc is not None)
    voltages = [sample.voltage_v for sample in samples if sample.voltage_v is not None]
    husky_socs = [sample.husky_soc for sample in samples if sample.husky_soc is not None]
    solix_socs = [sample.solix_soc for sample in samples if sample.solix_soc is not None]
    count = _count_husky(samples)
    legs = _legs(samples, min_soc_points)
    solix_ah = _combine(legs)
    peak = _charge_peak(samples)
    relaxation = _relaxation(samples)
    gap = _largest_gap(samples)
    latest = samples[-1] if samples else None
    remaining = _remaining(samples, count)
    observed = _logged_load(samples) if logged_load else None
    runtimes = _runtimes(remaining, loads_w, observed)
    return Analysis(
        samples=len(samples),
        husky_samples=husky_n,
        solix_samples=solix_n,
        start=samples[0].time if samples else None,
        end=samples[-1].time if samples else None,
        voltage_min=min(voltages) if voltages else None,
        voltage_max=max(voltages) if voltages else None,
        charge_peak_v=None if peak is None else peak[0],
        charge_peak_a=None if peak is None else peak[1],
        charge_peak_soc=None if peak is None else peak[2],
        husky_soc_min=min(husky_socs) if husky_socs else None,
        husky_soc_max=max(husky_socs) if husky_socs else None,
        solix_soc_min=min(solix_socs) if solix_socs else None,
        solix_soc_max=max(solix_socs) if solix_socs else None,
        husky_span=_continuous_span(samples, "husky_soc"),
        solix_span=_continuous_span(samples, "solix_soc"),
        legs=tuple(legs),
        husky_ah=None,
        solix_ah=solix_ah,
        husky_count=count,
        relaxation=relaxation,
        largest_gap=gap,
        skipped_lines=skipped_lines,
        latest=latest,
        remaining=remaining,
        runtimes=tuple(runtimes),
        logged_load_w=observed,
    )


def render(analysis: Analysis) -> str:
    """Plain-text report."""

    lines: list[str] = []
    _logs(lines, analysis)
    _nameplate(lines)
    _measured(lines, analysis)
    _voltage(lines, analysis)
    _write_remaining(lines, analysis)
    _runtime(lines, analysis)
    return "\n".join(lines) + "\n"


def to_json(analysis: Analysis) -> dict[str, object]:
    """JSON-ready copy of the report numbers."""

    remaining = analysis.remaining
    return {
        "samples": analysis.samples,
        "husky_samples": analysis.husky_samples,
        "solix_samples": analysis.solix_samples,
        "start": None if analysis.start is None else analysis.start.isoformat(),
        "end": None if analysis.end is None else analysis.end.isoformat(),
        "nameplate_ah": {"solix": SOLIX_AH, "husky": HUSKY_AH},
        "measured_ah": {"solix": analysis.solix_ah, "husky": None},
        "husky_count": _count_json(analysis.husky_count),
        "voltage_min": analysis.voltage_min,
        "voltage_max": analysis.voltage_max,
        "remaining_wh": None
        if remaining is None
        else {
            "solix": _round(remaining.solix_wh, 1),
            "husky": _round(remaining.husky_wh, 1),
        },
        "runtimes": [
            {
                "load_w": row.load_w,
                "combined_h": _round(row.combined_h, 2),
                "solix_h": _round(row.solix_h, 2),
                "logged": row.logged,
            }
            for row in analysis.runtimes
        ],
    }


def _sample(moment: datetime, husky: object, solix: object) -> Sample:
    husky_row = husky if isinstance(husky, dict) else {}
    solix_row = solix if isinstance(solix, dict) else {}
    solix_ok = "soc_percent" in solix_row and "error" not in solix_row
    usb = None
    if solix_ok:
        usb = sum(float(solix_row[key]) for key in _USB_KEYS if _num(solix_row.get(key)) is not None)
    return Sample(
        time=moment,
        voltage_v=_num(husky_row.get("voltage_v")),
        current_a=_num(husky_row.get("current_a")),
        husky_soc=_num(husky_row.get("soc_percent")),
        min_cell_v=_num(husky_row.get("min_cell_v")),
        max_cell_v=_num(husky_row.get("max_cell_v")),
        solix_soc=_num(solix_row.get("soc_percent")) if solix_ok else None,
        ac_in_w=_num(solix_row.get("ac_power_in_w")) if solix_ok else None,
        ac_out_w=_num(solix_row.get("ac_power_out_w")) if solix_ok else None,
        solar_w=_num(solix_row.get("solar_power_w")) if solix_ok else None,
        usb_w=usb,
        solix_hours=_num(solix_row.get("hours_remaining")) if solix_ok else None,
    )


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _stretches(samples: list[Sample]) -> list[list[Sample]]:
    groups: list[list[Sample]] = []
    current: list[Sample] = []
    previous: Sample | None = None
    for sample in samples:
        if previous is not None and (sample.time - previous.time).total_seconds() > MAX_GAP_S:
            if len(current) >= 2:
                groups.append(current)
            current = []
        current.append(sample)
        previous = sample
    if len(current) >= 2:
        groups.append(current)
    return groups


def _legs(samples: list[Sample], min_points: float) -> list[Leg]:
    found: list[Leg] = []
    for stretch in _stretches(samples):
        for pack, attr in (("solix", "solix_soc"),):
            for run in _present_runs(stretch, attr):
                for leg_samples in _monotonic(run, attr):
                    leg = _leg(pack, attr, leg_samples, min_points)
                    if leg is not None:
                        found.append(leg)
    return found


def _present_runs(samples: list[Sample], attr: str) -> list[list[Sample]]:
    runs: list[list[Sample]] = []
    current: list[Sample] = []
    for sample in samples:
        if getattr(sample, attr) is None:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        current.append(sample)
    if len(current) >= 2:
        runs.append(current)
    return runs


def _monotonic(samples: list[Sample], attr: str) -> list[list[Sample]]:
    if len(samples) < 2:
        return []
    legs: list[list[Sample]] = []
    start = 0
    direction = 0
    for index in range(1, len(samples)):
        delta = getattr(samples[index], attr) - getattr(samples[index - 1], attr)
        step = 0 if abs(delta) < 0.05 else (1 if delta > 0 else -1)
        if step == 0:
            continue
        if direction == 0:
            direction = step
            continue
        if step != direction:
            legs.append(samples[start:index])
            start = index - 1
            direction = step
    legs.append(samples[start:])
    return [leg for leg in legs if len(leg) >= 2]


def _leg(pack: str, attr: str, samples: list[Sample], min_points: float) -> Leg | None:
    soc_start = getattr(samples[0], attr)
    soc_end = getattr(samples[-1], attr)
    delta_soc = soc_end - soc_start
    if abs(delta_soc) < min_points:
        return None
    husky_ah, solix_ah = _integrated(samples)
    amp_hours = husky_ah if pack == "husky" else solix_ah
    if amp_hours is None or amp_hours == 0 or (amp_hours > 0) != (delta_soc > 0):
        return None
    capacity = amp_hours / (delta_soc / 100.0)
    if not CAPACITY_AH_MIN <= capacity <= CAPACITY_AH_MAX:
        return None
    return Leg(
        pack=pack,
        start=samples[0].time,
        end=samples[-1].time,
        soc_start=soc_start,
        soc_end=soc_end,
        amp_hours=amp_hours,
        capacity_ah=capacity,
    )


def _integrated(samples: list[Sample]) -> tuple[float | None, float | None]:
    """Amp-hours from the first sample to the last. Positive charges the pack."""

    husky = 0.0
    solix = 0.0
    husky_ok = True
    solix_ok = True
    for previous, nxt in zip(samples, samples[1:]):
        dt = (nxt.time - previous.time).total_seconds()
        if dt <= 0 or previous.current_a is None or previous.voltage_v is None:
            husky_ok = False
            solix_ok = False
            continue
        if previous.voltage_v <= 0:
            husky_ok = False
            solix_ok = False
            continue
        husky += previous.current_a * dt / 3600.0
        bus = _bus(previous)
        if bus is None:
            solix_ok = False
            continue
        solix += (bus - previous.voltage_v * previous.current_a) / previous.voltage_v * dt / 3600.0
    return (husky if husky_ok else None, solix if solix_ok else None)


def _bus(sample: Sample) -> float | None:
    if sample.ac_in_w is None or sample.ac_out_w is None:
        return None
    return battery_bus_watts(
        sample.ac_in_w,
        sample.ac_out_w,
        0.0 if sample.solar_w is None else sample.solar_w,
        0.0 if sample.usb_w is None else sample.usb_w,
    )


def _combine(legs: list[Leg]) -> float | None:
    if not legs:
        return None
    weight = sum(abs(leg.soc_end - leg.soc_start) for leg in legs)
    if weight <= 0:
        return None
    return sum(leg.capacity_ah * abs(leg.soc_end - leg.soc_start) for leg in legs) / weight


def _continuous_span(samples: list[Sample], attr: str) -> float:
    best = 0.0
    for stretch in _stretches(samples):
        for run in _present_runs(stretch, attr):
            for leg in _monotonic(run, attr):
                best = max(best, abs(getattr(leg[-1], attr) - getattr(leg[0], attr)))
    return best


def _charge_peak(samples: list[Sample]) -> tuple[float, float, float] | None:
    best: tuple[float, float, float] | None = None
    for run in _active_runs(samples, positive=True):
        for sample in run:
            if sample.voltage_v is None or sample.current_a is None:
                continue
            if best is None or sample.voltage_v > best[0]:
                soc = -1.0 if sample.husky_soc is None else sample.husky_soc
                best = (sample.voltage_v, sample.current_a, soc)
    return best


def _active_runs(samples: list[Sample], *, positive: bool) -> list[list[Sample]]:
    runs: list[list[Sample]] = []
    current: list[Sample] = []
    for sample in samples:
        amps = sample.current_a
        active = amps is not None and abs(amps) >= ACTIVE_AMPS and (amps > 0) == positive
        if active:
            current.append(sample)
            continue
        if len(current) >= ACTIVE_RUN:
            runs.append(current)
        current = []
    if len(current) >= ACTIVE_RUN:
        runs.append(current)
    return runs


def _relaxation(samples: list[Sample]) -> Relaxation | None:
    best: Relaxation | None = None
    best_drop = 0.0
    for run in _active_runs(samples, positive=True):
        found = _relaxation_after(samples, samples.index(run[-1]))
        if found is None:
            continue
        drop = found.first_rest_v - found.last_rest_v
        if drop > best_drop:
            best = found
            best_drop = drop
    return best


def _relaxation_after(samples: list[Sample], charge_at: int) -> Relaxation | None:
    rest: list[Sample] = []
    for sample in samples[charge_at + 1 :]:
        if _in_sustained_discharge(samples, sample):
            break
        if sample.voltage_v is None or sample.current_a is None:
            continue
        if abs(sample.current_a) >= ACTIVE_AMPS:
            continue
        rest.append(sample)
    if len(rest) < 2:
        return None
    husky_values = [sample.husky_soc for sample in rest if sample.husky_soc is not None]
    if not husky_values or max(husky_values) - min(husky_values) > 1.0:
        return None
    solix_values = [sample.solix_soc for sample in rest if sample.solix_soc is not None]
    solix_soc = None
    if solix_values and max(solix_values) - min(solix_values) <= 1.0:
        solix_soc = solix_values[-1]
    if rest[-1].voltage_v is None or rest[0].voltage_v is None:
        return None
    if rest[0].voltage_v - rest[-1].voltage_v < REST_DROP_V:
        return None
    charged = samples[charge_at]
    if charged.voltage_v is None or charged.current_a is None or not husky_values:
        return None
    return Relaxation(
        charge_voltage_v=charged.voltage_v,
        charge_current_a=charged.current_a,
        charge_time=charged.time,
        first_rest_v=rest[0].voltage_v,
        first_rest_time=rest[0].time,
        last_rest_v=rest[-1].voltage_v,
        last_rest_time=rest[-1].time,
        husky_soc=husky_values[-1],
        solix_soc=solix_soc,
    )


def _in_sustained_discharge(samples: list[Sample], sample: Sample) -> bool:
    for run in _active_runs(samples, positive=False):
        if sample in run:
            return True
    return False


def _largest_gap(samples: list[Sample]) -> Gap | None:
    best: Gap | None = None
    for previous, nxt in zip(samples, samples[1:]):
        seconds = (nxt.time - previous.time).total_seconds()
        if seconds <= MAX_GAP_S:
            continue
        husky_moved = _moved(previous.husky_soc, nxt.husky_soc)
        solix_moved = _moved(previous.solix_soc, nxt.solix_soc)
        if not husky_moved and not solix_moved:
            continue
        if best is None or seconds > best.seconds:
            best = Gap(
                seconds=seconds,
                husky_before=previous.husky_soc,
                husky_after=nxt.husky_soc,
                solix_before=previous.solix_soc,
                solix_after=nxt.solix_soc,
            )
    return best


def _moved(before: float | None, after: float | None) -> bool:
    if before is None or after is None:
        return False
    return abs(after - before) >= 0.5


def _is_full(sample: Sample) -> bool:
    """Rested, Solix still full, and the lowest cell at this bus's full voltage."""

    if sample.current_a is None or abs(sample.current_a) >= REST_AMPS:
        return False
    if sample.solix_soc is None or sample.solix_soc < SOLIX_FULL_SOC:
        return False
    if sample.min_cell_v is None or sample.min_cell_v < HUSKY_FULL_CELL_V:
        return False
    return True


def _count_husky(samples: list[Sample]) -> HuskyCount:
    """Amp-hours out of the Husky since the latest rested full cell.

    The integral is left Riemann and skips a gap. The Husky percentage is
    stored beside the count. It is not a capacity.
    """

    cumulative = 0.0
    anchor_cum: float | None = None
    anchor_soc: float | None = None
    anchor_time: datetime | None = None
    anchor_cell: float | None = None
    anchor_register: float | None = None
    previous: Sample | None = None
    for sample in samples:
        if previous is not None and previous.current_a is not None:
            dt = (sample.time - previous.time).total_seconds()
            if 0 < dt <= MAX_GAP_S:
                cumulative += previous.current_a * dt / 3600.0
        if _is_full(sample):
            anchor_cum = cumulative
            anchor_soc = sample.solix_soc
            anchor_time = sample.time
            anchor_cell = sample.min_cell_v
            anchor_register = sample.husky_soc
        previous = sample
    latest_cell = next(
        (sample.min_cell_v for sample in reversed(samples) if sample.min_cell_v is not None),
        None,
    )
    latest_register = next(
        (sample.husky_soc for sample in reversed(samples) if sample.husky_soc is not None),
        None,
    )
    at_stop = latest_cell is not None and latest_cell <= HUSKY_STOP_CELL_V
    amp_hours_out = None
    soc = None
    register_ah = None
    if anchor_cum is not None and anchor_soc is not None:
        amp_hours_out = -(cumulative - anchor_cum)
        soc = min(100.0, max(0.0, anchor_soc - amp_hours_out / HUSKY_AH * 100.0))
        drop = None
        if anchor_register is not None and latest_register is not None:
            drop = anchor_register - latest_register
        if amp_hours_out > 1.0 and drop is not None and drop >= 5.0:
            register_ah = amp_hours_out / (drop / 100.0)
    return HuskyCount(
        soc=soc,
        amp_hours_out=amp_hours_out,
        anchor_soc=anchor_soc,
        anchor_time=anchor_time,
        anchor_cell_v=anchor_cell,
        register_soc=latest_register,
        register_ah=register_ah,
        min_cell_v=latest_cell,
        at_stop=at_stop,
    )


def _remaining(samples: list[Sample], count: HuskyCount) -> Remaining | None:
    solix = next((sample for sample in reversed(samples) if sample.solix_soc is not None), None)
    # The Solix percentage is the station's own count of its 75 Ah pack.
    # Amp-hours inferred from the bus are short of that, so they do not replace it.
    solix_capacity = SOLIX_AH
    solix_wh = None
    if solix is not None and solix.solix_soc is not None:
        solix_wh = solix_capacity * NOMINAL_V * solix.solix_soc / 100.0
    husky_wh = None
    husky_soc = None
    if count.at_stop:
        husky_wh = 0.0
        husky_soc = 0.0
    elif count.soc is not None:
        husky_wh = HUSKY_AH * NOMINAL_V * count.soc / 100.0
        husky_soc = count.soc
    if husky_wh is None and solix_wh is None:
        return None
    return Remaining(
        husky_wh=husky_wh,
        solix_wh=solix_wh,
        husky_ah=HUSKY_AH,
        solix_ah=solix_capacity,
        husky_soc=husky_soc,
        solix_soc=None if solix is None else solix.solix_soc,
        husky_from_log=False,
        solix_from_log=False,
        husky_time=count.anchor_time,
        solix_time=None if solix is None else solix.time,
    )


def _logged_load(samples: list[Sample]) -> float | None:
    loads = []
    for sample in samples:
        if sample.ac_in_w is None or sample.ac_out_w is None:
            continue
        if sample.ac_out_w <= sample.ac_in_w + 25.0:
            continue
        bus = _bus(sample)
        if bus is None or -bus < BATTERY_LOAD_W:
            continue
        loads.append(sample.ac_out_w)
    if not loads:
        return None
    loads.sort()
    return loads[len(loads) // 2]


def _runtimes(
    remaining: Remaining | None,
    loads_w: tuple[float, ...],
    logged_load: float | None,
) -> list[Runtime]:
    if remaining is None:
        return []
    rows: list[tuple[float, bool]] = [(load, False) for load in loads_w if load > 0]
    if logged_load is not None and logged_load > 0:
        rows.append((logged_load, True))
    rows.sort(key=lambda item: (item[0], item[1]))
    runtimes = []
    for load, logged in rows:
        combined_h = None
        if remaining.husky_wh is not None and remaining.solix_wh is not None:
            combined_h = _hours(remaining.husky_wh, remaining.solix_wh, load)
        runtimes.append(
            Runtime(
                load_w=load,
                combined_h=combined_h,
                solix_h=_hours(None, remaining.solix_wh, load),
                logged=logged,
            )
        )
    return runtimes


def _hours(husky_wh: float | None, solix_wh: float | None, load_w: float) -> float | None:
    if load_w <= 0:
        return None
    total = 0.0
    present = False
    if husky_wh is not None:
        total += husky_wh
        present = True
    if solix_wh is not None:
        total += solix_wh
        present = True
    if not present:
        return None
    return total * INVERTER_EFF / load_w


def _logs(lines: list[str], analysis: Analysis) -> None:
    lines.append("Logs")
    if analysis.start is None or analysis.end is None:
        lines.append("  no samples")
        return
    lines.append(f"  {_when(analysis.start)} through {_when(analysis.end)}")
    lines.append(
        f"  {analysis.husky_samples} Husky samples, {analysis.solix_samples} Solix samples"
    )
    if analysis.skipped_lines:
        lines.append(f"  {analysis.skipped_lines} lines skipped")


def _nameplate(lines: list[str]) -> None:
    lines.append("")
    lines.append("Nameplate, 16S LiFePO4")
    lines.append(
        f"  Solix F3800   {SOLIX_AH:.0f} Ah   {SOLIX_WH:.0f} Wh   {NOMINAL_V:.1f} V"
        "   pack cutoff not published"
    )
    lines.append(
        f"  Husky 2      {HUSKY_AH:.0f} Ah   {HUSKY_WH:.0f} Wh   {NOMINAL_V:.1f} V"
        f"   {HUSKY_OPERATING_MIN_V:.0f}–{HUSKY_OPERATING_MAX_V:.0f} V,"
        f" suggested stop {HUSKY_SUGGESTED_STOP_V:.1f} V,"
        f" BMS cutoff {HUSKY_BMS_CUTOFF_MIN_V:.0f}–{HUSKY_BMS_CUTOFF_MAX_V:.0f} V"
    )
    lines.append(
        f"  Combined     {SOLIX_AH + HUSKY_AH:.0f} Ah   {SOLIX_WH + HUSKY_WH:.0f} Wh"
    )


def _measured(lines: list[str], analysis: Analysis) -> None:
    lines.append("")
    lines.append("Measured")
    if analysis.solix_ah is None:
        lines.append(
            "  The Solix percentage did not move "
            f"{MIN_SOC_POINTS:.0f} points inside a continuous stretch, "
            f"so its {SOLIX_AH:.0f} Ah nameplate stands."
        )
    else:
        points = sum(abs(leg.soc_end - leg.soc_start) for leg in analysis.legs)
        amp_hours = sum(abs(leg.amp_hours) for leg in analysis.legs)
        lines.append(
            f"  Inferred Solix current is {amp_hours:.0f} Ah across {points:.0f} points,"
            f" which would be a {analysis.solix_ah:.0f} Ah pack."
            " That current is the bus minus the Husky, assuming a 90% charger and a 90% inverter."
            f" Remaining watt-hours use the {SOLIX_AH:.0f} Ah nameplate."
        )
    lines.append(
        "  The Husky percentage is not a capacity measurement."
        " Remaining Husky watt-hours are amp-hours counted from a rested full cell."
    )
    _register_line(lines, analysis.husky_soc_min, analysis.husky_soc_max, analysis.husky_span)
    _soc_line(lines, "Solix", analysis.solix_soc_min, analysis.solix_soc_max, analysis.solix_span)
    gap = analysis.largest_gap
    if gap is not None:
        lines.append(
            f"  A {_span(gap.seconds)} gap sits between samples, and a counter moved across it."
            " The amp-hours in that gap are not in the file."
        )


def _register_line(
    lines: list[str],
    low: float | None,
    high: float | None,
    span: float,
) -> None:
    if low is None or high is None:
        lines.append("  The Husky percentage was not in the log.")
        return
    points = "point" if abs(span - 1.0) < 0.05 else "points"
    lines.append(
        f"  The Husky percentage in the log is {_pct(low)}–{_pct(high)} and is not calibrated."
        f" The longest continuous move is {span:.0f} {points}."
    )


def _soc_line(
    lines: list[str],
    name: str,
    low: float | None,
    high: float | None,
    span: float,
) -> None:
    if low is None or high is None:
        lines.append(f"  {name} SOC was not in the log.")
        return
    points = "point" if abs(span - 1.0) < 0.05 else "points"
    lines.append(
        f"  {name} SOC in the log is {_pct(low)}–{_pct(high)}."
        f" The longest continuous move is {span:.0f} {points}."
    )


def _voltage(lines: list[str], analysis: Analysis) -> None:
    lines.append("")
    lines.append("Bus voltage")
    latest = analysis.latest
    if latest is None or latest.voltage_v is None:
        lines.append("  The log has no Husky voltage. The Solix reports no pack voltage.")
        return
    cells = ""
    if latest.min_cell_v is not None and latest.max_cell_v is not None:
        cells = f", cells {latest.min_cell_v:.3f}–{latest.max_cell_v:.3f} V"
    lines.append(
        f"  Latest {latest.voltage_v:.2f} V at {_when(latest.time)}{cells}."
        " The Solix reports no pack voltage."
    )
    if analysis.voltage_min is not None and analysis.voltage_max is not None:
        lines.append(
            f"  Range in the log {analysis.voltage_min:.2f}–{analysis.voltage_max:.2f} V."
        )
    above_stop = latest.voltage_v - HUSKY_SUGGESTED_STOP_V
    above_floor = latest.voltage_v - HUSKY_OPERATING_MIN_V
    lines.append(
        f"  {above_stop:.2f} V above the Husky suggested stop at {HUSKY_SUGGESTED_STOP_V:.1f} V,"
        f" and {above_floor:.2f} V above the {HUSKY_OPERATING_MIN_V:.0f} V operating floor."
    )
    if latest.min_cell_v is not None:
        lines.append(
            f"  Lowest cell {latest.min_cell_v:.3f} V."
            f" The suggested stop is {HUSKY_STOP_CELL_V:.3f} V per cell"
            f" ({HUSKY_SUGGESTED_STOP_V:.1f} V),"
            f" and the operating floor is {HUSKY_FLOOR_CELL_V:.2f} V per cell"
            f" ({HUSKY_OPERATING_MIN_V:.0f} V)."
        )
    if (
        analysis.charge_peak_v is not None
        and analysis.charge_peak_v < HUSKY_CHARGE_MIN_V
        and analysis.charge_peak_soc is not None
        and analysis.charge_peak_soc >= 95
    ):
        lines.append(
            f"  The highest charge voltage was {analysis.charge_peak_v:.2f} V"
            f" at {analysis.charge_peak_a:.1f} A with the Husky percentage at {_pct(analysis.charge_peak_soc)}."
            f" The Husky charge window is {HUSKY_CHARGE_MIN_V:.1f}–{HUSKY_CHARGE_MAX_V:.0f} V,"
            " so the Solix is finishing the charge on its own curve."
        )
    rest = analysis.relaxation
    if rest is not None:
        held = f"the Husky percentage stayed at {_pct(rest.husky_soc)}"
        if rest.solix_soc is not None:
            held += f" and the Solix stayed at {_pct(rest.solix_soc)}"
        lines.append(
            f"  Charging stopped at {rest.charge_voltage_v:.2f} V and {rest.charge_current_a:.1f} A."
            f" The next rested sample was {rest.first_rest_v:.2f} V,"
            f" and the bus then settled to {rest.last_rest_v:.2f} V"
            f" over {_span((rest.last_rest_time - rest.first_rest_time).total_seconds())}"
            f" while {held}. That is surface charge leaving. It is not a measure of capacity."
        )
    lines.append(
        "  From about 3.20 V to 3.33 V per cell the voltage barely moves,"
        " so it is not turned into a percentage. It checks that the Husky is"
        " still above 3.175 V. The Solix percentage is the Solix pack."
    )


def _write_remaining(lines: list[str], analysis: Analysis) -> None:
    lines.append("")
    lines.append("Remaining")
    remaining = analysis.remaining
    if remaining is None or (remaining.husky_wh is None and remaining.solix_wh is None):
        lines.append("  no state of charge in the log")
        return
    _write_husky(lines, remaining, analysis.husky_count)
    _pack_remaining(lines, "Solix", remaining.solix_wh, remaining.solix_soc, remaining.solix_ah, remaining.solix_from_log, SOLIX_AH)
    if remaining.husky_wh is not None and remaining.solix_wh is not None:
        lines.append(f"  Combined {remaining.husky_wh + remaining.solix_wh:.0f} Wh.")
    if remaining.solix_wh is not None:
        lines.append(f"  Solix alone is {remaining.solix_wh:.0f} Wh.")
    latest = analysis.latest
    if latest is not None and latest.solix_hours is not None:
        lines.append(
            f"  The station last reported {latest.solix_hours:.1f} h remaining, from its own pack."
        )


def _write_husky(lines: list[str], remaining: Remaining, count: HuskyCount | None) -> None:
    if count is None:
        lines.append("  Husky watt-hours are not counted.")
        return
    if count.at_stop and count.min_cell_v is not None:
        lines.append(
            f"  The lowest cell is {count.min_cell_v:.3f} V, at or below"
            f" {HUSKY_STOP_CELL_V:.3f} V. Remaining Husky watt-hours are 0."
        )
        _register_note(lines, count)
        return
    if count.soc is None or remaining.husky_wh is None or count.anchor_soc is None:
        lines.append(
            "  Husky watt-hours are left out. The log has no rested full point"
            " (Solix near 100% and the lowest cell at or above"
            f" {HUSKY_FULL_CELL_V:.2f} V). The Husky percentage is not used."
        )
        return
    when = "" if count.anchor_time is None else f" at {_when(count.anchor_time)}"
    cell = "" if count.anchor_cell_v is None else f", lowest cell {count.anchor_cell_v:.3f} V"
    amp_hours = 0.0 if count.amp_hours_out is None else count.amp_hours_out
    if amp_hours >= 0:
        moved = f"{amp_hours:.1f} Ah have left since then."
    else:
        moved = f"{-amp_hours:.1f} Ah have gone in since then."
    lines.append(
        f"  Husky {_pct(count.soc)} of the {HUSKY_AH:.0f} Ah nameplate"
        f" = {remaining.husky_wh:.0f} Wh, counted from Solix {_pct(count.anchor_soc)}{when}{cell}."
        f" {moved}"
    )
    _register_note(lines, count)


def _register_note(lines: list[str], count: HuskyCount) -> None:
    if count.register_soc is None:
        lines.append("  The Husky percentage was not in the log and is not used.")
        return
    lines.append(f"  The Husky percentage reads {_pct(count.register_soc)} and is not used.")
    if count.register_ah is not None:
        lines.append(
            f"  That percentage moved as if the pack were {count.register_ah:.0f} Ah."
            f" The nameplate is {HUSKY_AH:.0f} Ah."
        )


def _pack_remaining(
    lines: list[str],
    name: str,
    watt_hours: float | None,
    soc: float | None,
    amp_hours: float,
    from_log: bool,
    nameplate: float,
) -> None:
    if watt_hours is None or soc is None:
        lines.append(f"  {name} has no state of charge.")
        return
    if from_log:
        detail = f"{amp_hours:.0f} Ah from the log (nameplate {nameplate:.0f} Ah)"
    else:
        detail = f"the {amp_hours:.0f} Ah nameplate"
    lines.append(f"  {name} {_pct(soc)} of {detail} = {watt_hours:.0f} Wh.")


def _runtime(lines: list[str], analysis: Analysis) -> None:
    lines.append("")
    lines.append(
        "Runtime at an AC load. DC watt-hours times a 90% inverter, divided by the load."
    )
    if not analysis.runtimes:
        lines.append("  no remaining energy")
        return
    lines.append(f"  {'load':>8}  {'combined':>10}  {'Solix alone':>12}")
    for row in analysis.runtimes:
        mark = "  logged" if row.logged else ""
        lines.append(
            f"  {_watts(row.load_w):>8}  {_hour(row.combined_h):>10}  {_hour(row.solix_h):>12}{mark}"
        )
    if analysis.logged_load_w is None:
        lines.append("  No stretch in the log has the batteries carrying the AC load.")


def _when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _pct(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}%"
    return f"{value:.1f}%"


def _span(seconds: float) -> str:
    minutes = int(round(seconds / 60.0))
    if minutes < 90:
        return f"{minutes} min"
    return f"{seconds / 3600.0:.1f} h"


def _watts(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f} W"
    return f"{value:.1f} W"


def _hour(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} h"


def _count_json(count: HuskyCount | None) -> dict[str, object] | None:
    if count is None:
        return None
    return {
        "soc": _round(count.soc, 1),
        "amp_hours_out": _round(count.amp_hours_out, 2),
        "register_soc": _round(count.register_soc, 1),
        "register_ah": _round(count.register_ah, 1),
        "at_stop": count.at_stop,
    }


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)
