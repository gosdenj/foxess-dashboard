"""Yesterday, for the comparison on the today panel.

The today panel shows what today has come to.  Under each figure it
also shows yesterday's, so that the two can be read against each other.

Yesterday comes from the inverter's own register - the monthly report
that has already been fetched - and from the meter for the cost.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import run as app
from foxess.octopus import OctopusMini

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _register(days: int, **values):
    """A monthly register: one value per day for each variable.

    The values are given as a list, so that a day can be picked out of
    the month by its index.
    """
    out = []
    for name, series in values.items():
        out.append({"variable": name, "values": list(series)})
    return out


class TestYesterdayTotals(unittest.TestCase):
    """Yesterday's figures, out of the inverter's own register."""

    def _yesterday(self, monthly, local=None, api=None):
        local = local or datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
        api = api or mock.Mock()
        return app._yesterday_totals(api, "key", "SN", local, monthly)

    def test_the_yesterdays_figures_are_picked_out_of_the_register(self):
        # the 27th is index 26, so yesterday the 26th is index 25
        monthly = _register(27,
                            generation=[1.0] * 25 + [2.0, 3.0],
                            loads=[10.0] * 25 + [11.0, 12.0],
                            gridConsumption=[20.0] * 25 + [21.0, 22.0],
                            feedin=[30.0] * 25 + [31.0, 32.0],
                            chargeEnergyToTal=[40.0] * 25 + [41.0, 42.0],
                            dischargeEnergyToTal=[50.0] * 25 + [51.0, 52.0],
                            PVEnergyTotal=[60.0] * 25 + [6.1, 7.2])
        y = self._yesterday(monthly)
        self.assertAlmostEqual(y["house"], 11.0, places=3)
        self.assertAlmostEqual(y["grid_in"], 21.0, places=3)
        self.assertAlmostEqual(y["grid_out"], 31.0, places=3)
        self.assertAlmostEqual(y["batt_in"], 41.0, places=3)
        self.assertAlmostEqual(y["batt_out"], 51.0, places=3)

    def test_the_solar_figure_is_the_yield_not_the_inverters_output(self):
        """The panel's solar figure is the yield, the way the app has it."""
        monthly = _register(27,
                            generation=[1.0] * 25 + [2.0, 3.0],
                            PVEnergyTotal=[60.0] * 25 + [6.1, 7.2])
        y = self._yesterday(monthly)
        self.assertAlmostEqual(y["pv"], 6.1, places=3)

    def test_the_first_of_the_month_asks_for_the_month_before(self):
        """Yesterday was in the month before, so its register is elsewhere."""
        api = mock.Mock()
        api.report_query.return_value = _register(
            31, loads=[1.0] * 30 + [7.7], PVEnergyTotal=[0.0] * 31)
        local = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        y = self._yesterday(_register(1, loads=[1.0], PVEnergyTotal=[0.0]),
                            local=local, api=api)
        self.assertAlmostEqual(y["house"], 7.7, places=3)
        # and it was asked for by month, for October
        self.assertEqual(api.report_query.call_args[0][2], "month")
        self.assertEqual(api.report_query.call_args[0][3], 2026)
        self.assertEqual(api.report_query.call_args[0][4], 10)

    def test_the_month_before_is_only_asked_for_once(self):
        """It does not change, so it is not asked for every five minutes."""
        api = mock.Mock()
        api.report_query.return_value = _register(
            31, loads=[1.0] * 30 + [7.7], PVEnergyTotal=[0.0] * 31)
        local = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        cache = {}
        monthly = _register(1, loads=[1.0], PVEnergyTotal=[0.0])
        for _ in range(3):
            app._yesterday_totals(api, "key", "SN", local, monthly, cache)
        self.assertEqual(api.report_query.call_count, 1)

    def test_a_register_that_has_no_yesterday_gives_nothing(self):
        """Better no comparison than a wrong one."""
        y = self._yesterday(_register(3, loads=[1.0, 2.0, 3.0]))
        self.assertEqual(y, {})


class TestYesterdayInThePayload(unittest.TestCase):
    """Yesterday has to reach the slide."""

    def _collect(self, monthly, hourly=None):
        api = mock.Mock()
        hourly = hourly or [{"variable": "generation", "values": [0.1] * 10}]

        def report_query(key, sn, dimension, year, month, day, variables):
            return monthly if dimension == "month" else hourly

        api.report_query.side_effect = report_query
        with mock.patch("run.datetime") as when:
            when.now.return_value = datetime(2026, 10, 27, 9, 0,
                                              tzinfo=timezone.utc)
            with mock.patch("zoneinfo.ZoneInfo"):
                return app.collect_daily(api, "key", "SN", "Europe/London")

    def test_the_daily_report_carries_yesterday(self):
        monthly = _register(27, loads=[10.0] * 25 + [11.0, 12.0],
                            PVEnergyTotal=[60.0] * 25 + [6.1, 7.2])
        daily = self._collect(monthly)
        self.assertAlmostEqual(daily["yesterday"]["house"], 11.0, places=3)
        self.assertAlmostEqual(daily["yesterday"]["pv"], 6.1, places=3)

    def test_the_usage_payload_carries_yesterday(self):
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        daily = {"totals": {"pv": 1.0}, "yesterday": {"pv": 2.0,
                                                       "house": 3.0}}
        payload = app.usage_payload(history, daily)
        self.assertEqual(payload["yesterday"]["pv"], 2.0)

    def test_a_day_with_no_yesterday_still_works(self):
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        payload = app.usage_payload(history, {"totals": {"pv": 1.0}})
        self.assertNotIn("yesterday", payload)


