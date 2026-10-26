"""Weather and solar forecast - Open-Meteo, standard library only.

Open-Meteo is used because it needs no API key for personal use, returns
the current conditions, the hourly detail, the seven-day outlook and the
solar irradiation in a single call, and asks for attribution rather than
money.  The free tier allows 10,000 calls a day, so one call every fifteen
minutes is nothing.

The solar part is the interesting one.  Open-Meteo can give the
*global tilted irradiance* - the sun that lands on a panel at a given tilt
and compass direction - which is the input a solar forecast needs.  What it
cannot know is how much of that your particular array actually collects.
That is measured here instead: the inverter's own daily PV yield is
compared with the forecast's irradiation for the same days, and the ratio
between them is the *system factor*.

    system factor  =  kWh produced  /  kWh per square metre of sun

That number is the whole trick.  Once it is known, tomorrow's forecast
irradiation can be turned into tomorrow's yield in kWh, and the size of the
array in kWp can be worked out from it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from statistics import median
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

#: Open-Meteo ask for a credit rather than payment, so it is shown on the
#: weather slide.
ATTRIBUTION = "Weather by Open-Meteo.com"

USER_AGENT = "foxess-dashboard/1.0 (python stdlib)"

#: The WMO weather codes: the words, and the icon that goes with them.
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "sun"),
    1: ("Mainly clear", "sun"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "sleet"),
    57: ("Freezing drizzle", "sleet"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "sleet"),
    67: ("Freezing rain", "sleet"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm with hail", "storm"),
    99: ("Thunderstorm with hail", "storm"),
}

#: The sixteen compass points.
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")

#: The units Open-Meteo will accept, and how they are written.
TEMPERATURE_UNITS = {"celsius": "°C", "fahrenheit": "°F"}
WIND_UNITS = {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "kn"}

#: Below this much sun a day's numbers say more about the cloud than about
#: the panels, so those days are left out of the calibration.
MIN_POA = 0.4

#: What a good array collects, as a share of the sun that lands on it.
#: Used to turn the measured system factor into a size in kWp.
DEFAULT_PERFORMANCE_RATIO = 0.85

#: The compass direction of the array, and the tilt from the horizontal.
DEFAULT_TILT = 35
DEFAULT_AZIMUTH = 0          # 0 is south, as Open-Meteo counts it


class WeatherError(RuntimeError):
    """Raised when the weather service cannot be reached, or says no."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def weather_text(code: Any) -> str:
    """The words for a WMO weather code."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "Unknown"
    return WMO_CODES.get(code, ("Unknown", "cloud"))[0]


def weather_icon(code: Any) -> str:
    """The icon for a WMO weather code."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "cloud"
    return WMO_CODES.get(code, ("Unknown", "cloud"))[1]


def compass(degrees: Any) -> str:
    """A compass point for a bearing in degrees."""
    try:
        degrees = float(degrees)
    except (TypeError, ValueError):
        return ""
    return COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]


def temperature_unit(name: Any) -> str:
    """The symbol for a temperature unit."""
    return TEMPERATURE_UNITS.get(str(name or "celsius").lower(), "°C")


def wind_unit(name: Any) -> str:
    """The unit for a wind speed."""
    return WIND_UNITS.get(str(name or "kmh").lower(), "km/h")


def clamp_azimuth(value: Any) -> int:
    """Open-Meteo only accepts an azimuth between -180 and 180.

    Their compass runs anticlockwise from south, so +90 is west and -90 is
    east.
    """
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_AZIMUTH
    if value < -180 or value > 180:
        value = ((value + 180) % 360) - 180
    return value


def azimuth_label(value: Any) -> str:
    """A compass word for an azimuth, for the settings page."""
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return "South"
    names = {0: "South", 45: "South-west", 90: "West", 135: "North-west",
             180: "North", -45: "South-east", -90: "East", -135: "North-east"}
    if value in names:
        return names[value]
    if abs(value) < 23:
        return "South"
    if 23 <= abs(value) <= 68:
        return "South-west" if value > 0 else "South-east"
    if 68 < abs(value) < 113:
        return "West" if value > 0 else "East"
    if 113 <= abs(value) <= 158:
        return "North-west" if value > 0 else "North-east"
    return "North"


