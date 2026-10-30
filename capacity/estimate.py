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
percentage is not. Husky watt-hours are amp-hours counted from the last
time the pack was rested full: Solix near 100% and the lowest cell at or
above 3.30 V. That is where this Solix stops the charge. A generic chart's
3.40 V rested-full assumes a charge to 3.65 V per cell, which this bus
does not reach.

The lowest cell is the empty check, not a percentage. The curve is flat
from about 3.20 V to 3.33 V. This discharge was stopped at a rested 3.00 V
on the lowest cell, before an emergency shutdown. That point is the usable
empty. The published suggested stop is 50.8 V, 3.175 V per cell, and the
operating floor is 48 V. The cells are not even at the bottom, so a 48 V
pack reading would push the lowest cell further down. The completed
discharge, 26 Oct 18:29 through 29 Oct 15:38, put 53.6 Ah through the Husky
between those two rested points. Remaining Husky watt-hours use that
window, not the 100 Ah nameplate.

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

# Husky 2 published limits. This bus stops on the lowest cell, not on the
# 48 V pack floor and not on the 50.8 V suggested stop.
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
# Usable empty: a lowest cell at or below this voltage. The 2026-10-29
# discharge was stopped here on purpose.
HUSKY_EMPTY_CELL_V = 3.00
# Amp-hours from that rested full point to the rested 3.00 V cell.
# A later rested empty in the log replaces this.
HUSKY_USABLE_AH = 53.6
HUSKY_USABLE_MIN_AH = 20.0
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
    usable_ah: float
    at_empty: bool


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