class TestMiniYesterday(unittest.TestCase):
    """The meter's own yesterday, for the cost."""

    def _mini(self, rows):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        mini.telemetry = mock.Mock(return_value=rows)
        return mini

    def _rows(self, days=1, per_day=48):
        """Half-hourly readings, with a running counter and a cost."""
        rows = []
        counter = 0
        base = datetime(2026, 10, 26, tzinfo=timezone.utc)
        for day in range(days):
            for slot in range(per_day):
                when = base + timedelta(days=day, minutes=30 * slot)
                counter += 100
                rows.append({"read_at": when.isoformat(),
                             "consumption": float(counter),
                             "export": float(counter // 2),
                             "demand": 200.0,
                             "consumption_delta": 100.0,
                             "cost": 25.0})
        return rows

    def test_yesterday_is_reported_when_the_whole_of_it_is_there(self):
        mini = self._mini(self._rows(days=2))
        with mock.patch("foxess.octopus.local_now") as now:
            now.return_value = datetime(2026, 10, 27, 9, 0,
                                        tzinfo=timezone.utc)
            with mock.patch("foxess.octopus.today_start") as start:
                start.return_value = datetime(2026, 10, 27, tzinfo=timezone.utc)
                out = mini.today()
        self.assertIn("yesterday", out)
        # 48 half hours of 100 Wh is 4.8 kWh
        self.assertAlmostEqual(out["yesterday"]["consumption_kwh"], 4.8,
                               places=3)
        self.assertAlmostEqual(out["yesterday"]["cost"], 12.0, places=2)

    def test_a_part_of_a_day_is_not_reported(self):
        """A part of yesterday is worse than none at all."""
        mini = self._mini(self._rows(days=2, per_day=20))
        with mock.patch("foxess.octopus.local_now") as now:
            now.return_value = datetime(2026, 10, 27, 9, 0,
                                        tzinfo=timezone.utc)
            with mock.patch("foxess.octopus.today_start") as start:
                start.return_value = datetime(2026, 10, 27, tzinfo=timezone.utc)
                out = mini.today()
        self.assertNotIn("yesterday", out)

    def test_the_query_reaches_back_to_yesterday(self):
        mini = self._mini([])
        with mock.patch("foxess.octopus.local_now") as now:
            now.return_value = datetime(2026, 10, 27, 9, 0,
                                        tzinfo=timezone.utc)
            with mock.patch("foxess.octopus.today_start") as start:
                start.return_value = datetime(2026, 10, 27, tzinfo=timezone.utc)
                mini.today()
        begin = mini.telemetry.call_args_list[0][0][0]
        self.assertEqual(begin, datetime(2026, 10, 25, 23, 0,
                                         tzinfo=timezone.utc))


class TestTheComparisonOnTheSlide(unittest.TestCase):
    """The comparison itself, in the page."""

    def setUp(self):
        self.index = INDEX.read_text()

    def test_each_tile_carries_the_comparison(self):
        self.assertIn('class="cmp"', self.index)
        self.assertIn(".tile .cmp{", self.index)

    def test_the_comparison_is_yesterdays_figure(self):
        self.assertIn('const Y = (o && o.yesterday) || {}', self.index)
        self.assertIn("<span class='cl'>yesterday</span>", self.index)

    def test_the_word_and_the_number_are_on_separate_lines(self):
        self.assertIn(".tile .cmp{margin-top:.3em;display:flex;"
                      "flex-direction:column", self.index)
        self.assertIn(".tile .cmp .cl{", self.index)
        self.assertIn(".tile .cmp .cv{", self.index)

    def test_the_number_carries_its_unit(self):
        """The energy tiles, the percentage and the cost all have one."""
        self.assertIn('gy(Y.pv, "kWh", 2)', self.index)
        self.assertIn('gy(Y.house, "kWh", 2)', self.index)
        self.assertIn('gy(Y.batt_out, "kWh", 2)', self.index)
        self.assertIn('gy(Y.grid_in, "kWh", 2)', self.index)
        self.assertIn('gy(keptY, "%", 0)', self.index)
        self.assertIn('"\\u00a3" + num(v, 2)', self.index)

    def test_the_comparison_is_smaller_than_the_figure_it_sits_under(self):
        self.assertIn(".tile .cmp .cv{color:var(--muted);font-size:var(--fsl)",
                      self.index)

    def test_the_cost_comparison_comes_from_the_meter(self):
        self.assertIn("const miniY = miniToday.yesterday || {}", self.index)
        self.assertIn("gcost(miniY.cost)", self.index)

    def test_the_share_of_the_solar_is_worked_out_for_yesterday_too(self):
        self.assertIn("const keptY =", self.index)
        self.assertIn("Y.grid_out", self.index)

    def test_a_tile_with_no_yesterday_has_no_comparison_line(self):
        self.assertIn('${t.y?`<div class="cmp">${t.y}</div>`:""}',
                      self.index)


if __name__ == "__main__":
    unittest.main()
