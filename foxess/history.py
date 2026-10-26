"""Half-hourly history of the live power.

The inverter only reports lifetime counters and an instantaneous power
reading, so the shape of a day has to be recorded locally. Each sample is
integrated into a half-hour bucket, which gives both the daily profile for
the usage chart and the "today" totals.

It also gives the solar calibration something to work with when the Open
API is not available - the live feed carries the solar power, and
integrating it over a day gives that day's yield.

Kept as a small JSON file next to the settings.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The keys we track, and the live field each one comes from.
CHANNELS = {
    "pv": "pvPower",
    "house": "loadsPower",
    "grid_in": "gridConsumptionPower",
    "grid_out": "feedinPower",
    "batt_in": "batChargePower",
    "batt_out": "batDischargePower",
}

SLOT = 1800.0     # half an hour, in seconds

#: The format of the history file.  Version 1 was in watt-hours rather
#: than kWh, so those numbers were a thousand times too big - they are
#: discarded rather than mixed in with the real ones.  Version 2 divided
#: the live power by a thousand as well, but the live feed is already in
#: kW, so its numbers came out a thousand times too small - those are
#: discarded in the same way.
HISTORY_VERSION = 3


def slot_start(when: float) -> float:
    """The start of the half-hour bucket *when* falls in."""
    return float(int(when // SLOT) * SLOT)


class HalfHourHistory:
    """Accumulates live power samples into half-hour energy buckets."""

    def __init__(self, path: str | os.PathLike | None = None,
                 days: int = 21) -> None:
        self.path = Path(path) if path else None
        self.days = days
        self._lock = threading.Lock()
        self._buckets: dict[float, dict[str, float]] = {}
        self._last_sample: float | None = None
        self._last_values: dict[str, float] = {}
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if raw.get("version") != HISTORY_VERSION:
            # an old file, or one written before the units were fixed -
            # start again rather than mixing the two scales
            return
        buckets = raw.get("buckets") or {}
        for key, values in buckets.items():
            try:
                self._buckets[float(key)] = {k: float(v) for k, v in values.items()}
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        if not self.path:
            return
        cutoff = time.time() - self.days * 86400
        data = {
            "version": HISTORY_VERSION,
            "buckets": {
                str(int(k)): {kk: round(vv, 5) for kk, vv in v.items()}
                for k, v in self._buckets.items() if k >= cutoff
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- recording --------------------------------------------------------

    def add(self, live: dict[str, float], when: float | None = None) -> None:
        """Integrate one live sample into the buckets."""
        now = when if when is not None else time.time()
        with self._lock:
            if self._last_sample is None:
                self._last_sample = now
                self._last_values = {k: float(live.get(f, 0) or 0)
                                     for k, f in CHANNELS.items()}
                return
            dt = max(0.0, min(now - self._last_sample, SLOT * 2)) / 3600.0
            self._last_sample = now
            if dt <= 0:
                return
            slot = slot_start(now)
            bucket = self._buckets.setdefault(slot, {})
            for key, field in CHANNELS.items():
                value = float(live.get(field, 0) or 0)
                # the live power is in kW and dt is in hours, so this is
                # kWh - the same unit as the rest of the dashboard.  The
                # feed was in watts once, and the extra thousand is what
                # made the chart come out a thousand times too small.
                bucket[key] = bucket.get(key, 0.0) + value * dt
                self._last_values[key] = value
            # keep the file from growing without bound
            cutoff = now - self.days * 86400
            for old in [k for k in self._buckets if k < cutoff]:
                del self._buckets[old]
            self._save()

    # -- reading ----------------------------------------------------------

    def series(self, count: int = 48) -> list[dict]:
        """The last *count* half-hour buckets, oldest first."""
        now = time.time()
        with self._lock:
            slots = sorted(k for k in self._buckets if k <= now)
            slots = slots[-count:]
            out = []
            for slot in slots:
                values = self._buckets.get(slot, {})
                out.append({
                    "start": datetime.fromtimestamp(slot, timezone.utc).isoformat(),
                    "house": values.get("house", 0.0),
                    "pv": values.get("pv", 0.0),
                    "grid_in": values.get("grid_in", 0.0),
                    "grid_out": values.get("grid_out", 0.0),
                    "batt_in": values.get("batt_in", 0.0),
                    "batt_out": values.get("batt_out", 0.0),
                })
            return out

    def today(self, timezone_name: str = "Europe/London") -> dict[str, float]:
        """Today's totals, from midnight local time."""
        try:
            from zoneinfo import ZoneInfo
            local = datetime.now(ZoneInfo(timezone_name))
        except Exception:  # noqa: BLE001
            local = datetime.now(timezone.utc)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = midnight.timestamp()
        with self._lock:
            totals: dict[str, float] = {k: 0.0 for k in CHANNELS}
            for slot, values in self._buckets.items():
                if slot < cutoff:
                    continue
                for key in totals:
                    totals[key] += values.get(key, 0.0)
        return totals

    def daily_pv(self, timezone_name: str = "Europe/London",
                 days: int = 21) -> dict[str, float]:
        """The solar energy of each day, in kWh, from the live feed.

        The inverter's own daily register is better where it exists, but
        it needs the Open API - and this does not.  The live feed carries
        the solar power, and integrating it over the day gives the yield.

        The day is the one in the plant's timezone, so that the days line
        up with the inverter's own register and with the forecast.
        """
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(timezone_name)
        except Exception:  # noqa: BLE001
            zone = timezone.utc
        cutoff = (datetime.now(zone) - timedelta(days=days)).date()
        out: dict[str, float] = {}
        with self._lock:
            for slot, values in self._buckets.items():
                day = datetime.fromtimestamp(slot, zone).date()
                if day < cutoff:
                    continue
                key = day.isoformat()
                out[key] = out.get(key, 0.0) + values.get("pv", 0.0)
        return {day: round(value, 3)
                for day, value in sorted(out.items()) if value > 0}
