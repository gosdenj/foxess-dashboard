"""Tests for the weather client, the solar calibration and the weather slide."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess import weather  # noqa: E402
from foxess.config import DEFAULTS, EDITABLE  # noqa: E402
from foxess.state import DashboardState  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
SETTINGS = os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _raw(hours=None, current_hour=12, offset=0, daily=None):
    """A reply shaped like Open-Meteo's.

    *hours* is a list of (hour-of-day, W/m²) for one day; the reply is
    built for three days so that the past and the future can be told
    apart.
    """
    if hours is None:
        hours = [(0, 0.0), (6, 100.0), (12, 800.0), (18, 100.0), (23, 0.0)]
    times, values = [], []
    for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
        for hour, value in hours:
            times.append(f"{day}T{hour:02d}:00")
            values.append(value)
    if daily is None:
        daily = {
            "time": ["2026-10-05", "2026-10-06", "2026-10-07"],
            "weather_code": [3, 0, 61],
            "temperature_2m_max": [15.0, 16.0, 13.0],
            "temperature_2m_min": [7.0, 8.0, 6.0],
            "precipitation_sum": [0.4, 0.0, 2.1],
            "precipitation_probability_max": [60, 0, 90],
            "wind_speed_10m_max": [22.0, 12.0, 30.0],
            "wind_direction_10m_dominant": [215, 180, 270],
            "sunrise": ["2026-10-05T07:00", "2026-10-06T07:00", "2026-10-07T07:00"],
            "sunset": ["2026-10-05T18:00", "2026-10-06T18:00", "2026-10-07T18:00"],
            "uv_index_max": [2.1, 2.4, 1.2],
        }
    return {
        "latitude": 51.8,
        "longitude": -0.2,
        "utc_offset_seconds": offset,
        "timezone": "Europe/London",
        "current": {
            "time": f"2026-10-06T{current_hour:02d}:00",
            "temperature_2m": 12.5,
            "apparent_temperature": 10.0,
            "is_day": 1,
            "precipitation": 0.0,
            "weather_code": 3,
            "cloud_cover": 90,
            "wind_speed_10m": 20.0,
            "wind_direction_10m": 215,
            "relative_humidity_2m": 88,
        },
        "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h",
                          "precipitation": "mm"},
        "hourly": {"time": times, "global_tilted_irradiance": values},
        "daily": daily,
    }


class TestWeatherCodes(unittest.TestCase):
    def test_every_code_has_words_and_an_icon(self):
        for code, (text, icon) in weather.WMO_CODES.items():
            self.assertTrue(text)
            self.assertTrue(icon)
            self.assertIn(code, weather.WMO_CODES)

    def test_known_codes(self):
        self.assertEqual(weather.weather_text(0), "Clear sky")
        self.assertEqual(weather.weather_text(3), "Overcast")
        self.assertEqual(weather.weather_text(95), "Thunderstorm")
        self.assertEqual(weather.weather_icon(0), "sun")
        self.assertEqual(weather.weather_icon(2), "partly")
        self.assertEqual(weather.weather_icon(61), "rain")
        self.assertEqual(weather.weather_icon(95), "storm")

    def test_unknown_code_is_handled(self):
        self.assertEqual(weather.weather_text(1234), "Unknown")
        self.assertEqual(weather.weather_icon(1234), "cloud")
        self.assertEqual(weather.weather_text(None), "Unknown")
        self.assertEqual(weather.weather_icon("nonsense"), "cloud")


class TestCompass(unittest.TestCase):
    def test_cardinal_points(self):
        self.assertEqual(weather.compass(0), "N")
        self.assertEqual(weather.compass(90), "E")
        self.assertEqual(weather.compass(180), "S")
        self.assertEqual(weather.compass(270), "W")
        self.assertEqual(weather.compass(360), "N")

    def test_bearings(self):
        self.assertEqual(weather.compass(215), "SW")
        self.assertEqual(weather.compass(225), "SW")
        self.assertEqual(weather.compass(46), "NE")

    def test_nonsense(self):
        self.assertEqual(weather.compass(None), "")
        self.assertEqual(weather.compass("x"), "")


class TestUnits(unittest.TestCase):
    def test_temperature(self):
        self.assertEqual(weather.temperature_unit("celsius"), "°C")
        self.assertEqual(weather.temperature_unit("fahrenheit"), "°F")
        self.assertEqual(weather.temperature_unit("nonsense"), "°C")

    def test_wind(self):
        self.assertEqual(weather.wind_unit("kmh"), "km/h")
        self.assertEqual(weather.wind_unit("ms"), "m/s")
        self.assertEqual(weather.wind_unit("mph"), "mph")
        self.assertEqual(weather.wind_unit("kn"), "kn")
        self.assertEqual(weather.wind_unit(None), "km/h")

    def test_the_spellings_the_service_uses_are_understood(self):
        # The service does not echo the names it was given - it writes
        # miles per hour as "mp/h" and metres per second as "m/s".  A
        # reading labelled from those has to say the same thing.
        self.assertEqual(weather.wind_unit("mp/h"), "mph")
        self.assertEqual(weather.wind_unit("m/s"), "m/s")
        self.assertEqual(weather.wind_unit("km/h"), "km/h")
        self.assertEqual(weather.temperature_unit("°C"), "°C")
        self.assertEqual(weather.temperature_unit("°F"), "°F")

    def test_the_case_does_not_matter(self):
        self.assertEqual(weather.wind_unit("MPH"), "mph")
        self.assertEqual(weather.temperature_unit("Fahrenheit"), "°F")

    def test_a_wind_speed_is_converted(self):
        self.assertAlmostEqual(weather.convert_wind(24.1, "kmh"), 24.1, places=3)
        self.assertAlmostEqual(weather.convert_wind(24.1, "ms"), 6.694, places=2)
        self.assertAlmostEqual(weather.convert_wind(24.1, "mph"), 14.974, places=2)
        self.assertAlmostEqual(weather.convert_wind(24.1, "kn"), 13.009, places=2)

    def test_a_temperature_is_converted(self):
        self.assertAlmostEqual(weather.convert_temperature(14.8, "celsius"), 14.8, places=3)
        self.assertAlmostEqual(weather.convert_temperature(14.8, "fahrenheit"),
                               58.64, places=2)

    def test_nothing_converts_to_nothing(self):
        self.assertIsNone(weather.convert_wind("rubbish", "mph"))
        self.assertIsNone(weather.convert_temperature(None, "fahrenheit"))


class TestAzimuth(unittest.TestCase):
    def test_south_is_zero(self):
        self.assertEqual(weather.clamp_azimuth(0), 0)
        self.assertEqual(weather.azimuth_label(0), "South")

    def test_west_is_positive(self):
        self.assertEqual(weather.clamp_azimuth(90), 90)
        self.assertEqual(weather.azimuth_label(90), "West")

    def test_east_is_negative(self):
        self.assertEqual(weather.clamp_azimuth(-90), -90)
        self.assertEqual(weather.azimuth_label(-90), "East")

    def test_out_of_range_is_wrapped(self):
        # Open-Meteo only accepts -180..180
        self.assertEqual(weather.clamp_azimuth(270), -90)
        self.assertEqual(weather.clamp_azimuth(360), 0)
        self.assertEqual(weather.clamp_azimuth(-200), 160)

    def test_nonsense(self):
        self.assertEqual(weather.clamp_azimuth(None), 0)
        self.assertEqual(weather.clamp_azimuth("x"), 0)


class TestDailyIrradiation(unittest.TestCase):
    def test_sums_the_hours_into_days(self):
        raw = _raw()
        days = weather.daily_irradiation(raw)
        self.assertEqual(sorted(days), ["2026-10-05", "2026-10-06", "2026-10-07"])
        # 100 + 800 + 100 W/m² over an hour each = 1.0 kWh/m²
        self.assertAlmostEqual(days["2026-10-06"], 1.0, places=6)

    def test_the_plant_timezone_moves_the_day_boundary(self):
        # 23:00 in London is 22:00 UTC, and a place in Tokyo counts that
        # hour as the next day
        raw = _raw(hours=[(23, 500.0)], current_hour=23, offset=0)
        raw["timezone"] = "UTC"
        london = weather.daily_irradiation(raw, "UTC")
        tokyo = weather.daily_irradiation(raw, "Asia/Tokyo")
        self.assertIn("2026-10-05", london)
        self.assertIn("2026-10-06", tokyo)

    def test_missing_values_are_skipped(self):
        raw = _raw()
        raw["hourly"]["global_tilted_irradiance"][0] = None
        days = weather.daily_irradiation(raw)
        self.assertAlmostEqual(days["2026-10-05"], 1.0, places=6)


class TestIrradiationSoFar(unittest.TestCase):
    def test_only_todays_hours(self):
        raw = _raw(hours=[(0, 100.0), (12, 800.0)], current_hour=15)
        day, total = weather.irradiation_so_far(raw)
        self.assertEqual(day, "2026-10-06")
        # the two complete hours of today - and not the same two on the
        # other two days
        self.assertAlmostEqual(total, 0.9, places=6)

    def test_an_hour_that_is_still_running_is_left_out(self):
        raw = _raw(hours=[(0, 100.0), (12, 800.0)], current_hour=12)
        day, total = weather.irradiation_so_far(raw)
        self.assertEqual(day, "2026-10-06")
        self.assertAlmostEqual(total, 0.1, places=6)

    def test_nothing_without_a_current_time(self):
        raw = _raw()
        raw["current"]["time"] = "rubbish"
        self.assertEqual(weather.irradiation_so_far(raw), ("", 0.0))


class TestCalibration(unittest.TestCase):
    def test_finds_the_factor(self):
        poa = {"2026-10-05": 2.0, "2026-10-06": 3.0}
        produced = {"2026-10-05": 8.0, "2026-10-06": 12.0}
        out = weather.calibrate(produced, poa)
        self.assertAlmostEqual(out["factor"], 4.0, places=3)
        self.assertEqual(out["days"], 2)
        self.assertTrue(out["plausible"])

    def test_the_kwp_follows_the_performance_ratio(self):
        poa = {"2026-10-05": 2.0}
        produced = {"2026-10-05": 8.0}
        out = weather.calibrate(produced, poa, performance_ratio=0.8)
        self.assertAlmostEqual(out["kwp"], 5.0, places=2)
        out = weather.calibrate(produced, poa, performance_ratio=0.85)
        self.assertAlmostEqual(out["kwp"], 4.71, places=2)

    def test_a_nonsense_performance_ratio_is_replaced(self):
        poa = {"2026-10-05": 2.0}
        produced = {"2026-10-05": 8.0}
        out = weather.calibrate(produced, poa, performance_ratio=9.0)
        self.assertEqual(out["performance_ratio"], weather.DEFAULT_PERFORMANCE_RATIO)

    def test_the_median_wins_over_one_bad_day(self):
        poa = {"2026-10-05": 2.0, "2026-10-06": 2.0, "2026-10-07": 2.0}
        produced = {"2026-10-05": 8.0, "2026-10-06": 8.0, "2026-10-07": 40.0}
        out = weather.calibrate(produced, poa)
        self.assertAlmostEqual(out["factor"], 4.0, places=3)
        self.assertAlmostEqual(out["high"], 20.0, places=3)

    def test_days_with_no_sun_are_left_out(self):
        poa = {"2026-10-05": 0.1, "2026-10-06": 2.0}
        produced = {"2026-10-05": 0.4, "2026-10-06": 8.0}
        out = weather.calibrate(produced, poa)
        self.assertEqual(out["days"], 1)
        self.assertAlmostEqual(out["factor"], 4.0, places=3)

    def test_a_day_with_no_yield_is_left_out(self):
        poa = {"2026-10-05": 2.0, "2026-10-06": 2.0}
        produced = {"2026-10-05": 0.0, "2026-10-06": 8.0}
        out = weather.calibrate(produced, poa)
        self.assertEqual(out["days"], 1)

    def test_nothing_to_compare(self):
        out = weather.calibrate({}, {})
        self.assertIsNone(out["factor"])
        self.assertIsNone(out["kwp"])
        self.assertEqual(out["days"], 0)
        self.assertTrue(out["note"])


class TestParse(unittest.TestCase):
    def test_the_outlook_starts_today(self):
        raw = _raw()
        parsed = weather.parse(raw, "Somewhere")
        self.assertEqual([d["date"] for d in parsed["days"]],
                         ["2026-10-06", "2026-10-07"])

    def test_the_days_carry_their_sun(self):
        raw = _raw()
        parsed = weather.parse(raw, "Somewhere")
        self.assertAlmostEqual(parsed["days"][0]["poa"], 1.0, places=6)

    def test_the_current_conditions(self):
        parsed = weather.parse(_raw(), "Somewhere")
        self.assertEqual(parsed["current"]["text"], "Overcast")
        self.assertEqual(parsed["current"]["icon"], "cloud")
        self.assertEqual(parsed["current"]["wind_text"], "SW")
        self.assertAlmostEqual(parsed["current"]["temperature"], 12.5)

    def test_the_units_are_carried(self):
        parsed = weather.parse(_raw(), "Somewhere")
        self.assertEqual(parsed["units"]["temperature"], "°C")
        self.assertEqual(parsed["units"]["wind"], "km/h")

    def test_sunrise_and_sunset_are_clipped_to_the_time(self):
        parsed = weather.parse(_raw(), "Somewhere")
        self.assertEqual(parsed["days"][0]["sunrise"], "07:00")
        self.assertEqual(parsed["days"][0]["sunset"], "18:00")

    def test_the_attribution_is_there(self):
        parsed = weather.parse(_raw(), "Somewhere")
        self.assertIn("Open-Meteo", parsed["attribution"])


def _raw_in(temperature: str = "°C", wind: str = "km/h") -> dict:
    """A reply that names its units the way the service does.

    The service writes its own spellings into the reply - miles per hour
    comes back as "mp/h" - so this is how a reply really looks.
    """
    raw = _raw()
    raw["current_units"] = {"temperature_2m": temperature,
                             "wind_speed_10m": wind,
                             "precipitation": "mm"}
    return raw


class TestUnitsAreCarriedThrough(unittest.TestCase):
    """The units that are set have to reach the slide.

    The service writes its units in its own way, so the label cannot be
    worked out from the reply - it has to come from the settings, or the
    number and the unit will disagree.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _config(self, **settings):
        from foxess.config import ConfigStore
        config = ConfigStore(os.path.join(self.tmp, "config.json"))
        config.save({f"weather.{name}": value
                     for name, value in settings.items()})
        return config

    def test_the_unit_that_was_asked_for_is_the_one_shown(self):
        # A reply that does not name its units - only the settings know
        # what the numbers are in.
        raw = _raw_in()
        raw.pop("current_units")
        parsed = weather.parse(raw, "Somewhere", temperature="fahrenheit",
                               wind="mph")
        self.assertEqual(parsed["units"]["temperature"], "°F")
        self.assertEqual(parsed["units"]["wind"], "mph")

    def test_the_reply_is_read_when_no_unit_is_given(self):
        parsed = weather.parse(_raw_in("°F", "mp/h"), "Somewhere")
        self.assertEqual(parsed["units"]["temperature"], "°F")
        self.assertEqual(parsed["units"]["wind"], "mph")

    def test_the_settings_reach_the_payload(self):
        import run as app
        from foxess.state import DashboardState

        config = self._config(location="Somewhere", latitude=51.8,
                              longitude=-0.2, wind_speed_unit="mph",
                              temperature_unit="fahrenheit")
        state = DashboardState(config)
        state.set_daily({})
        raw = _raw_in()
        raw.pop("current_units")
        with mock.patch.object(weather, "fetch", return_value=raw):
            payload = app.collect_weather(state, config, history=None)
        self.assertEqual(payload["units"]["wind"], "mph")
        self.assertEqual(payload["units"]["temperature"], "°F")

    def test_the_units_are_there_before_there_is_a_forecast(self):
        import run as app
        from foxess.state import DashboardState

        config = self._config(wind_speed_unit="mph",
                              temperature_unit="fahrenheit")
        state = DashboardState(config)
        payload = app.collect_weather(state, config, history=None)
        self.assertEqual(payload["units"]["wind"], "mph")
        self.assertEqual(payload["units"]["temperature"], "°F")

    def test_the_demo_converts_its_numbers(self):
        import run as app

        payload = app.demo_weather("fahrenheit", "mph")
        self.assertEqual(payload["units"]["temperature"], "°F")
        self.assertEqual(payload["units"]["wind"], "mph")
        # 24.1 km/h is about 15 mph
        self.assertAlmostEqual(payload["current"]["wind"], 14.97, places=1)
        # 14.8 °C is about 59 °F
        self.assertAlmostEqual(payload["current"]["temperature"], 58.6, places=1)


