"""Persistent configuration for the FoxESS dashboard.

Settings live in a JSON file so they survive restarts - which matters when
the dashboard runs unattended on a Pi or an ESP32.

Resolution order for the config file location:

1. ``FOXESS_DASHBOARD_CONFIG`` environment variable
2. ``./config.json`` next to the project
3. ``~/.foxess-dashboard/config.json``

Secrets (password, API key) are written with owner-only permissions and are
never returned in plaintext by the settings API - the UI sends back a mask
placeholder when a secret is left unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

#: Marker the UI sends back for a secret field the user did not change.
KEEP = "__KEEP__"

#: Build stamp, so it is obvious at a glance which version is running.
BUILD = "2026-10-29.1"

#: What the API returns in place of a secret.
MASK = "********"

SECRET_PATHS = (
    "foxess.password",
    "foxess.api_key",
    "octopus.api_key",
    "octopus.password",
)

DEFAULTS: dict[str, Any] = {
    "foxess": {
        "user": "",
        "password": "",
        "api_key": "",
        "domain": "https://www.foxesscloud.com",
        "sn": "",
        "plant_id": "",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "auto_open": False,
    },
    "api_interval": 300,
    "live_enabled": True,
    "api_enabled": True,
    "octopus": {
        "api_key": "",
        "email": "",
        "password": "",
        "account_number": "",
        "mpan": "",
        "meter_serial": "",
        "export_mpan": "",
        "export_meter_serial": "",
        "tariff": "flux",
    },
    "display": {
        "theme": "dark",          # dark | light | auto
        "units": "kW",            # kW | W
        "clock": "24h",           # 24h | 12h
        "timezone": "auto",       # auto | Europe/London | ...
        "layout": "auto",         # auto | full | compact | tiny
        "diagram": "rings",       # rings | flow
        "show_totals": True,
        "show_system": True,
        "show_soc": True,
        "poll_ms": 1000,
        "slides": ["power", "usage", "weather"],   # which views to rotate through
        "slide_interval": 20,                     # seconds, 0 = no rotation
        "rotate_ip": "",          # optional second address for a split-brain display
    },
    "weather": {
        "location": "",           # the place, as it is shown
        "latitude": "",           # found with the Find button
        "longitude": "",
        "temperature_unit": "celsius",   # celsius | fahrenheit
        "wind_speed_unit": "kmh",        # kmh | ms | mph | kn
        "tilt": 35,               # the roof's angle from the horizontal
        "azimuth": 0,             # 0 is south, +90 west, -90 east
        "performance_ratio": 0.85,       # used to turn the measured factor into kWp
        "array_kwp": "",          # optional: the size you know, to check the fit
    },
}

#: Settings the settings page is allowed to change.
EDITABLE = {
    "foxess.user",
    "foxess.password",
    "foxess.api_key",
    "foxess.domain",
    "foxess.sn",
    "foxess.plant_id",
    "api_interval",
    "live_enabled",
    "api_enabled",
    "display.theme",
    "display.units",
    "display.clock",
    "display.timezone",
    "display.layout",
    "display.diagram",
    "display.show_totals",
    "display.show_system",
    "display.show_soc",
    "display.poll_ms",
    "display.slides",
    "display.slide_interval",
    "octopus.api_key",
    "octopus.email",
    "octopus.password",
    "octopus.account_number",
    "octopus.mpan",
    "octopus.meter_serial",
    "octopus.export_mpan",
    "octopus.export_meter_serial",
    "octopus.tariff",
    "weather.location",
    "weather.latitude",
    "weather.longitude",
    "weather.temperature_unit",
    "weather.wind_speed_unit",
    "weather.tilt",
    "weather.azimuth",
    "weather.performance_ratio",
    "weather.array_kwp",
}


def default_config_path() -> Path:
    env = os.environ.get("FOXESS_DASHBOARD_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path(__file__).resolve().parent.parent / "config.json"
    if local.parent.exists():
        return local
    return Path.home() / ".foxess-dashboard" / "config.json"


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge *patch* into a copy of *base*."""
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def get_path(config: dict, dotted: str, default: Any = None) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(config: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class ConfigStore:
    """Thread-safe JSON-backed settings store."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path).expanduser() if path else default_config_path()
        self._lock = threading.RLock()
        self._config = copy.deepcopy(DEFAULTS)
        self.load()

    # -- persistence -----------------------------------------------------

    def load(self) -> dict:
        with self._lock:
            data: dict = {}
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = {}
            self._config = deep_merge(DEFAULTS, data if isinstance(data, dict) else {})
            return copy.deepcopy(self._config)

    def save(self, updates: dict) -> dict:
        """Merge *updates* (dotted paths) and persist to disk atomically."""
        with self._lock:
            merged = copy.deepcopy(self._config)
            for dotted, value in (updates or {}).items():
                if dotted not in EDITABLE:
                    continue
                if dotted in SECRET_PATHS:
                    if value in (None, "", KEEP, MASK):
                        continue          # leave the stored secret alone
                set_path(merged, dotted, value)
            self._config = merged
            self._write(merged)
            return copy.deepcopy(self._config)

    def _write(self, config: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # 0600 - owner only
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- access ----------------------------------------------------------

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._config)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            return get_path(self._config, dotted, default)

    def masked(self) -> dict:
        """Config with secrets replaced by a mask - safe to send to a browser."""
        out = self.all()
        for dotted in SECRET_PATHS:
            value = get_path(out, dotted, "")
            if value:
                set_path(out, dotted, MASK)
        return out

    def has_secret(self, dotted: str) -> bool:
        return bool(self.get(dotted, ""))

    def path_str(self) -> str:
        return str(self.path)
