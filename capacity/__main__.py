"""Report capacity and runtime from the collector logs.

    python -m capacity
    python -m capacity --load 500
    python -m capacity --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from capacity.estimate import LOADS_W, MIN_SOC_POINTS, analyze, load_logs, render, to_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capacity",
        description=(
            "Read data/**/readings.ndjson and estimate combined capacity and "
            "runtime. The Solix percentage is its own pack. Husky watt-hours "
            "are counted from a rested full cell. The Husky percentage is not used."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="root of the year/month/day log folders",
    )
    parser.add_argument(
        "--load",
        type=float,
        action="append",
        default=None,
        help="extra AC load in watts. Repeat for more than one. The usual loads stay.",
    )
    parser.add_argument(
        "--min-soc",
        type=float,
        default=MIN_SOC_POINTS,
        help="percentage points a continuous Solix stretch must move before inferred amp-hours are reported",
    )
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.data_dir)
    samples, skipped = load_logs(root)
    if not samples:
        print(f"no readings under {root}", file=sys.stderr)
        return 1
    loads = tuple(LOADS_W)
    if args.load:
        loads = tuple(dict.fromkeys([*loads, *args.load]))
    report = analyze(samples, skipped_lines=skipped, loads_w=loads, min_soc_points=args.min_soc)
    if args.json:
        print(json.dumps(to_json(report), indent=2))
    else:
        print(render(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
