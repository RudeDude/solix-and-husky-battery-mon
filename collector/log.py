"""Append NDJSON into data/year/month/day, rolling at local midnight."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def day_path(root: Path, moment: datetime) -> Path:
    """Return ``root/YYYY/MM/DD/readings.ndjson`` for the local date of ``moment``."""

    return (
        root
        / f"{moment.year:04d}"
        / f"{moment.month:02d}"
        / f"{moment.day:02d}"
        / "readings.ndjson"
    )


class DayLog:
    """One open day file. A new local date opens the next day's file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path: Path | None = None
        self._day: tuple[int, int, int] | None = None
        self._handle = None

    def write(self, moment: datetime, record: dict[str, object]) -> Path:
        day = (moment.year, moment.month, moment.day)
        if self._handle is None or self._day != day:
            self.close()
            path = day_path(self.root, moment)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")
            self._day = day
            self.path = path
        assert self._handle is not None and self.path is not None
        self._handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._handle.flush()
        return self.path

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