class TestGeocode(unittest.TestCase):
    def test_the_results_are_normalised(self):
        reply = {"results": [{
            "name": "Luton", "admin1": "England", "country": "United Kingdom",
            "latitude": 51.8797, "longitude": -0.4175, "timezone": "Europe/London",
        }]}
        with mock.patch.object(weather, "_get_json", return_value=reply) as get:
            places = weather.geocode("Luton")
        get.assert_called_once()
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "Luton")
        self.assertEqual(places[0]["admin"], "England")
        self.assertAlmostEqual(places[0]["latitude"], 51.8797)
        self.assertEqual(places[0]["timezone"], "Europe/London")

    def test_an_empty_name_asks_nothing(self):
        with mock.patch.object(weather, "_get_json") as get:
            self.assertEqual(weather.geocode("  "), [])
        get.assert_not_called()

    def test_nothing_found(self):
        with mock.patch.object(weather, "_get_json", return_value={"results": []}):
            self.assertEqual(weather.geocode("Nowhere-at-all"), [])


class TestFetch(unittest.TestCase):
    def test_the_request_carries_everything_needed(self):
        captured = {}

        def fake(url, params, timeout=25):
            captured.update(params)
            return _raw()

        with mock.patch.object(weather, "_get_json", side_effect=fake):
            weather.fetch(51.8, -0.2, tilt=40, azimuth=45, temperature="fahrenheit",
                          wind="mph", past_days=10, forecast_days=5)
        self.assertEqual(captured["latitude"], 51.8)
        self.assertEqual(captured["tilt"], 40)
        self.assertEqual(captured["azimuth"], 45)
        self.assertEqual(captured["temperature_unit"], "fahrenheit")
        self.assertEqual(captured["wind_speed_unit"], "mph")
        self.assertEqual(captured["past_days"], 10)
        self.assertEqual(captured["forecast_days"], 5)
        self.assertIn("global_tilted_irradiance", captured["hourly"])
        self.assertIn("uv_index_max", captured["daily"])
        self.assertIn("weather_code", captured["current"])

    def test_nonsense_units_fall_back(self):
        captured = {}

        def fake(url, params, timeout=25):
            captured.update(params)
            return _raw()

        with mock.patch.object(weather, "_get_json", side_effect=fake):
            weather.fetch(51.8, -0.2, temperature="furlongs", wind="knots")
        self.assertEqual(captured["temperature_unit"], "celsius")
        self.assertEqual(captured["wind_speed_unit"], "kmh")

    def test_the_tilt_and_azimuth_are_kept_in_range(self):
        captured = {}

        def fake(url, params, timeout=25):
            captured.update(params)
            return _raw()

        with mock.patch.object(weather, "_get_json", side_effect=fake):
            weather.fetch(51.8, -0.2, tilt=200, azimuth=270)
        self.assertEqual(captured["tilt"], 90)
        self.assertEqual(captured["azimuth"], -90)