# ---------------------------------------------------------------------------
# Talking to Open-Meteo
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict, timeout: int = 25) -> dict:
    full = f"{url}?{urlencode(params)}"
    request = Request(full, headers={"User-Agent": USER_AGENT,
                                     "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise WeatherError(f"HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise WeatherError(f"network: {exc.reason}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise WeatherError("the service sent something that is not JSON") from None
    if isinstance(data, dict) and data.get("error"):
        raise WeatherError(str(data.get("reason") or "the service refused the request"))
    if not isinstance(data, dict):
        raise WeatherError("unexpected reply from the service")
    return data


def geocode(name: str, count: int = 5) -> list[dict]:
    """Look a place up by name.

    Only towns and villages in the gazetteer come back - a hamlet or a
    street will not, and the nearest town is the one to use.
    """
    name = (name or "").strip()
    if not name:
        return []
    data = _get_json(GEOCODE_URL, {
        "name": name,
        "count": max(1, min(20, int(count))),
        "language": "en",
        "format": "json",
    })
    places = []
    for row in data.get("results") or []:
        places.append({
            "name": row.get("name") or name,
            "admin": row.get("admin1") or "",
            "country": row.get("country") or "",
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "timezone": row.get("timezone") or "",
        })
    return places


def fetch(latitude: float, longitude: float, tilt: int = DEFAULT_TILT,
          azimuth: int = DEFAULT_AZIMUTH, temperature: str = "celsius",
          wind: str = "kmh", past_days: int = 21, forecast_days: int = 7,
          timeout: int = 25) -> dict:
    """One call for the current conditions, the outlook and the sun."""
    params = {
        "latitude": round(float(latitude), 4),
        "longitude": round(float(longitude), 4),
        "current": ",".join((
            "temperature_2m", "apparent_temperature", "is_day", "precipitation",
            "weather_code", "cloud_cover", "wind_speed_10m", "wind_direction_10m",
            "relative_humidity_2m",
        )),
        "hourly": ",".join((
            "global_tilted_irradiance", "temperature_2m",
            "precipitation_probability",
        )),
        "daily": ",".join((
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "precipitation_probability_max",
            "wind_speed_10m_max", "wind_direction_10m_dominant",
            "sunrise", "sunset", "uv_index_max",
        )),
        "temperature_unit": temperature if temperature in TEMPERATURE_UNITS else "celsius",
        "wind_speed_unit": wind if wind in WIND_UNITS else "kmh",
        "timezone": "auto",
        "tilt": max(0, min(90, int(tilt))),
        "azimuth": clamp_azimuth(azimuth),
        "past_days": max(0, min(92, int(past_days))),
        "forecast_days": max(1, min(16, int(forecast_days))),
    }
    return _get_json(FORECAST_URL, params, timeout)


# ---------------------------------------------------------------------------
# Turning the reply into something useful
# ---------------------------------------------------------------------------

def _zone(tz_name: str | None):
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return None


def _hourly_rows(raw: dict) -> list[tuple[datetime, float]]:
    """Every hour of the reply with its irradiation, as (local time, W/m²)."""
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("global_tilted_irradiance") or []
    rows = []
    for stamp, value in zip(times, values):
        if value is None:
            continue
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            continue
        try:
            rows.append((when, float(value)))
        except (TypeError, ValueError):
            continue
    return rows


def daily_irradiation(raw: dict, tz_name: str | None = None) -> dict[str, float]:
    """The irradiation on the roof plane for each day, in kWh per square metre.

    The days are the ones in the location's own timezone.  Pass *tz_name*
    to get the days of another timezone instead - the inverter's daily
    register uses the plant's timezone, so that is the one to use when the
    two are compared.
    """
    zone = _zone(tz_name)
    offset = raw.get("utc_offset_seconds") or 0
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    out: dict[str, float] = {}
    for when, value in _hourly_rows(raw):
        if zone is not None:
            utc = when - timedelta(seconds=offset)
            day = utc.astimezone(zone).date().isoformat()
        else:
            day = when.date().isoformat()
        out[day] = out.get(day, 0.0) + value / 1000.0
    return out


def irradiation_so_far(raw: dict, tz_name: str | None = None) -> tuple[str, float]:
    """Today's irradiation up to this moment, and the day it belongs to.

    The hours are the ones that have already happened, and the day is the
    one in the timezone given - which is the inverter's own, so that the
    half-day can be compared with the inverter's half-day.
    """
    stamp = (raw.get("current") or {}).get("time") or ""
    try:
        now = datetime.strptime(stamp, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return "", 0.0
    zone = _zone(tz_name)
    offset = raw.get("utc_offset_seconds") or 0
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    now_utc = now - timedelta(seconds=offset)
    if zone is not None:
        day = now_utc.astimezone(zone).date().isoformat()
    else:
        day = now.date().isoformat()
    total = 0.0
    for when, value in _hourly_rows(raw):
        # only the hours of that day that have already happened - the
        # reply also carries weeks of history.  The value is for the hour
        # that starts at the stamp, so an hour that is still running is
        # left out.
        stamp_utc = when - timedelta(seconds=offset)
        if stamp_utc >= now_utc:
            continue
        if zone is not None:
            if stamp_utc.astimezone(zone).date().isoformat() != day:
                continue
        elif when.date().isoformat() != day:
            continue
        total += value / 1000.0
    return day, round(total, 3)


def _daily_list(raw: dict) -> list[dict]:
    """The seven-day outlook, one entry per day."""
    daily = raw.get("daily") or {}
    times = daily.get("time") or []

    def column(name: str) -> list:
        values = daily.get(name)
        return values if isinstance(values, list) else []

    codes = column("weather_code")
    tmax = column("temperature_2m_max")
    tmin = column("temperature_2m_min")
    rain = column("precipitation_sum")
    chance = column("precipitation_probability_max")
    wind = column("wind_speed_10m_max")
    bearing = column("wind_direction_10m_dominant")
    sunrise = column("sunrise")
    sunset = column("sunset")
    uv = column("uv_index_max")

    out = []
    for index, date in enumerate(times):
        def at(column_values, default=None):
            if index < len(column_values):
                value = column_values[index]
                return default if value is None else value
            return default

        code = at(codes, 0)
        try:
            weekday = datetime.strptime(str(date), "%Y-%m-%d").strftime("%a")
        except ValueError:
            weekday = ""
        out.append({
            "date": str(date),
            "weekday": weekday,
            "code": code,
            "text": weather_text(code),
            "icon": weather_icon(code),
            "tmax": at(tmax),
            "tmin": at(tmin),
            "precip": at(rain),
            "precip_prob": at(chance),
            "wind": at(wind),
            "wind_dir": at(bearing),
            "wind_text": compass(at(bearing)),
            "sunrise": str(at(sunrise, "") or "")[11:16],
            "sunset": str(at(sunset, "") or "")[11:16],
            "uv": at(uv),
        })
    return out


def parse(raw: dict, name: str = "", tz_name: str | None = None) -> dict:
    """The reply, in the shape the dashboard wants."""
    current = raw.get("current") or {}
    code = current.get("weather_code")

    def number(value, default=None):
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    payload = {
        "location": {
            "name": name or "",
            "latitude": number(raw.get("latitude")),
            "longitude": number(raw.get("longitude")),
            "timezone": raw.get("timezone") or "",
        },
        "updated": time.time(),
        "units": {
            "temperature": temperature_unit(raw.get("temperature_unit")
                                            or (raw.get("current_units") or {})
                                            .get("temperature_2m", "°C")),
            "wind": wind_unit(raw.get("wind_speed_unit")
                              or (raw.get("current_units") or {}).get("wind_speed_10m", "km/h")),
            "precipitation": (raw.get("current_units") or {}).get("precipitation", "mm"),
        },
        "current": {
            "time": current.get("time") or "",
            "temperature": number(current.get("temperature_2m")),
            "apparent": number(current.get("apparent_temperature")),
            "code": code,
            "text": weather_text(code),
            "icon": weather_icon(code),
            "is_day": current.get("is_day"),
            "wind": number(current.get("wind_speed_10m")),
            "wind_dir": number(current.get("wind_direction_10m")),
            "wind_text": compass(current.get("wind_direction_10m")),
            "humidity": number(current.get("relative_humidity_2m")),
            "precipitation": number(current.get("precipitation")),
            "cloud": number(current.get("cloud_cover")),
        },
        "days": _daily_list(raw),
        "attribution": ATTRIBUTION,
    }
    # The sun on the roof, for the days the forecast covers.
    poa = daily_irradiation(raw)
    for day in payload["days"]:
        day["poa"] = round(poa.get(day["date"], 0.0), 3)
    # The reply also carries the days that have already happened, which
    # are only wanted for the calibration - so the outlook starts today.
    today = (current.get("time") or "")[:10]
    if today:
        payload["days"] = [day for day in payload["days"]
                           if day["date"] >= today]
    return payload


# ---------------------------------------------------------------------------
# The calibration
# ---------------------------------------------------------------------------

def calibrate(pv_daily: dict, poa_daily: dict,
              performance_ratio: float = DEFAULT_PERFORMANCE_RATIO) -> dict:
    """Work out how much the array collects, and how big it is.

    ``pv_daily``   the inverter's own daily PV yield in kWh, by date
    ``poa_daily``  the forecast's irradiation on the roof plane in kWh/m²

    The ratio of the two is the *system factor*: the kWh the array makes
    for every kWh per square metre of sun that lands on it.  It carries
    the panels' efficiency, the losses, the dirt and the inverter all at
    once, and it is measured rather than assumed.

    The size of the array in kWp is the factor divided by the performance
    ratio - the share of the sun's energy a good array collects once the
    heat, the cabling, the mismatch and the dirt have taken their cut.
    The performance ratio is an assumption, so the kWp is an estimate;
    the factor itself is a measurement.
    """
    samples = []
    for date in sorted(poa_daily or {}):
        produced = (pv_daily or {}).get(date)
        poa = poa_daily.get(date)
        if produced is None or poa is None:
            continue
        try:
            produced = float(produced)
            poa = float(poa)
        except (TypeError, ValueError):
            continue
        if poa < MIN_POA or produced <= 0:
            continue
        samples.append({
            "date": date,
            "poa": round(poa, 3),
            "pv": round(produced, 3),
            "ratio": produced / poa,
        })

    if not samples:
        return {
            "factor": None, "kwp": None, "days": 0, "samples": [],
            "performance_ratio": performance_ratio,
            "note": "not enough days yet to compare the sun with your yield",
        }

    ratios = sorted(sample["ratio"] for sample in samples)
    factor = median(ratios)
    total_pv = sum(sample["pv"] for sample in samples)
    total_poa = sum(sample["poa"] for sample in samples)
    try:
        pr = float(performance_ratio)
    except (TypeError, ValueError):
        pr = DEFAULT_PERFORMANCE_RATIO
    if not 0.3 < pr < 1.0:
        pr = DEFAULT_PERFORMANCE_RATIO
    kwp = factor / pr

    return {
        "factor": round(factor, 3),
        "kwp": round(kwp, 2),
        "days": len(samples),
        "from": samples[0]["date"],
        "to": samples[-1]["date"],
        "low": round(ratios[0], 3),
        "high": round(ratios[-1], 3),
        "sum_ratio": round(total_pv / total_poa, 3) if total_poa else None,
        "performance_ratio": pr,
        "plausible": 0.3 <= kwp <= 30.0,
        "samples": samples[-14:],
        "note": "",
    }