def render(analysis: Analysis, *, verbose: bool = False) -> str:
    """Plain-text report.

    The default is total and remaining capacity, then shared-pack runtime.
    ``verbose`` adds the log notes.
    """

    lines: list[str] = []
    _capacity_table(lines, analysis)
    _runtime(lines, analysis, verbose=verbose)
    if verbose:
        _logs(lines, analysis)
        _nameplate(lines)
        _write_remaining(lines, analysis)
        _measured(lines, analysis)
        _voltage(lines, analysis)
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
        "nameplate_ah": {"solix": SOLIX_AH, "husky": HUSKY_AH, "total": SOLIX_AH + HUSKY_AH},
        "shared_pack_wh": {
            "solix": SOLIX_WH,
            "husky": HUSKY_WH,
            "total": SOLIX_WH + HUSKY_WH,
        },
        "usable_wh": _usable_json(analysis),
        "measured_ah": {"solix": analysis.solix_ah, "husky": None},
        "husky_count": _count_json(analysis.husky_count),
        "voltage_min": analysis.voltage_min,
        "voltage_max": analysis.voltage_max,
        "remaining_wh": _remaining_json(remaining),
        "runtimes": [
            {
                "load_w": row.load_w,
                "full_h": _round(_hours(*_usable_watt_hours(analysis), row.load_w), 2),
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


def _is_rested_empty(sample: Sample) -> bool:
    """Rested, and the lowest cell is at the usable empty voltage."""

    if sample.current_a is None or abs(sample.current_a) >= REST_AMPS:
        return False
    if sample.min_cell_v is None or sample.min_cell_v > HUSKY_EMPTY_CELL_V:
        return False
    return True


def _count_husky(samples: list[Sample]) -> HuskyCount:
    """Amp-hours out of the Husky since the latest rested full cell.

    The integral is left Riemann and skips a gap. Usable capacity is the
    deepest rested discharge to ``HUSKY_EMPTY_CELL_V`` in the log, or the
    measured window when the log has not finished one. The Husky percentage
    is stored beside the count. It is not a capacity.
    """

    cumulative = 0.0
    anchor_cum: float | None = None
    anchor_soc: float | None = None
    anchor_time: datetime | None = None
    anchor_cell: float | None = None
    anchor_register: float | None = None
    measured: float | None = None
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
        if anchor_cum is not None and _is_rested_empty(sample):
            taken = -(cumulative - anchor_cum)
            if taken >= HUSKY_USABLE_MIN_AH and (measured is None or taken >= measured):
                measured = taken
        previous = sample
    latest_cell = next(
        (sample.min_cell_v for sample in reversed(samples) if sample.min_cell_v is not None),
        None,
    )
    latest_register = next(
        (sample.husky_soc for sample in reversed(samples) if sample.husky_soc is not None),
        None,
    )
    usable = measured if measured is not None else HUSKY_USABLE_AH
    at_empty = latest_cell is not None and latest_cell <= HUSKY_EMPTY_CELL_V
    amp_hours_out = None
    soc = None
    register_ah = None
    if anchor_cum is not None and anchor_soc is not None:
        amp_hours_out = -(cumulative - anchor_cum)
        remaining_ah = min(usable, max(0.0, usable - amp_hours_out))
        soc = remaining_ah / usable * 100.0
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
        usable_ah=usable,
        at_empty=at_empty,
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
    if count.at_empty:
        husky_wh = 0.0
        husky_soc = 0.0
    elif count.soc is not None:
        husky_wh = count.usable_ah * NOMINAL_V * count.soc / 100.0
        husky_soc = count.soc
    if husky_wh is None and solix_wh is None:
        return None
    return Remaining(
        husky_wh=husky_wh,
        solix_wh=solix_wh,
        husky_ah=count.usable_ah,
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


def estimate_env(analysis: Analysis, *, generated_at: datetime | None = None) -> str:
    """Shell assignments for the overall estimate. ``status.sh`` sources this text."""

    moment = generated_at if generated_at is not None else datetime.now().astimezone()
    lines = [
        "# Overall capacity estimate. Written by python -m capacity for status.sh.",
        "# GENERATED_AT is when this estimate was run.",
        f"GENERATED_AT={_env_quote(moment.isoformat(timespec='seconds'))}",
        f"SOLIX_TOTAL_AH={SOLIX_AH:.0f}",
    ]
    lines.append(f"SOLIX_TOTAL_WH={SOLIX_WH:.0f}")
    lines.append(f"HUSKY_TOTAL_AH={HUSKY_AH:.0f}")
    lines.append(f"HUSKY_TOTAL_WH={HUSKY_WH:.0f}")
    lines.append(f"SHARED_TOTAL_AH={SOLIX_AH + HUSKY_AH:.0f}")
    lines.append(f"SHARED_TOTAL_WH={SOLIX_WH + HUSKY_WH:.0f}")
    husky_usable, solix_usable = _usable_watt_hours(analysis)
    shared_usable = husky_usable + solix_usable
    lines.append(f"HUSKY_USABLE_AH={_env_ah(_usable_husky_ah(analysis))}")
    lines.append(f"HUSKY_USABLE_WH={husky_usable}")
    lines.append(f"SOLIX_USABLE_AH={SOLIX_AH:.0f}")
    lines.append(f"SOLIX_USABLE_WH={solix_usable}")
    lines.append(f"SHARED_USABLE_AH={_env_ah(_usable_husky_ah(analysis) + SOLIX_AH)}")
    lines.append(f"SHARED_USABLE_WH={shared_usable}")
    remaining = analysis.remaining
    _env_remaining(lines, "SOLIX", *_pack_figures(remaining, "solix"))
    _env_remaining(lines, "HUSKY", *_pack_figures(remaining, "husky"))
    _env_remaining(lines, "SHARED", *_shared_figures(remaining))
    rows = [row for row in analysis.runtimes if not row.logged]
    lines.append(f"RUNTIME_COUNT={len(rows)}")
    for index, row in enumerate(rows, start=1):
        full_h = _hours(husky_usable, solix_usable, row.load_w)
        lines.append(f"RUNTIME_{index}_W={_env_number(row.load_w, 0)}")
        lines.append(f"RUNTIME_{index}_FULL_H={_env_hours(full_h)}")
        lines.append(f"RUNTIME_{index}_REMAINING_H={_env_hours(row.combined_h)}")
    return "\n".join(lines) + "\n"


def write_estimate(analysis: Analysis, path: Path) -> None:
    """Replace ``path`` with the overall estimate. Readers never see a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(estimate_env(analysis), encoding="utf-8")
    temporary.replace(path)


def _env_remaining(
    lines: list[str],
    prefix: str,
    amp_hours: float | None,
    watt_hours: float | None,
    percent: float | None,
) -> None:
    lines.append(f"{prefix}_REMAINING_AH={_env_ah(amp_hours)}")
    lines.append(f"{prefix}_REMAINING_WH={'' if watt_hours is None else str(_wh(watt_hours))}")
    lines.append(f"{prefix}_REMAINING_PERCENT={_env_pct(percent)}")


def _env_quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def _env_pct(value: float | None) -> str:
    if value is None:
        return ""
    return _pct(value)[:-1]


def _env_ah(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}"


def _env_hours(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}"


def _env_number(value: float, whole_digits: int) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.{whole_digits}f}" if whole_digits else f"{value:.1f}"


def _usable_husky_ah(analysis: Analysis) -> float:
    if analysis.husky_count is None:
        return HUSKY_USABLE_AH
    return analysis.husky_count.usable_ah


def _usable_watt_hours(analysis: Analysis) -> tuple[int, int]:
    """Husky and Solix watt-hours at the usable full point."""

    return _wh(_usable_husky_ah(analysis) * NOMINAL_V), _wh(SOLIX_WH)


def _usable_json(analysis: Analysis) -> dict[str, int]:
    husky, solix = _usable_watt_hours(analysis)
    return {"solix": solix, "husky": husky, "total": husky + solix}


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


def _capacity_table(lines: list[str], analysis: Analysis) -> None:
    """Usable and remaining amp-hours and watt-hours for each pack."""

    remaining = analysis.remaining
    husky_ah = _usable_husky_ah(analysis)
    husky_wh, solix_wh = _usable_watt_hours(analysis)
    lines.append("Capacity")
    lines.append(f"  {'':6} {'usable':^19}   {'remaining':^27}")
    lines.append(_capacity_row("Solix", SOLIX_AH, solix_wh, *_pack_figures(remaining, "solix")))
    lines.append(_capacity_row("Husky", husky_ah, husky_wh, *_pack_figures(remaining, "husky")))
    lines.append(
        _capacity_row(
            "Shared",
            husky_ah + SOLIX_AH,
            husky_wh + solix_wh,
            *_shared_figures(remaining),
        )
    )


def _pack_figures(
    remaining: Remaining | None,
    pack: str,
) -> tuple[float | None, float | None, float | None]:
    """Remaining amp-hours, watt-hours, and percent for one pack."""

    if remaining is None:
        return None, None, None
    if pack == "husky":
        if remaining.husky_wh is None or remaining.husky_soc is None:
            return None, None, None
        amp_hours = remaining.husky_ah * remaining.husky_soc / 100.0
        return amp_hours, remaining.husky_wh, remaining.husky_soc
    if remaining.solix_wh is None or remaining.solix_soc is None:
        return None, None, None
    amp_hours = remaining.solix_ah * remaining.solix_soc / 100.0
    return amp_hours, remaining.solix_wh, remaining.solix_soc


def _shared_figures(
    remaining: Remaining | None,
) -> tuple[float | None, float | None, float | None]:
    """Remaining amp-hours, watt-hours, and percent of the usable shared pack."""

    if remaining is None:
        return None, None, None
    parts = _still_parts(remaining)
    if len(parts) < 2:
        return None, None, None
    watt_hours = float(sum(item[1] for item in parts))
    amp_hours = sum(item[2] for item in parts)
    usable = _wh(remaining.husky_ah * NOMINAL_V) + _wh(remaining.solix_ah * NOMINAL_V)
    return amp_hours, watt_hours, watt_hours / usable * 100.0


def _capacity_row(
    name: str,
    total_ah: float,
    total_wh: float,
    rem_ah: float | None,
    rem_wh: float | None,
    rem_pct: float | None,
) -> str:
    rem_ah_s = "—" if rem_ah is None else f"{rem_ah:.1f} Ah"
    rem_wh_s = "—" if rem_wh is None else f"{_wh(rem_wh)} Wh"
    pct_s = "—" if rem_pct is None else _pct(rem_pct)
    return (
        f"  {name:<6} {_ah(total_ah):>6} Ah {total_wh:6.0f} Wh"
        f"   {rem_ah_s:>10} {rem_wh_s:>9} {pct_s:>6}"
    )


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
    lines.append("Shared pack capacity, 16S LiFePO4")
    lines.append("  Both packs full.")
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
        f"  Total shared {SOLIX_AH + HUSKY_AH:.0f} Ah   {SOLIX_WH + HUSKY_WH:.0f} Wh"
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
        " Remaining Husky watt-hours are amp-hours counted from a rested full cell"
        f" across the {_usable_husky_ah(analysis):.1f} Ah usable window."
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
        " so it is not turned into a percentage. The Husky is empty when the"
        f" lowest cell reaches {HUSKY_EMPTY_CELL_V:.2f} V."
        " The Solix percentage is the Solix pack."
    )


def _write_remaining(lines: list[str], analysis: Analysis) -> None:
    lines.append("")
    lines.append("Remaining now")
    remaining = analysis.remaining
    if remaining is None or (remaining.husky_wh is None and remaining.solix_wh is None):
        lines.append("  no state of charge in the log")
        return
    _write_husky(lines, remaining, analysis.husky_count)
    _pack_remaining(
        lines,
        "Solix",
        remaining.solix_wh,
        remaining.solix_soc,
        remaining.solix_ah,
        remaining.solix_from_log,
        SOLIX_AH,
    )
    _write_still(lines, remaining)
    latest = analysis.latest
    if latest is not None and latest.solix_hours is not None:
        lines.append(
            f"  The station last reported {latest.solix_hours:.1f} h remaining, for the Solix pack."
        )


def _write_husky(lines: list[str], remaining: Remaining, count: HuskyCount | None) -> None:
    if count is None:
        lines.append("  Husky watt-hours are not counted.")
        return
    if count.at_empty and count.min_cell_v is not None:
        lines.append(
            f"  The lowest cell is {count.min_cell_v:.3f} V, at or below"
            f" {HUSKY_EMPTY_CELL_V:.2f} V. Remaining Husky watt-hours are 0."
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
        f"  Husky {_pct(count.soc)} of its {count.usable_ah:.1f} Ah usable,"
        f" {_wh(remaining.husky_wh)} Wh of its {_wh(count.usable_ah * NOMINAL_V)} Wh,"
        f" counted from Solix {_pct(count.anchor_soc)}{when}{cell}."
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
    pack_wh = amp_hours * NOMINAL_V
    if from_log:
        lines.append(
            f"  {name} {_pct(soc)} of {amp_hours:.0f} Ah from the log"
            f" (nameplate {nameplate:.0f} Ah) = {_wh(watt_hours)} Wh."
        )
        return
    lines.append(
        f"  {name} {_pct(soc)} of its {amp_hours:.0f} Ah,"
        f" {_wh(watt_hours)} Wh of its {_wh(pack_wh)} Wh."
    )


def _still_parts(remaining: Remaining) -> list[tuple[str, int, float]]:
    """Rounded watt-hours and amp-hours for each pack that has a reading."""

    parts: list[tuple[str, int, float]] = []
    if remaining.husky_wh is not None and remaining.husky_soc is not None:
        parts.append(
            ("Husky", _wh(remaining.husky_wh), remaining.husky_ah * remaining.husky_soc / 100.0)
        )
    if remaining.solix_wh is not None and remaining.solix_soc is not None:
        parts.append(
            ("Solix", _wh(remaining.solix_wh), remaining.solix_ah * remaining.solix_soc / 100.0)
        )
    return parts


def _write_still(lines: list[str], remaining: Remaining) -> None:
    """Watt-hours still in the packs, set beside the full shared pack."""

    parts = _still_parts(remaining)
    if not parts:
        return
    watt_hours = sum(item[1] for item in parts)
    amp_hours = sum(item[2] for item in parts)
    shared_wh = _wh(remaining.husky_ah * NOMINAL_V) + _wh(remaining.solix_ah * NOMINAL_V)
    shared_ah = remaining.husky_ah + remaining.solix_ah
    if len(parts) == 2:
        fraction = watt_hours / shared_wh * 100.0
        lines.append(
            f"  Still in the packs: {watt_hours:.0f} Wh,"
            f" {_pct(fraction)} of the {shared_wh:.0f} Wh usable pack"
            f" ({amp_hours:.1f} Ah of {_ah(shared_ah)} Ah)."
        )
        return
    name = parts[0][0]
    lines.append(
        f"  {watt_hours:.0f} Wh counted so far, in the {name}."
        f" Usable shared pack with both packs full: {_ah(shared_ah)} Ah, {shared_wh:.0f} Wh."
    )


def _remaining_json(remaining: Remaining | None) -> dict[str, object] | None:
    if remaining is None:
        return None
    parts = _still_parts(remaining)
    total = sum(item[1] for item in parts) if parts else None
    fraction = None
    if len(parts) == 2 and total is not None:
        usable = _wh(remaining.husky_ah * NOMINAL_V) + _wh(remaining.solix_ah * NOMINAL_V)
        fraction = _round(total / usable, 3)
    return {
        "solix": _round(remaining.solix_wh, 1),
        "husky": _round(remaining.husky_wh, 1),
        "total": None if total is None else float(total),
        "fraction_of_shared": fraction,
    }


def _runtime(lines: list[str], analysis: Analysis, *, verbose: bool) -> None:
    lines.append("")
    if verbose:
        lines.append("Runtime of the watt-hours still in the packs")
        lines.append("  DC watt-hours times a 90% inverter, divided by the AC load.")
    else:
        lines.append("Shared runtime")
        _shared_runtime(lines, analysis)
        return
    if not analysis.runtimes:
        lines.append("  no remaining energy")
        return
    lines.append(f"  {'load':>8}  {'both packs':>10}  {'Solix pack':>12}")
    for row in analysis.runtimes:
        mark = "  logged" if row.logged else ""
        lines.append(
            f"  {_watts(row.load_w):>8}  {_hour(row.combined_h):>10}  {_hour(row.solix_h):>12}{mark}"
        )
    if analysis.logged_load_w is None:
        lines.append("  No stretch in the log has the batteries carrying the AC load.")


def _shared_runtime(lines: list[str], analysis: Analysis) -> None:
    """Example loads only. Full pack hours beside hours at the watt-hours still in the packs."""

    rows = [row for row in analysis.runtimes if not row.logged]
    if not rows:
        lines.append("  no combined estimate")
        return
    lines.append(f"  {'':>8}  {'full':>8}  {'remaining':>10}")
    for row in rows:
        full_h = _hours(*_usable_watt_hours(analysis), row.load_w)
        lines.append(f"  {_watts(row.load_w):>8}  {_hour(full_h):>8}  {_hour(row.combined_h):>10}")


def _when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _ah(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}"
    return f"{value:.1f}"


def _pct(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}%"
    return f"{value:.1f}%"


def _span(seconds: float) -> str:
    minutes = int(round(seconds / 60.0))
    if minutes < 90:
        return f"{minutes} min"
    return f"{seconds / 3600.0:.1f} h"


def _wh(value: float) -> int:
    return int(round(value))


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
        "usable_ah": _round(count.usable_ah, 2),
        "at_empty": count.at_empty,
    }


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)