class TestWeatherSettings(unittest.TestCase):
    def test_the_defaults(self):
        section = DEFAULTS["weather"]
        self.assertEqual(section["temperature_unit"], "celsius")
        self.assertEqual(section["wind_speed_unit"], "kmh")
        self.assertEqual(section["tilt"], 35)
        self.assertEqual(section["azimuth"], 0)
        self.assertEqual(section["performance_ratio"], 0.85)
        self.assertEqual(section["latitude"], "")

    def test_every_weather_setting_is_editable(self):
        for path in ("weather.location", "weather.latitude", "weather.longitude",
                     "weather.temperature_unit", "weather.wind_speed_unit",
                     "weather.tilt", "weather.azimuth", "weather.performance_ratio"):
            self.assertIn(path, EDITABLE)

    def test_the_weather_slide_is_in_the_rotation(self):
        self.assertIn("weather", DEFAULTS["display"]["slides"])


class TestWeatherState(unittest.TestCase):
    def test_the_weather_travels_in_the_snapshot(self):
        state = DashboardState()
        state.set_weather({"days": [{"date": "2026-10-06"}], "solar": {"kwp": 4.9}})
        self.assertEqual(state.weather()["solar"]["kwp"], 4.9)
        self.assertIn("weather", state.snapshot())
        self.assertEqual(state.snapshot()["weather"]["days"][0]["date"], "2026-10-06")

    def test_the_weather_starts_empty(self):
        state = DashboardState()
        self.assertEqual(state.weather(), {})


