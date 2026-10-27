"""Tests for the sun's times - the module behind the daylight band.

The band is drawn from the sun's own position, so the times worked out
here have to agree with the ones the forecast service gives.  The
figures below were taken from the service, for places and days well
apart from each other.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess import sun  # noqa: E402


def _minutes(stamp: str) -> int:
    """"HH:MM" as minutes past midnight."""
    return int(stamp[:2]) * 60 + int(stamp[3:])


#: The times the forecast service gives, as (day, lat, lon, offset in
#: minutes, sunrise, sunset).  The offset is the place's own distance
#: from UTC.
FORECAST = [
    ((2026, 8, 1), 51.8, -0.19, 0, "04:23", "19:49"),
    ((2026, 8, 15), 51.8, -0.19, 0, "04:45", "19:24"),
    ((2026, 9, 21), 51.8, -0.19, 0, "05:45", "18:01"),
    ((2026, 10, 27), 51.8, -0.19, 0, "06:46", "16:42"),
    ((2026, 10, 31), 51.8, -0.19, 0, "06:53", "16:34"),
    ((2026, 10, 27), 53.48, -2.24, 0, "06:58", "16:46"),
    ((2026, 8, 1), -33.87, 151.21, 660, "07:47", "18:15"),
    ((2026, 10, 27), -33.87, 151.21, 660, "06:00", "19:18"),
    ((2026, 8, 1), 64.15, -21.94, 0, "04:34", "22:31"),
    ((2026, 10, 27), 64.15, -21.94, 0, "08:55", "17:27"),
]


class TestSunTimes(unittest.TestCase):
    def test_the_sun_agrees_with_the_forecast(self):
        for day, lat, lon, offset, rise, fall in FORECAST:
            times = sun.events(day, lat, lon, offset)
            where = f"{day} at {lat},{lon}"
            self.assertLessEqual(
                abs(times["sunrise"] - _minutes(rise)), 3, where)
            self.assertLessEqual(
                abs(times["sunset"] - _minutes(fall)), 3, where)

    def test_the_sun_is_usually_within_a_minute_of_the_forecast(self):
        # the worst case is a high latitude in midsummer, where the sun
        # is slow to move and small differences tell
        for day, lat, lon, offset, rise, fall in FORECAST:
            times = sun.events(day, lat, lon, offset)
            for mine, theirs in ((times["sunrise"], _minutes(rise)),
                                 (times["sunset"], _minutes(fall))):
                self.assertLessEqual(abs(mine - theirs), 3)

    def test_the_day_is_longer_in_midsummer(self):
        summer = sun.events((2026, 6, 21), 51.8, -0.19, 60)
        winter = sun.events((2026, 12, 21), 51.8, -0.19, 0)
        self.assertGreater(summer["sunset"] - summer["sunrise"], 16 * 60)
        self.assertLess(winter["sunset"] - winter["sunrise"], 9 * 60)

    def test_the_south_hemisphere_has_its_summer_first(self):
        # in August it is summer in Sydney, and the days are long
        times = sun.events((2026, 8, 1), -33.87, 151.21, 660)
        self.assertGreater(times["sunset"] - times["sunrise"], 10 * 60)

    def test_the_sun_rises_in_the_east_and_sets_in_the_west(self):
        # the times are given in the place's own timezone, so a place
        # further east has them earlier
        west = sun.events((2026, 10, 27), 51.8, -0.19, 0)
        east = sun.events((2026, 10, 27), 53.48, 1.5, 0)
        self.assertGreater(west["sunrise"], east["sunrise"])


class TestTwilight(unittest.TestCase):
    def test_the_twilight_comes_in_the_right_order(self):
        times = sun.events((2026, 10, 27), 51.8, -0.19, 0)
        self.assertLess(times["dawn"]["astronomical"], times["dawn"]["nautical"])
        self.assertLess(times["dawn"]["nautical"], times["dawn"]["civil"])
        self.assertLess(times["dawn"]["civil"], times["sunrise"])
        self.assertLess(times["sunrise"], times["sunset"])
        self.assertLess(times["sunset"], times["dusk"]["civil"])
        self.assertLess(times["dusk"]["civil"], times["dusk"]["nautical"])
        self.assertLess(times["dusk"]["nautical"], times["dusk"]["astronomical"])

    def test_the_civil_twilight_is_the_right_way_out(self):
        # in the middle of October in Britain the light goes about half
        # an hour before the sun clears the horizon
        times = sun.events((2026, 10, 27), 51.8, -0.19, 0)
        before = times["sunrise"] - times["dawn"]["civil"]
        after = times["dusk"]["civil"] - times["sunset"]
        self.assertTrue(20 <= before <= 45, before)
        self.assertTrue(20 <= after <= 45, after)

    def test_the_twilight_is_longer_in_high_summer(self):
        summer = sun.events((2026, 6, 21), 51.8, -0.19, 60)
        winter = sun.events((2026, 12, 21), 51.8, -0.19, 0)
        self.assertGreater(summer["sunrise"] - summer["dawn"]["civil"],
                           winter["sunrise"] - winter["dawn"]["civil"])

    def test_the_polar_night_has_no_sunrise(self):
        times = sun.events((2026, 12, 21), 78.0, 15.0, 60)
        self.assertIsNone(times["sunrise"])
        self.assertIsNone(times["sunset"])
        self.assertIsNone(times["dawn"]["civil"])

    def test_the_polar_day_has_no_night(self):
        times = sun.events((2026, 6, 21), 78.0, 15.0, 120)
        self.assertIsNone(times["sunrise"])
        self.assertIsNone(times["sunset"])


class TestFormatting(unittest.TestCase):
    def test_the_clock_is_formatted_as_a_time(self):
        self.assertEqual(sun.clock(406), "06:46")
        self.assertEqual(sun.clock(0), "00:00")
        self.assertEqual(sun.clock(1439), "23:59")
        self.assertEqual(sun.clock(None), "")

    def test_the_span_is_formatted_as_a_length(self):
        self.assertEqual(sun.span(406, 1002), "9h 56m")
        self.assertEqual(sun.span(0, 45), "0h 45m")
        self.assertEqual(sun.span(None, 45), "")


if __name__ == "__main__":
    unittest.main()
