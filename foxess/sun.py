"""When the sun rises and sets, and when the twilight around it begins.

The forecast already tells us when the sun rises and sets, but not the
twilight that comes either side of it - the first light, the last light,
the two blues in between.  Those are what make the daylight band on the
weather slide worth drawing, so they are worked out here.

The standard solar position formula is used, the same one the reference
sites use.  Nothing outside the standard library is needed, and the
whole thing is a few lines of arithmetic once the sun's place for the
day is known.

The times come out in minutes past midnight in the place's own
timezone, because that is what the band is drawn against.
"""

from __future__ import annotations

import math
from typing import Any

# The sun's centre is this far below the horizon when it counts as
# sunrise or sunset.  The third of a degree is the usual allowance for
# the edge of the sun and for the way the air bends the light.
SUN = -0.833
# The three twilights: civil, nautical and astronomical.
CIVIL = -6.0
NAUTICAL = -12.0
ASTRONOMICAL = -18.0


def _julian(day: tuple[int, int, int]) -> float:
    """The julian day at midnight UTC on the day given."""
    year, month, date = day
    # January and February are counted as months 13 and 14 of the year
    # before, so that the leap day lands at the end.
    if month <= 2:
        year -= 1
        month += 12
    century = year // 100
    correction = 2 - century + century // 4
    return (int(365.25 * (year + 4716)) + int(30.6001 * (month + 1))
            + date + correction - 1524.5)


def _sun(julian: float) -> tuple[float, float]:
    """The sun's declination and the equation of time for a julian day.

    The declination is how far the sun leans north or south of the
    equator - it is what makes the days long in summer and short in
    winter.  The equation of time is how far the sun runs ahead of the
    clock; it is never more than a quarter of an hour.
    """
    t = (julian - 2451545.0) / 36525.0
    mean_longitude = math.radians(
        (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360)
    anomaly = math.radians(
        (357.52911 + t * (35999.05029 - 0.0001537 * t)) % 360)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    # The sun's real place runs a little ahead of its mean place - the
    # difference between the two is at most a couple of degrees.
    centre = (math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t)
              + math.sin(3 * anomaly) * 0.000289)
    true_longitude = mean_longitude + math.radians(centre)
    # The earth's axis leans, and it wobbles very slightly as it turns.
    omega = math.radians(125.04 - 1934.136 * t)
    obliquity = (math.radians(23 + (26 + (21.448 - t * (46.815 + t * (
        0.00059 - t * 0.001813))) / 60) / 60) + 0.00256 * math.cos(omega))
    declination = math.asin(math.sin(obliquity) * math.sin(true_longitude))
    y = math.tan(obliquity / 2) ** 2
    equation = (y * math.sin(2 * mean_longitude)
                - 2 * eccentricity * math.sin(anomaly)
                + 4 * eccentricity * y * math.sin(anomaly)
                * math.cos(2 * mean_longitude)
                - 0.5 * y * y * math.sin(4 * mean_longitude)
                - 1.25 * eccentricity * eccentricity * math.sin(2 * anomaly))
    # The bracket works in radians, and the sun moves four degrees of it
    # an hour, so four times the degrees is the minutes it gains.
    return declination, equation * 180 / math.pi * 4


def events(day: tuple[int, int, int], latitude: float, longitude: float,
           offset: float = 0) -> dict[str, Any]:
    """The sun's times for a day, in minutes past local midnight.

    *day* is a (year, month, date) tuple, *latitude* and *longitude* are
    in degrees with north and east positive, and *offset* is the
    timezone's distance from UTC in minutes.

    The result holds "sunrise" and "sunset", and the twilight either
    side of them under "dawn" and "dusk".  Anything the sun does not
    reach - the middle of a polar night, say - comes back as None.
    """
    julian = _julian(day)
    declination, equation = _sun(julian)
    noon = 720 - 4 * longitude - equation
    # The sun is wanted where it is at noon, not where it was at
    # midnight, so the figures are worked out again at midday.  That
    # sharpens the result by a minute or so.
    for _ in range(2):
        declination, equation = _sun(julian + noon / 1440.0)
        noon = 720 - 4 * longitude - equation

    def half_day(depression: float) -> float | None:
        """Minutes from noon to the sun sitting at *depression* degrees.

        None when the sun never gets that far, which is what happens
        inside the polar circles.
        """
        cos_hour = ((math.sin(math.radians(depression))
                     - math.sin(math.radians(latitude)) * math.sin(declination))
                    / (math.cos(math.radians(latitude))
                       * math.cos(declination)))
        if cos_hour < -1 or cos_hour > 1:
            return None
        return 4 * math.degrees(math.acos(cos_hour))

    def crossings(depression: float) -> tuple[float | None, float | None]:
        """The times the sun crosses that depression, rising and setting."""
        half = half_day(depression)
        if half is None:
            return (None, None)
        return (noon - half + offset, noon + half + offset)

    rising, setting = crossings(SUN)
    result: dict[str, Any] = {"sunrise": rising, "sunset": setting,
                               "dawn": {}, "dusk": {}}
    for name, depression in (("civil", CIVIL), ("nautical", NAUTICAL),
                              ("astronomical", ASTRONOMICAL)):
        result["dawn"][name] = crossings(depression)[0]
        result["dusk"][name] = crossings(depression)[1]
    return result


def clock(minutes: float | None) -> str:
    """Minutes past midnight as "HH:MM"."""
    if minutes is None:
        return ""
    total = int(round(minutes)) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def span(start: float | None, end: float | None) -> str:
    """The time between two moments, as "9h 56m"."""
    if start is None or end is None:
        return ""
    minutes = int(round(end - start))
    if minutes < 0:
        minutes = 0
    return f"{minutes // 60}h {minutes % 60}m"
