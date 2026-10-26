#!/usr/bin/env python3
"""Standalone FoxESS dashboard.

    Live power + SoC from the FoxESS WebSocket   (~5 second resolution)
    Cumulative energy totals from the Open API    (~300 second resolution)

Both sources are optional - the dashboard renders whatever it can get.

Settings are stored in a JSON file (see foxess/config.py) and can be edited
in the browser at ``/settings``.  Environment variables are used only to seed
values that are still empty, so anything you set in the UI sticks.

Usage:
    python3 run.py                 # normal
    python3 run.py --demo          # synthetic data, no credentials needed
    python3 run.py --config /etc/foxess.json
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    ZoneInfo = None
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foxess.history import HalfHourHistory  # noqa: E402
from foxess.octopus import OctopusClient, OctopusMini, OctopusError, today_start  # noqa: E402
from foxess.api import (  # noqa: E402
    EU_DOMAIN,
    US_DOMAIN,
    FoxESSApi,
    FoxESSError,
    device_timestamp,
    report_values,
    variables_from_real_time,
)
from foxess.config import ConfigStore  # noqa: E402
from foxess.server import serve  # noqa: E402
from foxess.state import TOTAL_VARIABLES, DashboardState  # noqa: E402
from foxess import weather  # noqa: E402
from foxess.ws import FoxESSLive  # noqa: E402

#: Datalogger type is encoded in the module serial number.
DATALOGGER_BY_ID = {
    "WLan-WW (WiFi & LAN)": ("9WW", "9WL", "9WX"),
    "SMART WB (WiFi)": ("9W6", "9W7"),
    "SMART WB-2 (WiFi)": ("9Q1",),
    "SMART W (WiFi)": ("9W3", "9W2"),
    "Smart-Wifi-M (WiFi)": ("9M3",),
    "SMART 4G V4.0 EU": ("9E4",),
    "SMART 4G V4.1 EU": ("9E5",),
    "Smart-4G-M (4G)": ("9M1",),
    "SMART 4G-2 (4G)": ("9Q2",),
}


def identify_datalogger(module_sn: str) -> str:
    for name, ids in DATALOGGER_BY_ID.items():
        if any(i in (module_sn or "") for i in ids):
            return name
    return "Integrated / unknown"


class Controller:
    """Owns the live feed and the Open API poller, and can re-wire them."""

    def __init__(self, config: ConfigStore, state: DashboardState,
                 history: HalfHourHistory | None = None):
        self.config = config
        self.state = state
        self.history = history
        self.api = FoxESSApi(self._domain())
        self.stop = threading.Event()          # everything
        self._api_stop = threading.Event()     # just the open api poller
        self.octopus_wake = threading.Event()  # wake the octopus worker early
        self.weather_wake = threading.Event()  # wake the weather worker early
        self.live: FoxESSLive | None = None
        self._live_thread: threading.Thread | None = None
        self._api_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._prev_pv: list[dict] = []      # the month before, for the solar fit
        self._prev_pv_month: str = ""

    # -- helpers ---------------------------------------------------------

    def _domain(self) -> str:
        domain = self.config.get("foxess.domain", EU_DOMAIN) or EU_DOMAIN
        return US_DOMAIN if "portal.foxesscloud.us" in domain else domain

    def _api_key(self) -> str:
        return self.config.get("foxess.api_key", "") or ""

    def _live_enabled(self) -> bool:
        return bool(self.config.get("live_enabled", True))

    def _api_enabled(self) -> bool:
        return bool(self.config.get("api_enabled", True))

    # -- setup -----------------------------------------------------------

    def setup(self) -> None:
        """Discover the inverter and power station from the Open API."""
        key = self._api_key()
        if not key or not self._api_enabled():
            print("no API key - totals disabled")
            return
        try:
            devices = self.api.device_list(key)
            if not devices:
                raise FoxESSError("no inverters on this account")
            device = devices[0]
            sn = device.get("deviceSN")
            plants = self.api.plant_list(key)
            plant_id = plants[0].get("stationID") if plants else None

            detail = self.api.device_detail(key, sn)
            modules = self.api.module_list(key)
            module = next((m for m in modules
                           if m.get("moduleSN") == device.get("moduleSN")), {})
            soc = self.api.battery_soc(key, sn)
            quota = self.api.access_count(key)

            # Persist anything we had to discover, so the user can see it.
            self.config.save({"foxess.sn": sn, "foxess.plant_id": plant_id})

            self.state.set_meta(
                plant_name=plants[0].get("name") if plants else None,
                plant_id=plant_id,
                plant_timezone=plants[0].get("ianaTimezone") if plants else None,
                inverter_sn=sn,
                module_sn=device.get("moduleSN"),
                datalogger=identify_datalogger(device.get("moduleSN") or ""),
                datalogger_version=module.get("softVersion"),
                signal=module.get("signal"),
                manager_version=detail.get("managerVersion"),
                master_version=detail.get("masterVersion"),
                slave_version=detail.get("slaveVersion"),
                battery_units=len(detail.get("batteryList") or []),
                battery_types=[b.get("type") for b in (detail.get("batteryList") or [])],
                min_soc=soc.get("minSoc"),
                min_soc_on_grid=soc.get("minSocOnGrid"),
                quota=quota,
                api_known=True,
            )
            print(f"open api ok - inverter {sn}, plant {plant_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"open api setup failed: {exc}")
            self.state.set_api_error(str(exc))

    # -- workers ---------------------------------------------------------

    def start(self) -> None:
        if self._api_enabled() and self._api_key():
            thread = threading.Thread(target=self._api_worker, daemon=True,
                                      name="foxess-api")
            thread.start()
            self._api_thread = thread
        if self._live_enabled():
            self.start_live()

    def record_live(self, data: dict) -> None:
        """Take a live reading: publish it, and record it in the history.

        The history is what fills the usage chart and the daily totals, so
        a reading that only reaches the state leaves the usage slide
        empty.
        """
        self.state.set_live(data)
        history = self.history
        if history is not None:
            history.add(data)
            daily = self.state.daily()
            payload = usage_payload(history, daily)
            self.state.set_usage(payload)
            if "live_pv" in payload:
                daily["live_pv"] = payload["live_pv"]
                daily["pv_yield_difference"] = payload["pv_yield_difference"]
                self.state.set_daily(daily)

    def start_live(self) -> None:
        user = self.config.get("foxess.user", "") or ""
        password = self.config.get("foxess.password", "") or ""
        plant_id = self.config.get("foxess.plant_id", "") or ""
        if not (user and password and plant_id):
            print("no user/password or no plant id - live feed disabled")
            return
        def on_live(data):
            self.record_live(data)

        live = FoxESSLive(user, password, plant_id, on_update=on_live)
        thread = threading.Thread(target=live._run, daemon=True, name="foxess-ws")
        thread.start()
        self.live = live
        self._live_thread = thread
        print(f"websocket starting for plant {plant_id}")

    def _api_worker(self) -> None:
        key = self._api_key()
        sn = self.config.get("foxess.sn", "") or ""
        if not key or not sn:
            return
        stop = self._api_stop
        while not stop.is_set():
            interval = int(self.config.get("api_interval", 300) or 300)
            try:
                result = self.api.real_time(key, [sn])
                flat = variables_from_real_time(result)
                self.state.set_totals({k: flat[k] for k in TOTAL_VARIABLES if k in flat})
                self.state.set_api_error(None)
                tz = self.config.get("display.timezone", "auto")
                if tz in ("", "auto", None):
                    tz = "Europe/London"
                daily = collect_daily(self.api, key, sn, tz)
                # The solar forecast is calibrated against the inverter's
                # own daily yield, and the window has to reach back past
                # the turn of the month - so the month before is fetched
                # once and kept.
                self._remember_previous_pv(key, sn, tz)
                if self._prev_pv:
                    daily["pv_daily"] = self._prev_pv + (daily.get("pv_daily") or [])
                # the octopus figures fall back to this report, so wake the
                # octopus worker rather than leaving the panel showing
                # yesterday's numbers for another five minutes
                if daily.get("totals"):
                    self.octopus_wake.set()
                if daily.get("error"):
                    print(f"daily report failed: {daily['error']}")
                else:
                    print("daily report: " + ", ".join(
                        f"{k} {v:.2f}" for k, v in
                        (daily.get("totals") or {}).items()) or "empty")
                    for name, c in (daily.get("check") or {}).items():
                        if c.get("difference") is None:
                            print(f"  {name}: {c['monthly']:.2f} kWh")
                            continue
                        print(f"  {name}: month {c['monthly']:.2f} vs "
                              f"hours {c['hourly']:.2f} "
                              f"(difference {c['difference']:+.2f})")
                    if daily.get("pv_yield") is not None:
                        print(f"  pv yield today: "
                              f"{daily['pv_yield']:.2f} kWh")
                    if daily.get("live_pv") is not None:
                        print(f"  pv from the live feed: "
                              f"{daily['live_pv']:.2f} kWh "
                              f"(difference {daily.get('pv_yield_difference', 0.0):+.2f})")
                    if daily.get("balance") is not None:
                        print(f"  balance: {daily['balance']:+.2f} kWh "
                              f"(the sun, the battery and the grid in, less "
                              f"the house, the battery and the grid out)")
                self.state.set_daily(daily)
                self.state.set_usage(usage_payload(self.history, daily))
                # The solar forecast is calibrated against that report, so
                # it is worth fitting again now rather than waiting for the
                # next weather poll.
                self.weather_wake.set()
                stamp = device_timestamp(result)
                if stamp:
                    self.state.set_meta(last_api_timestamp=stamp)
            except Exception as exc:  # noqa: BLE001
                self.state.set_api_error(f"{type(exc).__name__}: {exc}")
            stop.wait(interval)

    # -- settings --------------------------------------------------------

    def _remember_previous_pv(self, key: str, sn: str, tz: str) -> None:
        """Keep the month before this one, for the solar calibration.

        It is fetched once and then left alone until the month turns.
        """
        local = _local_now(tz)
        stamp = f"{local.year:04d}-{local.month:02d}"
        if stamp == self._prev_pv_month:
            return
        rows = collect_previous_pv(self.api, key, sn, tz)
        if rows:
            self._prev_pv = rows
            self._prev_pv_month = stamp
            print(f"previous month's solar yield: {len(rows)} days")

    def apply_settings(self, saved: dict) -> None:
        """Re-wire live connections after a settings change."""
        with self._lock:
            # The saved settings are a flat dict of paths, and a blank or
            # masked field means "keep what is stored" - so neither counts
            # as a change.
            def changed(key: str) -> bool:
                value = saved.get(key)
                if value in (None, "", "********", "__KEEP__"):
                    return False
                return value != self.config.get(key)

            changed_live = any(changed(f"foxess.{k}")
                               for k in ("user", "password", "plant_id"))
            if changed_live and self._live_thread is not None:
                print("credentials changed - restarting the live feed")
                if self.live is not None:
                    self.live.stop()
                self._live_thread = None
                self.live = None
                self.start_live()
            if (changed_live
                    or ("api_enabled" in saved
                        and saved["api_enabled"] != self.config.get("api_enabled"))):
                print("open api settings changed - restarting the poller")
                self._api_stop.set()
                time.sleep(0.2)
                self._api_stop = threading.Event()
                if self._api_enabled() and self._api_key():
                    thread = threading.Thread(target=self._api_worker, daemon=True,
                                              name="foxess-api")
                    thread.start()
                    self._api_thread = thread
            if any(k.startswith("octopus.") for k in saved):
                # the octopus worker reads the settings each time round, so
                # it just needs waking up
                print("octopus settings changed - refreshing")
                self.octopus_wake.set()
            if any(k.startswith("weather.") for k in saved):
                # the same for the weather - a new place, a new unit or a
                # new roof angle all need a fresh forecast
                print("weather settings changed - refreshing")
                self.weather_wake.set()

    def shutdown(self) -> None:
        self.stop.set()
        self._api_stop.set()
        self.octopus_wake.set()
        if self.live is not None:
            self.live.stop()



class SlideRotator:
    """Advances the slide every N seconds.

    The slide index lives in the server, not in the settings file, so the
    settings are not rewritten every time the display changes - and so that
    every connected display shows the same slide at the same moment.
    """

    def __init__(self, state: DashboardState, config: ConfigStore) -> None:
        self.state = state
        self.config = config
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="slides")

    def start(self) -> None:
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()

    def _interval(self) -> int:
        """The rotation interval, in seconds. 0 means no rotation."""
        try:
            return int(self.config.get("display.slide_interval", 20) or 0)
        except (TypeError, ValueError):
            return 20

    def run(self) -> None:
        while not self.stop.is_set():
            names = self.config.get("display.slides", ["power"]) or ["power"]
            if isinstance(names, str):
                names = [names]
            self.state.set_slides(list(names))
            interval = self._interval()
            if interval <= 0:
                self.stop.wait(3)
                continue
            deadline = time.time() + interval
            changed = False
            while time.time() < deadline and not self.stop.is_set():
                self.stop.wait(0.5)
                # The interval may have been changed in the settings.  Stop
                # there and then, and do not move the slide on the way
                # out - the user has just put it where they want it.
                if self._interval() != interval:
                    changed = True
                    break
            if not self.stop.is_set() and not changed:
                self.state.advance_slide()


#: The production report's variables, and what the dashboard calls them.
REPORT_VARIABLES = ["generation", "loads", "gridConsumption", "feedin",
                     "chargeEnergyToTal", "dischargeEnergyToTal",
                     "PVEnergyTotal"]
#: "generation" is the inverter's AC output, which for a hybrid includes
#: what the battery sent out.  "PVEnergyTotal" is the PV yield - the
#: figure the Fox app shows.  It is a running total, so today's yield is
#: the difference between today and yesterday.
PV_TOTAL_VARIABLE = "PVEnergyTotal"
REPORT_NAMES = {
    "generation": "pv",
    "loads": "house",
    "gridConsumption": "grid_in",
    "feedin": "grid_out",
    "chargeEnergyToTal": "batt_in",
    "dischargeEnergyToTal": "batt_out",
}


def _pv_daily_series(values, year: int, month: int, upto: int) -> list[dict]:
    """The daily PV yields of a month, as {date, kwh}.

    The report's values are positional - the first is the first of the
    month - so the day of the month is the index.
    """
    out = []
    for index, value in enumerate(values or []):
        day = index + 1
        if day > upto:
            break
        if not isinstance(value, (int, float)):
            continue
        try:
            date = datetime(year, month, day).date().isoformat()
        except ValueError:
            continue
        out.append({"date": date, "kwh": round(float(value), 3)})
    return out


def collect_previous_pv(api, key: str, sn: str,
                        timezone_name: str = "Europe/London") -> list[dict]:
    """The daily PV yields of the month before this one.

    The solar forecast is calibrated by putting the inverter's yield next
    to the sun that the weather service says arrived on the same day, so
    the window has to reach back past the turn of the month.  Only the
    first 28 days are asked for, which covers the fortnight either side.
    """
    local = _local_now(timezone_name)
    year, month = local.year, local.month
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    try:
        report = api.report_query(key, sn, "month", year, month, 28,
                                   [PV_TOTAL_VARIABLE])
    except Exception as exc:  # noqa: BLE001
        print(f"previous month's report failed: {exc}")
        return []
    yields = report_values(report, PV_TOTAL_VARIABLE)
    # every day of that month, not just the first 28 - the reply may
    # carry the whole month
    try:
        last = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30
        if month == 2:
            leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
            last = 29 if leap else 28
    except Exception:  # noqa: BLE001
        last = 28
    return _pv_daily_series(yields, year, month, last)


def _local_now(timezone_name: str = "Europe/London") -> datetime:
    """Now, in the plant's own timezone."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def collect_daily(api, key: str, sn: str,
                  timezone_name: str = "Europe/London") -> dict:
    """Today's totals from the inverter's own production report.

    The report is the inverter's own daily register, so it is right even
    when the dashboard has only just started and has no history of its
    own.  The monthly report is used rather than the daily one, because
    the daily one is known to be a little wrong at the edges of the day.

    The hourly report is fetched as well, and the two are compared: the
    hours that have gone by added up should match the day in the
    monthly register.  When they do not, one of them is wrong and it is
    worth knowing which.
    """
    try:
        from zoneinfo import ZoneInfo
        local = datetime.now(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001
        local = datetime.now(timezone.utc)
    out: dict = {"totals": {}}
    try:
        report = api.report_query(key, sn, "month", local.year, local.month,
                                   local.day, REPORT_VARIABLES)
        index = local.day - 1
        for variable, name in REPORT_NAMES.items():
            values = report_values(report, variable)
            if index < len(values):
                value = values[index]
                if isinstance(value, (int, float)):
                    out["totals"][name] = float(value)

        # The PV yield for the day.  The report's values are daily
        # figures, not a running counter - subtracting yesterday's from
        # today's gives a nonsense number.
        yields = report_values(report, PV_TOTAL_VARIABLE)
        if index < len(yields) and isinstance(yields[index], (int, float)):
            out["pv_yield"] = round(float(yields[index]), 3)
        # The whole month, day by day.  This is what the solar forecast
        # is calibrated against: the inverter's own yield, next to the
        # sun that the weather service says landed on the roof.
        out["pv_daily"] = _pv_daily_series(yields, local.year, local.month, local.day)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # The PV yield is the figure the Fox app shows, so it is the one the
    # dashboard uses for "solar generated today".
    if "pv_yield" in out:
        out["totals"]["pv"] = out["pv_yield"]

    # The hourly report: one value per hour of today, so the hours that
    # have gone by can be added up and compared with the monthly
    # register.
    hours_gone = local.hour + 1
    try:
        hourly = api.report_query(key, sn, "day", local.year, local.month,
                                  local.day, REPORT_VARIABLES)
        hourly_totals = {}
        for variable, name in REPORT_NAMES.items():
            values = report_values(hourly, variable)
            taken = [v for v in values[:hours_gone]
                     if isinstance(v, (int, float))]
            if taken:
                hourly_totals[name] = round(sum(taken), 3)
        # the PV yield for the day, from the hours that have gone by
        yields = report_values(hourly, PV_TOTAL_VARIABLE)
        taken = [v for v in yields[:hours_gone] if isinstance(v, (int, float))]
        if taken:
            hourly_totals["pv_yield"] = round(sum(taken), 3)
        if hourly_totals:
            out["hourly_totals"] = hourly_totals
        # the difference between the two ways of counting the day
        check = {}
        for name, value in out["totals"].items():
            other = hourly_totals.get(name)
            if other is not None:
                check[name] = {
                    "monthly": round(value, 3),
                    "hourly": other,
                    "difference": round(value - other, 3),
                }
        if check:
            out["check"] = check
        if "pv_yield" in out:
            hours = hourly_totals.get("pv_yield")
            check["pv yield"] = {
                "monthly": out["pv_yield"],
                "hourly": hours,
                "difference": (round(out["pv_yield"] - hours, 3)
                               if hours is not None else None),
            }

        # The balance: what came in from the sun, the battery and the
        # grid, less what went to the house, the battery and the grid.
        # It should be near zero - what is left over is the battery's
        # own losses.
        totals = out["totals"]
        if all(k in totals for k in ("pv", "house", "grid_in", "grid_out",
                                     "batt_in", "batt_out")):
            incoming = totals["pv"] + totals["batt_out"] + totals["grid_in"]
            outgoing = totals["house"] + totals["batt_in"] + totals["grid_out"]
            out["balance"] = round(incoming - outgoing, 3)

        # The hourly report also fills the chart when there is no local
        # history yet - which is the first few hours after a restart.
        # Every channel has to come from it: the chart draws the grid, and
        # a series with the grid left at zero would draw a flat, empty
        # chart under a "grid import" legend.
        channels = {"house": "loads", "grid_in": "gridConsumption",
                    "grid_out": "feedin", "batt_in": "chargeEnergyToTal",
                    "batt_out": "dischargeEnergyToTal", "pv": "PVEnergyTotal"}
        per_hour = {name: report_values(hourly, variable)
                    for name, variable in channels.items()}
        series = []
        for hour in range(len(per_hour["house"])):
            row = {}
            for name, values in per_hour.items():
                value = values[hour] if hour < len(values) else None
                row[name] = float(value) if isinstance(value, (int, float)) else 0.0
            if not any(row.values()):
                continue
            when = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            row["start"] = when.isoformat()
            series.append(row)
        if series:
            out["hourly"] = series
    except Exception as exc:  # noqa: BLE001
        out.setdefault("hourly_error", f"{type(exc).__name__}: {exc}")
    return out


def flux_bands(series, timezone_name="Europe/London"):
    """The Flux windows, as ranges across the chart.

    Cheap is 02:00-05:00 and peak is 16:00-19:00, UK local time.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return []
    if not series:
        return []
    try:
        step = (datetime.fromisoformat(series[-1]["start"]) -
                datetime.fromisoformat(series[0]["start"])).total_seconds()
        step = step / max(1, len(series) - 1)
    except (KeyError, ValueError, IndexError):
        return []
    if step <= 0:
        return []
    marks = []
    for row in series:
        try:
            when = datetime.fromisoformat(row["start"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        hour = when.hour
        fill = None
        if 2 <= hour < 5:
            fill = "rgba(125,211,252,.07)"
        elif 16 <= hour < 19:
            fill = "rgba(240,168,104,.10)"
        marks.append((row.get("start"), row.get("end"), fill))

    # The bands are given as times, not as positions - the chart is a
    # timeline, so they have to be placed by when they happened.
    bands = []
    for start, end, fill in marks:
        if fill is None:
            continue
        if bands and bands[-1]["fill"] == fill and bands[-1]["end"] == start:
            bands[-1]["end"] = end
        else:
            bands.append({"start": start, "end": end, "fill": fill})
    return bands


def usage_payload(history: HalfHourHistory,
                  daily: dict | None = None) -> dict:
    """The usage slide: the chart, today's totals and the tariff bands.

    The inverter's own daily report is used for the totals when there is
    one, and the locally recorded half-hourly history fills the chart.
    If the history is empty - which it is for the first few hours after
    a restart - the inverter's hourly report draws the chart instead.
    """
    daily = daily or {}
    series = history.series(48)
    today = history.today()
    totals = daily.get("totals") or {}
    for key, value in totals.items():
        if isinstance(value, (int, float)):
            today[key] = float(value)
    hourly = daily.get("hourly") or []
    # The inverter's hourly report is used when the local history is
    # nearly empty - which it is for the first few hours after a
    # restart.  It is a different grain, so the chart is told: an hour's
    # energy must not be drawn as though it were half an hour's.
    granularity = "half_hour"
    if len(series) < 8 and hourly:
        series = hourly
        granularity = "hour"
    out = {
        "series": series,
        "granularity": granularity,
        "today": today,
        "bands": flux_bands(series),
    }

    # the live feed's own integration of the solar power - a third
    # opinion on the same day, straight from the inverter
    live_pv = today.get("pv")
    if daily.get("pv_yield") is not None and live_pv:
        out["live_pv"] = round(live_pv, 3)
        out["pv_yield_difference"] = round(
            daily["pv_yield"] - live_pv, 3)
    return out


def collect_octopus(client: OctopusClient,
                    daily: dict | None = None) -> dict:
    """What Octopus tells us.

    The tariff rates are public, so they work without an API key. Only the
    meter's own readings need one.
    """
    out: dict = {}
    out["meters"] = {
        "import_mpan": client.mpan,
        "import_serial": client.meter_serial,
        "export_mpan": client.export_mpan,
        "export_serial": client.export_meter_serial,
    }
    try:
        out["rates"] = client.rates()
    except OctopusError as exc:
        out["rates"] = {}
        out["error"] = str(exc)
    if client.configured:
        try:
            out.update(_meter_figures(client, daily))
        except OctopusError as exc:
            out["meter"] = {}
            out["error"] = str(exc)
    return out


def _meter_figures(client: OctopusClient,
                   daily: dict | None = None) -> dict:
    """Today's import, export and cost, priced period by period.

    The meter is the authority on what crossed the grid boundary, but
    its readings can be a day or two behind - Octopus only gets them
    when the meter sends them.  And many homes have no export register
    at all, because the inverter measures the export instead.

    So each direction is taken from the meter when it has something to
    say, and from the inverter's own daily register when it does not -
    and each is marked with where it came from.
    """
    now = datetime.now(timezone.utc)
    start = today_start(client.timezone_name)
    readings = client.consumption(client.mpan, client.meter_serial, start, now)
    import_rates = client.rate_series("import", start, now)
    export_rates = client.rate_series("export", start, now)

    imported = 0.0
    cost = 0.0
    for row in readings:
        imported += row["kwh"]
        when = _parse_iso(row["start"])
        rate = client.rate_at(import_rates, when) if when else None
        if rate is not None:
            cost += row["kwh"] * rate / 100.0

    exported = 0.0
    earning = 0.0
    export_readings: list[dict] = []
    if client.export_mpan:
        export_readings = client.consumption(
            client.export_mpan,
            client.export_meter_serial or client.meter_serial,
            start, now)
        for row in export_readings:
            exported += row["kwh"]
            when = _parse_iso(row["start"])
            rate = client.rate_at(export_rates, when) if when else None
            if rate is not None:
                earning += row["kwh"] * rate / 100.0

    totals = (daily or {}).get("totals") or {}
    rate_in = _average_rate(import_rates, start, now)
    rate_out = _average_rate(export_rates, start, now)

    # The import: the meter if it has readings for today.
    if readings:
        import_source = "meter"
    elif totals:
        imported = float(totals.get("grid_in") or 0.0)
        cost = imported * rate_in / 100.0 if rate_in else 0.0
        import_source = "inverter"
    else:
        import_source = "none"

    # The export: only from a meter if there is one with readings.
    if export_readings:
        export_source = "meter"
    elif totals:
        exported = float(totals.get("grid_out") or 0.0)
        earning = exported * rate_out / 100.0 if rate_out else 0.0
        export_source = "inverter"
    else:
        export_source = "none"

    out = {
        "imported_kwh": round(imported, 3),
        "exported_kwh": round(exported, 3),
        "import_cost": round(cost, 2) if import_source != "none" else None,
        "export_earning": round(earning, 2) if export_source != "none" else None,
        "import_source": import_source,
        "export_source": export_source,
        "source": ("meter" if import_source == "meter"
                   and export_source == "meter" else "inverter"),
    }

    # when did the meter last say anything?
    latest = None
    for row in list(readings) + list(export_readings):
        when = _parse_iso(row.get("start"))
        if when is not None and (latest is None or when > latest):
            latest = when
    if latest is not None:
        out["last_reading"] = latest.isoformat()
        age = (now - latest).total_seconds() / 3600.0
        out["last_reading_age_h"] = round(age, 1)

    out["cost"] = round((out.get("import_cost") or 0.0) -
                        (out.get("export_earning") or 0.0), 2)
    return {"meter": out, "cost": out["cost"]}


def _average_rate(rates: list[dict], start: datetime, end: datetime) -> float:
    """The mean rate over the window, in pence per kWh."""
    values = []
    for row in rates or []:
        when = _parse_iso(row.get("from"))
        if when is None:
            continue
        if start <= when < end:
            try:
                values.append(float(row.get("p_kwh")))
            except (TypeError, ValueError):
                continue
    if not values:
        return 0.0
    return sum(values) / len(values)


def _parse_iso(value):
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def octopus_worker(state: DashboardState, config: ConfigStore,
                   history: HalfHourHistory, stop: threading.Event,
                   wake: threading.Event | None = None) -> None:
    """Poll Octopus slowly - the data only changes every half hour.

    The worker must never die: if it does, the panel keeps whatever it
    last had and silently stops updating.
    """
    first_pass = True
    looked_up = False
    while not stop.is_set():
        try:
            client = OctopusClient(
                api_key=config.get("octopus.api_key", "") or "",
                account_number=config.get("octopus.account_number", "") or "",
                mpan=config.get("octopus.mpan", "") or "",
                meter_serial=config.get("octopus.meter_serial", "") or "",
                export_mpan=config.get("octopus.export_mpan", "") or "",
                export_meter_serial=config.get("octopus.export_meter_serial", "") or "",
                tariff=config.get("octopus.tariff", "flux") or "flux",
            )
            # The account number is enough: look the meter point up once
            # and remember it, so the meter readings work from then on.
            # The export half of a dual-register meter is found at the
            # same time, so it is looked up until it turns up.
            if (client.api_key and client.account_number and not looked_up
                    and not (client.meter_serial and client.export_mpan)):
                try:
                    resolve_meter(client, config)
                    # only stop looking when both halves have been found -
                    # a meter point that cannot be asked yet may be able
                    # to be asked later
                    looked_up = bool(client.meter_serial
                                     and client.export_mpan)
                except OctopusError as exc:
                    message = f"account lookup: {exc}"
                    print(f"octopus: {message}")
                    state.set_octopus({"error": message,
                                       "rates": _safe_rates(client)})
                    _wait(stop, wake, 30)   # try again soon, not in five minutes
                    continue
            # On the first pass, wait a moment for the inverter's daily
            # report - the meter figures fall back to it, and without it
            # the panel would show a zero until the next poll.
            if first_pass and config.get("foxess.api_key", "") \
                    and config.get("api_enabled", True):
                # only worth waiting for when there is an open api key
                # to fetch the report with
                state.wait_for_daily(60)
            first_pass = False
            try:
                state.set_octopus(collect_octopus(client, state.daily()))
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                print(f"octopus: {message}")
                state.set_octopus({"error": message,
                                   "rates": _safe_rates(client)})
                _wait(stop, wake, 30)
                continue
            _wait(stop, wake, 300)
        except Exception as exc:  # noqa: BLE001
            # nothing that happens in here may kill the worker - if it
            # did, the panel would quietly stop updating for ever.  The
            # handler must not be able to fail either.
            print(f"octopus: {type(exc).__name__}: {exc}")
            try:
                stop.wait(30)
            except Exception:  # noqa: BLE001
                pass


def octopus_mini_worker(state: DashboardState, config: ConfigStore,
                        stop: threading.Event,
                        wake: threading.Event | None = None) -> None:
    """Poll the Octopus Mini for live meter data.

    The Mini reads the meter itself, so its numbers are live rather than
    a day behind like the half-hourly readings.  They only come from the
    GraphQL API, which wants the account's email and password rather
    than the API key.

    Octopus allows about a hundred calls an hour for the whole account,
    so this runs every two minutes and makes two of them.
    """
    mini = OctopusMini()
    backoff = 120
    while not stop.is_set():
        try:
            # The client is kept between passes, so the session token is
            # fetched once rather than every time.  It is only replaced
            # when the credentials actually change.
            credentials = (config.get("octopus.email", "") or "",
                           config.get("octopus.password", "") or "",
                           config.get("octopus.account_number", "") or "",
                           config.get("octopus.api_key", "") or "")
            if credentials != (mini.email, mini.password,
                               mini.account_number, mini.api_key):
                mini = OctopusMini(email=credentials[0],
                                   password=credentials[1],
                                   account_number=credentials[2],
                                   api_key=credentials[3])
            if mini.configured:
                payload: dict = {"source": "mini"}
                failed = False
                try:
                    payload["live"] = mini.live()
                except OctopusError as exc:
                    payload["error"] = str(exc)
                    failed = True
                try:
                    payload["today"] = mini.today()
                except OctopusError as exc:
                    payload.setdefault("error", str(exc))
                    failed = True
                state.set_mini(payload)
                _report_mini(payload, state.daily())
                if failed:
                    # Octopus limits how often the account may be asked
                    # for a session, so back off rather than keep trying
                    backoff = min(900, max(300, backoff * 2))
                    print(f"mini: {payload.get('error')} - retrying in "
                          f"{backoff // 60} minutes")
                else:
                    backoff = 120
            _wait(stop, wake, backoff)
        except Exception as exc:  # noqa: BLE001
            print(f"mini: {type(exc).__name__}: {exc}")
            try:
                stop.wait(30)
            except Exception:  # noqa: BLE001
                pass


def _report_mini(payload: dict, daily: dict) -> None:
    """Print what the Mini says, and check it against the inverter.

    The meter and the inverter both measure the grid, so their totals for
    the day should be close.  A big difference means one of them is
    wrong, and it is worth knowing which.
    """
    today = payload.get("today") or {}
    imported = today.get("consumption_kwh")
    if imported is None:
        return
    peak = today.get("peak_import_kwh") or 0.0
    when = (today.get("peak_import_at") or "")[11:16]
    print(f"mini: {imported:.2f} kWh imported today, "
          f"the biggest half hour {peak:.2f} kWh"
          + (f" at {when}" if when else ""))
    grid_in = ((daily or {}).get("totals") or {}).get("grid_in")
    if isinstance(grid_in, (int, float)) and imported:
        difference = imported - float(grid_in)
        print(f"  against the inverter: {float(grid_in):.2f} kWh "
              f"(difference {difference:+.2f} kWh)")


# ---------------------------------------------------------------------------
# The weather and the solar forecast
# ---------------------------------------------------------------------------

#: How often the weather is fetched.  A forecast does not change that fast,
#: and there is no need to be a burden on a free service.
WEATHER_INTERVAL = 900

#: How many days of the sun are kept for the calibration.
WEATHER_PAST_DAYS = 21


def plant_timezone(state: DashboardState, config: ConfigStore) -> str:
    """The timezone the inverter's days are counted in.

    The solar forecast has to line up with the inverter's own daily
    register, and that register is in the plant's timezone.
    """
    tz = config.get("display.timezone", "auto")
    if tz in ("", "auto", None):
        tz = state.meta().get("plant_timezone") or "Europe/London"
    return tz or "Europe/London"


def collect_weather(state: DashboardState, config: ConfigStore,
                    demo: bool = False,
                    history: HalfHourHistory | None = None) -> dict:
    """The weather, the outlook, and the sun as it will land on the roof.

    The solar forecast is the interesting part.  The weather service knows
    how much sun lands on a tilted plane, but not how much of it your
    array collects - so that is measured, by putting the inverter's own
    daily yield next to the forecast for the same days.
    """
    latitude = config.get("weather.latitude")
    longitude = config.get("weather.longitude")
    name = config.get("weather.location", "") or ""
    now = time.time()
    if latitude in (None, "") or longitude in (None, ""):
        return {
            "location": {"name": name},
            "error": "no location set - add one in the settings",
            "days": [], "current": {}, "solar": {}, "updated": now,
            "attribution": weather.ATTRIBUTION,
        }

    tilt = int(config.get("weather.tilt", weather.DEFAULT_TILT) or 0)
    azimuth = int(config.get("weather.azimuth", weather.DEFAULT_AZIMUTH) or 0)
    temperature = config.get("weather.temperature_unit", "celsius")
    wind = config.get("weather.wind_speed_unit", "kmh")
    performance_ratio = config.get("weather.performance_ratio",
                                   weather.DEFAULT_PERFORMANCE_RATIO)

    raw = weather.fetch(latitude, longitude, tilt=tilt, azimuth=azimuth,
                        temperature=temperature, wind=wind,
                        past_days=WEATHER_PAST_DAYS, forecast_days=7)
    payload = weather.parse(raw, name)

    # -- the calibration ------------------------------------------------
    tz = plant_timezone(state, config)
    daily = state.daily()
    produced: dict[str, float] = {}
    from_report: set[str] = set()
    from_live: dict[str, float] = {}
    source = ""

    if demo:
        # The demo has no inverter, so a pretend one is used instead: a
        # 5.5 kWp array that collects 4.7 kWh for every kWh/m² of sun.
        # Only the days that have happened - a forecast is not evidence.
        poa = weather.daily_irradiation(raw, tz)
        today = str((raw.get("current") or {}).get("time") or "")[:10]
        produced = {date: round(value * 4.7, 3)
                    for date, value in poa.items()
                    if value >= weather.MIN_POA and (not today or date < today)}
        source = "a pretend 5.5 kWp array"
    else:
        produced = {row["date"]: row["kwh"]
                    for row in (daily.get("pv_daily") or []) if row.get("date")}
        from_report = set(produced)
        # The live feed carries the solar power as well, so the days can
        # be fitted even when the Open API is not there to report them.
        # The inverter's own register wins where both have it.
        if history is not None:
            from_live = history.daily_pv(tz, days=WEATHER_PAST_DAYS)
        for date, kwh in from_live.items():
            produced.setdefault(date, kwh)
        poa = weather.daily_irradiation(raw, tz)

        if from_report:
            source = "the inverter's report"
            if set(from_live) - from_report:
                source = "the inverter's report and the live feed"
        elif from_live:
            source = "the live feed"
        else:
            source = ""

    calibration = weather.calibrate(produced, poa, performance_ratio)

    # Why it is not fitted, when it is not.
    reason = ""
    if not produced:
        if daily.get("error"):
            reason = "the inverter's daily report failed: " + str(daily["error"])
        elif not daily:
            reason = ("the inverter has not reported yet - the Open API key "
                      "may not be set")
        else:
            reason = "the inverter has not reported a daily solar yield yet"

    factor = calibration.get("factor")
    for day in payload["days"]:
        day["pv"] = (round(day.get("poa", 0.0) * factor, 2)
                     if factor else None)

    tomorrow = payload["days"][1] if len(payload["days"]) > 1 else {}
    today = payload["days"][0] if payload["days"] else {}
    solar = {
        "factor": calibration.get("factor"),
        "kwp": calibration.get("kwp"),
        "days": calibration.get("days"),
        "from": calibration.get("from"),
        "to": calibration.get("to"),
        "low": calibration.get("low"),
        "high": calibration.get("high"),
        "sum_ratio": calibration.get("sum_ratio"),
        "performance_ratio": calibration.get("performance_ratio"),
        "plausible": calibration.get("plausible"),
        "samples": calibration.get("samples") or [],
        "note": calibration.get("note", ""),
        "calibrated": bool(factor),
        "source": source,
        "reason": reason,
        "tilt": tilt,
        "azimuth": azimuth,
        "azimuth_text": weather.azimuth_label(azimuth),
        "today": today.get("pv"),
        "tomorrow": tomorrow.get("pv"),
        "today_poa": today.get("poa"),
        "tomorrow_poa": tomorrow.get("poa"),
    }

    # The cross-check: if the size of the array is known, the fit can be
    # compared with it, and the performance ratio measured rather than
    # assumed.  "array_kwp" is optional - the fit does not need it.
    known = config.get("weather.array_kwp")
    try:
        known = float(known) if known not in (None, "") else 0.0
    except (TypeError, ValueError):
        known = 0.0
    if known > 0:
        solar["array_kwp"] = known
        if factor:
            implied = factor / known
            solar["implied_ratio"] = round(implied, 3)
            solar["kwp_difference"] = round(
                (calibration.get("kwp") or 0.0) - known, 2)
            solar["agrees"] = abs(solar["kwp_difference"]) <= max(
                0.3, known * 0.1)

    # How the day is going so far: the yield the inverter has recorded
    # against the sun the forecast says has arrived.  It should land on
    # the same factor, and if it does not then the forecast is drifting.
    date, poa_now = weather.irradiation_so_far(raw, tz)
    pv_now = daily.get("pv_yield")
    if pv_now is None and history is not None and not demo:
        # The live feed's own integration of today's solar, when the
        # inverter's register has not arrived yet.
        pv_now = history.daily_pv(tz, days=2).get(date)
    if poa_now and pv_now is not None and poa_now > 0.2:
        solar["today_ratio"] = round(float(pv_now) / poa_now, 3)
        solar["today_poa_so_far"] = poa_now
        solar["today_pv_so_far"] = round(float(pv_now), 3)
        solar["today_date"] = date

    payload["solar"] = solar
    payload["units"]["precipitation"] = payload["units"].get("precipitation", "mm")
    return payload


def demo_weather() -> dict:
    """A made-up forecast, so the slide can be seen without a location.

    The numbers are plausible for an October in the UK: a bright day, a
    wet one, and a run of fog.  The solar figures come from a pretend
    5.5 kWp array, which is what a real one would look like.
    """
    now = time.time()
    conditions = [(3, "Overcast", 1.4), (0, "Clear sky", 3.1),
                  (2, "Partly cloudy", 2.6), (61, "Light rain", 0.9),
                  (45, "Fog", 0.5), (3, "Overcast", 1.8), (1, "Mainly clear", 3.4)]
    days = []
    base = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                              microsecond=0)
    for index, (code, text, poa) in enumerate(conditions):
        when = base + timedelta(days=index)
        days.append({
            "date": when.date().isoformat(),
            "weekday": when.strftime("%a"),
            "code": code,
            "text": text,
            "icon": weather.weather_icon(code),
            "tmax": 12 + (index % 3),
            "tmin": 6 + (index % 2),
            "precip": 0.0 if code in (0, 1, 2) else 1.4,
            "precip_prob": 0 if code in (0, 1, 2) else 60,
            "wind": 12 + index * 3,
            "wind_dir": 215.0,
            "wind_text": "SW",
            "sunrise": "07:12",
            "sunset": "17:54",
            "uv": 2.1,
            "poa": poa,
            "pv": round(poa * 4.7, 2),
        })
    return {
        "location": {"name": "Demo - Welwyn Garden City", "latitude": 51.8,
                     "longitude": -0.19, "timezone": "Europe/London"},
        "units": {"temperature": "°C", "wind": "km/h", "precipitation": "mm"},
        "current": {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "temperature": 14.8, "apparent": 12.7, "code": 3,
            "text": "Overcast", "icon": "cloud", "is_day": 1, "wind": 24.1,
            "wind_dir": 214.0, "wind_text": "SW", "humidity": 96.0,
            "precipitation": 0.0, "cloud": 98.0,
        },
        "days": days,
        "solar": {
            "factor": 4.7, "kwp": 5.53, "days": 14, "from": "2026-10-13",
            "to": "2026-10-26", "low": 4.5, "high": 4.9, "sum_ratio": 4.7,
            "performance_ratio": 0.85, "plausible": True, "samples": [],
            "note": "", "calibrated": True, "source": "a pretend 5.5 kWp array",
            "reason": "", "tilt": 35, "azimuth": 0,
            "azimuth_text": "South", "today": days[0]["pv"],
            "tomorrow": days[1]["pv"], "today_poa": days[0]["poa"],
            "tomorrow_poa": days[1]["poa"],
        },
        "attribution": weather.ATTRIBUTION,
        "updated": now,
    }


def weather_worker(state: DashboardState, config: ConfigStore,
                   stop: threading.Event, wake: threading.Event | None = None,
                   demo: bool = False,
                   history: HalfHourHistory | None = None) -> None:
    """Keep the weather and the solar forecast up to date.

    The worker never dies: a failure is reported on the slide and tried
    again on the next round.
    """
    while not stop.is_set():
        try:
            if demo and not config.get("weather.latitude"):
                payload = demo_weather()
            else:
                payload = collect_weather(state, config, demo=demo,
                                          history=history)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": f"{type(exc).__name__}: {exc}",
                       "updated": time.time(), "days": [], "current": {},
                       "solar": {}, "attribution": weather.ATTRIBUTION}
            print(f"weather failed: {payload['error']}")
        if payload:
            state.set_weather(payload)
            solar = payload.get("solar") or {}
            if solar.get("calibrated"):
                print(f"weather: {payload.get('location', {}).get('name', '')}"
                      f" - array about {solar.get('kwp')} kWp"
                      f" (factor {solar.get('factor')}"
                      f" from {solar.get('days')} days,"
                      f" {solar.get('source')})")
            elif solar.get("reason"):
                print(f"weather: {solar['reason']}")
        _wait(stop, wake, WEATHER_INTERVAL)


def _wait(stop: threading.Event, wake: threading.Event | None,
          seconds: float) -> None:
    """Wait, but wake early if the settings changed."""
    if wake is not None:
        wake.wait(seconds)
        wake.clear()
        return
    stop.wait(seconds)


def resolve_meter(client: OctopusClient, config: ConfigStore) -> dict:
    """Find the meter point from the account number and remember it.

    The user only has to paste an account number; the MPAN and the meter
    serial are looked up once and saved, so the readings work from then on.
    """
    found = client.lookup()
    client.mpan = client.mpan or found["mpan"]
    client.meter_serial = client.meter_serial or found["meter_serial"]
    client.export_mpan = client.export_mpan or found["export_mpan"]
    client.export_meter_serial = (client.export_meter_serial
                                  or found["export_meter_serial"])
    # anything the user typed in by hand wins over what we looked up
    changes = {}
    if found["mpan"] and not config.get("octopus.mpan"):
        changes["octopus.mpan"] = found["mpan"]
    if found["meter_serial"] and not config.get("octopus.meter_serial"):
        changes["octopus.meter_serial"] = found["meter_serial"]
    if found["export_mpan"] and not config.get("octopus.export_mpan"):
        changes["octopus.export_mpan"] = found["export_mpan"]
    if (found["export_meter_serial"]
            and not config.get("octopus.export_meter_serial")):
        changes["octopus.export_meter_serial"] = found["export_meter_serial"]
    if changes:
        config.save(changes)
        print(f"octopus: found {found['mpan']} / {found['meter_serial']}")
    return found


def _safe_rates(client: OctopusClient) -> dict:
    """The rates are public, so they still work when the meter does not."""
    try:
        return client.rates()
    except Exception:  # noqa: BLE001
        return {}

def seed_from_env(config: ConfigStore) -> None:
    """Fill empty settings from the environment (once)."""
    updates = {}
    mapping = {
        "foxess.user": "FOXESS_USER",
        "foxess.password": "FOXESS_PASS",
        "foxess.api_key": "FOXESS_API_KEY",
        "foxess.sn": "FOXESS_SN",
        "foxess.plant_id": "FOXESS_PLANT_ID",
        "foxess.domain": "FOXESS_DOMAIN",
        "octopus.api_key": "OCTOPUS_API_KEY",
        "octopus.email": "OCTOPUS_EMAIL",
        "octopus.password": "OCTOPUS_PASSWORD",
        "octopus.account_number": "OCTOPUS_ACCOUNT_NUMBER",
        "octopus.tariff": "OCTOPUS_TARIFF",
    }
    for dotted, env in mapping.items():
        value = os.environ.get(env)
        if value and not config.get(dotted, ""):
            updates[dotted] = value
    if updates:
        config.save(updates)
        print(f"seeded {len(updates)} setting(s) from the environment")


def seed_demo_history(history=None) -> None:
    """Give the demo a day of history, so the chart looks like the mock-up."""
    if history is None:
        return
    import math
    import random
    now = time.time()
    random.seed(7)
    for slot in range(48):
        when = now - (47 - slot) * 1800.0
        hour = (when % 86400) / 3600.0
        # a house: quiet overnight, busy in the morning and the evening
        shape = (0.25 + 0.75 * math.exp(-((hour - 7.5) ** 2) / 6.0)
                 + 0.6 * math.exp(-((hour - 19.0) ** 2) / 8.0))
        house = max(0.05, shape * (0.45 + random.random() * 0.5))
        solar = max(0.0, math.sin((hour - 6.0) / 12.0 * math.pi) * 2.4)
        # the battery is charged from the grid in the cheap window, which
        # is what puts a spike on the chart at two in the morning
        charging = 2.4 if 2 <= hour < 5 else 0.0
        history.add({
            "pvPower": solar * 1000,
            "loadsPower": house * 1000,
            "gridConsumptionPower": max(0.0, house + charging - solar) * 1000,
            "feedinPower": max(0.0, solar - house - charging) * 1000,
            "batChargePower": max(0.0, solar * 0.4 - house * 0.2
                                  + charging) * 1000,
            "batDischargePower": max(0.0, house * 0.3 - solar * 0.2) * 1000,
            "SoC": 55.0,
        }, when=when)


def demo_worker(state: DashboardState, stop: threading.Event,
                history=None) -> None:
    """Feed synthetic data so the UI can be previewed without credentials.

    Set ``FOXESS_DEMO_HOUR`` (0-24) to preview a particular point in the day,
    e.g. 20 for the evening discharge / grid-import case.
    """
    import math
    start_hour = float(os.environ.get("FOXESS_DEMO_HOUR", "13"))
    state.set_meta(
        plant_name="DEMO - not your system", plant_id="demo-plant",
        plant_timezone="Europe/London", inverter_sn="DEMO0000000000",
        module_sn="demo", datalogger="WLan-WW (WiFi & LAN)",
        datalogger_version="2.05", signal=48, manager_version="1.78",
        master_version="1.59", slave_version="1.03", battery_units=6,
        battery_types=["bcu", "bmu", "bmu", "bmu", "bmu", "ivu"],
        min_soc=10, min_soc_on_grid=10,
        quota={"total": "∞", "remaining": "∞"}, api_known=True,
    )
    t0 = time.time()
    while not stop.is_set():
        t = time.time() - t0
        hour = (t / 60.0 + start_hour) % 24
        pv = max(0.0, math.sin(math.pi * (hour - 6) / 14) * 5.2) if 6 <= hour <= 20 else 0.0
        load = 1.1 + 0.5 * math.sin(t / 90) + (1.8 if 17 <= hour <= 22 else 0.0)
        soc = 20 + 70 * max(0.0, math.sin(math.pi * (hour - 9) / 12))
        net = load - pv
        if net > 0:                       # demand exceeds PV -> discharge, then grid
            chg, dis = 0.0, min(net, 3.4)
            grid = max(0.0, net - dis)
            imp, exp = grid, 0.0
        else:                             # surplus -> charge, then export
            surplus = -net
            chg, dis = min(surplus, 3.0), 0.0
            exp = surplus - chg
            imp = 0.0
        # a synthetic Octopus Mini, so the live panel can be seen in the
        # demo without a real account
        state.set_mini({
            "source": "mini",
            "live": {"demand_w": round(imp * 1000 + 60, 1),
                     "consumption_wh": 41234, "export_wh": 8123},
            "today": {"consumption_kwh": round(6.42 + imp * 3, 3),
                      "export_kwh": round(1.87 + exp * 2, 3),
                      "cost": round(0.83 + imp * 0.4, 2),
                      "series": []},
        })
        state.set_live({
            "pvPower": round(pv, 3), "loadsPower": round(load, 3),
            "gridConsumptionPower": round(imp, 3), "feedinPower": round(exp, 3),
            "batChargePower": round(chg, 3), "batDischargePower": round(dis, 3),
            "SoC": round(soc, 1),
        })
        state.set_totals({
            "generation": 12536.2, "PVEnergyTotal": 8990.2, "loads": 10044.0,
            "feedin": 4024.1, "gridConsumption": 6356.6, "chargeEnergyToTal": 5771.0,
            "dischargeEnergyToTal": 5507.7, "batCycleCount": 547, "SOH": 92.0,
        })
        if history is not None:
            history.add({
                "pvPower": pv, "loadsPower": load, "gridConsumptionPower": imp,
                "feedinPower": exp, "batChargePower": chg,
                "batDischargePower": dis, "SoC": soc,
            })
            state.set_usage(usage_payload(history))
        stop.wait(2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone FoxESS dashboard")
    ap.add_argument("--config", help="path to the settings JSON file")
    ap.add_argument("--host", default=os.environ.get("FOXESS_HOST"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FOXESS_PORT", "0")) or None)
    ap.add_argument("--demo", action="store_true",
                    help="feed synthetic data so the UI can be previewed offline")
    ap.add_argument("--no-live", action="store_true", help="disable the WebSocket feed")
    ap.add_argument("--no-api", action="store_true", help="disable the Open API feed")
    args = ap.parse_args()

    config = ConfigStore(args.config)
    seed_from_env(config)

    state = DashboardState(config)

    config_path = config.path
    history_path = config_path.with_name(config_path.stem + "-history.json")
    history = HalfHourHistory(history_path)

    host = args.host or config.get("server.host", "0.0.0.0")
    port = args.port or int(config.get("server.port", 8765) or 8765)

    if args.demo:
        stop = threading.Event()
        demo_wake = threading.Event()
        seed_demo_history(history)
        threading.Thread(target=demo_worker, args=(state, stop, history),
                         daemon=True, name="demo").start()
        rotator = SlideRotator(state, config)
        rotator.start()
        threading.Thread(target=octopus_worker,
                         args=(state, config, history, stop),
                         daemon=True, name="octopus").start()
        threading.Thread(target=octopus_mini_worker,
                         args=(state, config, stop),
                         daemon=True, name="mini").start()
        threading.Thread(target=weather_worker,
                         args=(state, config, stop, demo_wake, True, history),
                         daemon=True, name="weather").start()
        print("DEMO MODE - synthetic data, no credentials needed")
        try:
            serve(state, host, port, config=config,
                  on_settings_saved=lambda saved: demo_wake.set())
        finally:
            rotator.shutdown()
            stop.set()
        return 0

    if args.no_live:
        config.save({"live_enabled": False})
    if args.no_api:
        config.save({"api_enabled": False})

    controller = Controller(config, state, history)
    controller.setup()
    controller.start()

    rotator = SlideRotator(state, config)
    rotator.start()
    threading.Thread(target=octopus_worker,
                     args=(state, config, history, controller.stop,
                           controller.octopus_wake),
                     daemon=True, name="octopus").start()
    threading.Thread(target=octopus_mini_worker,
                     args=(state, config, controller.stop,
                           controller.octopus_wake),
                     daemon=True, name="mini").start()
    threading.Thread(target=weather_worker,
                     args=(state, config, controller.stop,
                           controller.weather_wake, False, history),
                     daemon=True, name="weather").start()

    try:
        serve(state, host, port, config=config,
              on_settings_saved=controller.apply_settings)
    finally:
        rotator.shutdown()
        controller.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
