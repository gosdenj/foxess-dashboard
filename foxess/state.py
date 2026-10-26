"""Shared dashboard state.

Combines two data sources into a single snapshot for the UI:

* ``live``   - the WebSocket feed (~5 s resolution), live power and SoC
* ``totals`` - the Open API (300 s resolution), cumulative energy counters

The current settings travel with every snapshot so that every connected
display (browser, Pi kiosk, ESP32 client) picks up a settings change without
being restarted.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .config import BUILD

#: Cumulative counters worth showing in the UI.
TOTAL_VARIABLES = (
    "generation",          # inverter AC output, lifetime (kWh)
    "PVEnergyTotal",       # PV energy captured, lifetime (kWh)
    "loads",               # household consumption, lifetime (kWh)
    "feedin",              # exported to grid, lifetime (kWh)
    "gridConsumption",     # imported from grid, lifetime (kWh)
    "chargeEnergyToTal",   # energy charged into the battery, lifetime (kWh)
    "dischargeEnergyToTal",# energy discharged from the battery, lifetime (kWh)
    "batCycleCount",       # charge cycles
    "SOH",                 # state of health (%)
    "totalDischargeKW",    # lifetime discharge energy (kWh)
    "batDischargeEnergyToTal",
)


class DashboardState:
    """Thread-safe container for everything the dashboard displays."""

    def __init__(self, config=None):
        self._lock = threading.RLock()
        self._live: dict[str, float] = {}
        self._totals: dict[str, float] = {}
        self._meta: dict[str, Any] = {}
        self._live_update: float | None = None
        self._api_update: float | None = None
        self._api_error: str | None = None
        self._connected = False
        self._messages = 0
        self._config = config
        self._slide_names: list[str] = []
        self._slide_index = 0
        self._octopus: dict[str, Any] = {}
        self._mini: dict[str, Any] = {}
        self._usage: dict[str, Any] = {}
        self._daily: dict[str, Any] = {}
        self._weather: dict[str, Any] = {}

    # -- setters ---------------------------------------------------------

    def set_live(self, data: dict[str, float]) -> None:
        with self._lock:
            self._live = dict(data)
            self._live_update = time.time()
            self._connected = True
            self._messages += 1

    def set_live_status(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._connected = connected
            if error is not None:
                self._api_error = error

    def set_totals(self, totals: dict[str, float]) -> None:
        with self._lock:
            self._totals = dict(totals)
            self._api_update = time.time()

    def set_meta(self, **kwargs: Any) -> None:
        with self._lock:
            self._meta.update(kwargs)

    def set_api_error(self, message: str | None) -> None:
        with self._lock:
            self._api_error = message

    # -- slides ----------------------------------------------------------

    def set_slides(self, names: list[str]) -> None:
        """Set the rotation list, keeping the current slide if it survives."""
        with self._lock:
            names = [n for n in (names or []) if n]
            if not names:
                names = ["power"]
            current = (self._slide_names[self._slide_index]
                       if self._slide_names else None)
            self._slide_names = names
            if current in names:
                self._slide_index = names.index(current)
            else:
                self._slide_index = 0

    def advance_slide(self) -> None:
        """Move to the next slide in the rotation."""
        with self._lock:
            if len(self._slide_names) > 1:
                self._slide_index = (self._slide_index + 1) % len(self._slide_names)

    def set_slide(self, name: str) -> None:
        """Jump to a named slide, if it is in the rotation."""
        with self._lock:
            if name in self._slide_names:
                self._slide_index = self._slide_names.index(name)

    def slide(self) -> dict[str, Any]:
        with self._lock:
            names = self._slide_names or ["power"]
            return {
                "index": self._slide_index,
                "name": names[self._slide_index],
                "count": len(names),
                "names": list(names),
            }

    # -- octopus ---------------------------------------------------------

    def set_octopus(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._octopus = dict(data)

    def set_mini(self, data: dict[str, Any]) -> None:
        """The Octopus Mini's live meter data."""
        with self._lock:
            self._mini = dict(data)

    def set_usage(self, data: dict[str, Any]) -> None:
        """The locally recorded half-hour history. Updated with the live
        feed, not on the slow Octopus poll."""
        with self._lock:
            self._usage = dict(data)

    def set_daily(self, data: dict[str, Any]) -> None:
        """The inverter's own daily totals, from the production report."""
        with self._lock:
            self._daily = dict(data)

    def set_weather(self, data: dict[str, Any]) -> None:
        """The weather and the solar forecast."""
        with self._lock:
            self._weather = dict(data)

    # -- readers ---------------------------------------------------------

    def settings(self) -> dict:
        if self._config is None:
            return {}
        try:
            return self._config.masked()
        except Exception:  # noqa: BLE001
            return {}

    def meta(self) -> dict[str, Any]:
        """What the cloud says about the plant."""
        with self._lock:
            return dict(self._meta)

    def octopus(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._octopus)

    def mini(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._mini)

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._usage)

    def daily(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._daily)

    def weather(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._weather)

    def wait_for_daily(self, timeout: float = 120.0) -> dict[str, Any]:
        """Wait for the inverter's daily report, if it is coming.

        The Octopus figures fall back to that report when the meter has
        no readings for today, so it is worth a short wait before the
        first pass - otherwise the panel shows a zero for five minutes.
        """
        end = time.time() + timeout
        while time.time() < end:
            daily = self.daily()
            if daily.get("totals") or daily.get("error"):
                return daily
            time.sleep(2.0)
        return self.daily()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            live = dict(self._live)
            totals = dict(self._totals)
            meta = dict(self._meta)
            live_age = (now - self._live_update) if self._live_update else None
            api_age = (now - self._api_update) if self._api_update else None
            return {
                "live": live,
                "totals": totals,
                "meta": meta,
                "settings": self.settings(),
                "build": BUILD,
                "slide": self.slide(),
                "octopus": self.octopus(),
                "mini": self.mini(),
                "usage": self.usage(),
                "daily": self.daily(),
                "weather": self.weather(),
                "connected": self._connected,
                "messages": self._messages,
                "live_age_s": round(live_age, 1) if live_age is not None else None,
                "api_age_s": round(api_age, 1) if api_age is not None else None,
                "api_error": self._api_error,
                "server_time": now,
            }