class TestLiveFeedFitting(unittest.TestCase):
    """The fit does not need the Open API.

    The live feed carries the solar power, so integrating it gives the
    day's yield - which is enough to fit the array from.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _history(self, factor: float):
        """Three days of solar, at *factor* kWh for every kWh/m² of sun."""
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        # 1.0 kWh/m² a day, so the yield is *factor* kWh a day.  The feed
        # is in kW, and each slot is half an hour.
        kw = factor / (48 * 0.5)
        for day in (5, 6, 7):
            for slot in range(48):
                when = datetime(2026, 10, day, slot // 2, slot % 2 * 30,
                                tzinfo=timezone.utc).timestamp()
                history.add({"pvPower": kw, "loadsPower": 0,
                             "gridConsumptionPower": 0, "feedinPower": 0,
                             "batChargePower": 0, "batDischargePower": 0,
                             "SoC": 50}, when=when)
        return history

    def test_the_daily_solar_is_read_from_the_history(self):
        history = self._history(4.7)
        # The history is from a fixed date, so the window has to be wide
        # enough to reach it - the default window is three weeks, and it
        # moves on with the calendar.
        days = history.daily_pv("Europe/London", days=3650)
        for date in ("2026-10-06", "2026-10-07"):
            self.assertAlmostEqual(days[date], 4.7, places=1)
        # The days are the plant's, not UTC's, so the first day starts an
        # hour late and the last half hour lands on the next day.
        self.assertLess(days["2026-10-05"], 4.7)
        self.assertIn("2026-10-08", days)

    def test_the_array_is_fitted_without_the_open_api(self):
        import run as app
        from foxess.config import ConfigStore
        from foxess.state import DashboardState

        config = ConfigStore(os.path.join(self.tmp, "config.json"))
        config.save({"weather.location": "Somewhere",
                     "weather.latitude": 51.8, "weather.longitude": -0.2,
                     "weather.array_kwp": 5.5})
        state = DashboardState(config)
        state.set_daily({})        # no Open API report at all

        raw = _raw(hours=[(12, 1000.0)])   # 1.0 kWh/m² a day
        with mock.patch.object(weather, "fetch", return_value=raw):
            payload = app.collect_weather(state, config,
                                          history=self._history(4.7))
        solar = payload["solar"]
        self.assertTrue(solar["calibrated"])
        self.assertAlmostEqual(solar["factor"], 4.7, places=1)
        # 4.7 / 0.85 = 5.53 kWp - which is a 5.5 kWp array
        self.assertAlmostEqual(solar["kwp"], 5.53, places=1)
        self.assertEqual(solar["source"], "the live feed")
        # the cross-check against the size the user knows
        self.assertAlmostEqual(solar["array_kwp"], 5.5, places=2)
        self.assertAlmostEqual(solar["implied_ratio"], 4.7 / 5.5, places=2)
        self.assertTrue(solar["agrees"])

    def test_the_reason_is_said_when_it_cannot_be_fitted(self):
        import run as app
        from foxess.config import ConfigStore
        from foxess.state import DashboardState

        config = ConfigStore(os.path.join(self.tmp, "config.json"))
        config.save({"weather.location": "Somewhere",
                     "weather.latitude": 51.8, "weather.longitude": -0.2})
        state = DashboardState(config)
        state.set_daily({})
        raw = _raw(hours=[(12, 1000.0)])
        with mock.patch.object(weather, "fetch", return_value=raw):
            payload = app.collect_weather(state, config, history=None)
        solar = payload["solar"]
        self.assertFalse(solar["calibrated"])
        self.assertTrue(solar["reason"])
        self.assertIn("Open API", solar["reason"])


class TestWeatherSlide(unittest.TestCase):
    """The slide itself - the markup, the layout and the drawing code."""

    def setUp(self):
        self.index = _read(INDEX)
        self.settings = _read(SETTINGS)

    def test_the_three_panels_are_there(self):
        for panel in ('class="panel now"', 'class="panel sun"', 'class="panel week"'):
            self.assertIn(panel, self.index)

    def test_the_slide_hides_the_other_panels(self):
        self.assertIn('.dash[data-slide="weather"] .now', self.index)
        self.assertIn('.dash[data-slide="weather"] .sun', self.index)
        self.assertIn('.dash[data-slide="weather"] .week', self.index)
        self.assertIn('.dash[data-slide="weather"] .flow', self.index)

    def test_the_grid_is_set_for_the_slide(self):
        self.assertIn('.dash[data-slide="weather"]{', self.index)
        self.assertIn('grid-template-areas:"now sun" "week week"', self.index)

    def test_the_icons_are_drawn_not_fetched(self):
        self.assertIn("const WX_ICONS", self.index)
        for icon in ("sun", "partly", "cloud", "fog", "drizzle", "rain",
                     "snow", "sleet", "storm"):
            self.assertIn(f"{icon}:", self.index)
        # no external icon files
        self.assertNotIn("weather-icons", self.index)

    def test_the_render_is_called(self):
        self.assertIn("renderWeather(s.weather || {})", self.index)

    def test_the_settings_page_has_the_weather_fields(self):
        for field in ("wx_location", "wx_latitude", "wx_longitude",
                      "wx_temperature_unit", "wx_wind_speed_unit",
                      "wx_tilt", "wx_azimuth", "wx_performance_ratio"):
            self.assertIn(f'id="{field}"', self.settings)
            self.assertIn(field, self.settings)

    def test_the_settings_page_can_find_a_place_and_test(self):
        self.assertIn("function findPlace()", self.settings)
        self.assertIn("function testWeather()", self.settings)
        self.assertIn("/api/weather/locate", self.settings)
        self.assertIn("/api/weather/test", self.settings)

    def test_the_slide_uses_the_units_that_are_set(self):
        self.assertIn('const tempUnit = units.temperature', self.index)
        self.assertIn('const precipUnit = units.precipitation', self.index)
        self.assertIn('Math.round(cur.temperature)+tempUnit', self.index)
        self.assertIn('Math.round(cur.apparent)+tempUnit', self.index)
        self.assertIn('Math.round(d.tmax)+tempUnit', self.index)
        self.assertIn('Math.round(d.tmin)+tempUnit', self.index)

    def test_the_slide_has_no_units_written_into_it(self):
        # A unit typed into the slide would ignore the settings, and
        # would be wrong for anyone who chose a different one.
        for hardcoded in ('Math.round(cur.temperature)+"°"',
                          'Math.round(cur.apparent)+"°"',
                          'Math.round(d.tmax)+"°"',
                          'Math.round(d.tmin)+"°"',
                          '.toFixed(1)+" mm"'):
            self.assertNotIn(hardcoded, self.index)

    def test_the_weather_slide_is_offered(self):
        self.assertIn('["weather","Weather"', self.settings)


if __name__ == "__main__":
    unittest.main()
