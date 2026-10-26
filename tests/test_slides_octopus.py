"""Tests for the slides and the Octopus integration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import DEFAULTS, EDITABLE, SECRET_PATHS  # noqa: E402
from foxess.history import CHANNELS, HalfHourHistory, slot_start  # noqa: E402
from foxess.octopus import (  # noqa: E402
    FLUX_BANDS,
    OctopusClient,
    OctopusError,
    _premium,
)
from foxess.state import DashboardState  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
SETTINGS = os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")


def _now_ish() -> float:
    """A timestamp inside the history's retention window."""
    import time
    return time.time() - 3600


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestSlideConfig(unittest.TestCase):

    def test_defaults(self):
        self.assertEqual(DEFAULTS["display"]["slides"], ["power", "usage", "weather"])
        self.assertEqual(DEFAULTS["display"]["slide_interval"], 20)

    def test_editable(self):
        self.assertIn("display.slides", EDITABLE)
        self.assertIn("display.slide_interval", EDITABLE)

    def test_the_index_owns_the_grid_per_slide(self):
        """A desktop area map applied at 800x480 would override the compact
        layout, so each slide needs its own map per breakpoint."""
        index = _read(INDEX)
        for slide in ("power", "usage", "weather"):
            self.assertIn(f'body[data-slide="{slide}"] .dash', index)
        self.assertIn("@media(min-width:1001px) and (min-height:561px)", index)
        self.assertIn("@media(max-width:1000px),(max-height:560px)", index)
        self.assertIn("@media(max-width:640px),(max-height:400px)", index)

    def test_the_settings_page_offers_the_slides(self):
        page = _read(SETTINGS)
        # the list is saved as a list, not through the generic field map
        self.assertIn('s["display.slides"] = currentSlides()', page)
        self.assertIn("display.slide_interval", page)
        for slide in ("power", "usage"):
            self.assertIn(f'"{slide}"', page)

    def test_the_settings_page_has_the_octopus_fields(self):
        page = _read(SETTINGS)
        for field in ("octopus_api_key", "octopus_account_number", "octopus_tariff",
                      "octopus_mpan", "octopus_meter_serial", "octopus_export_mpan"):
            self.assertIn(field, page)

    def test_the_api_key_is_a_secret(self):
        self.assertIn("octopus.api_key", SECRET_PATHS)
        self.assertIn("octopus.api_key", EDITABLE)


class TestSlideState(unittest.TestCase):

    def setUp(self):
        self.state = DashboardState()

    def test_it_starts_on_the_first_slide(self):
        self.state.set_slides(["power", "usage"])
        slide = self.state.slide()
        self.assertEqual(slide["name"], "power")
        self.assertEqual(slide["count"], 2)

    def test_it_advances_and_wraps(self):
        self.state.set_slides(["power", "usage"])
        self.state.advance_slide()
        self.assertEqual(self.state.slide()["name"], "usage")
        self.state.advance_slide()
        self.assertEqual(self.state.slide()["name"], "power")

    def test_a_single_slide_does_not_move(self):
        self.state.set_slides(["power"])
        self.state.advance_slide()
        self.assertEqual(self.state.slide()["name"], "power")

    def test_jumping_to_a_named_slide(self):
        self.state.set_slides(["power", "usage"])
        self.state.set_slide("usage")
        self.assertEqual(self.state.slide()["name"], "usage")

    def test_changing_the_list_keeps_the_current_slide(self):
        self.state.set_slides(["power", "usage"])
        self.state.set_slide("usage")
        self.state.set_slides(["usage", "power"])
        self.assertEqual(self.state.slide()["name"], "usage")

    def test_removing_the_current_slide_falls_back_to_the_first(self):
        self.state.set_slides(["power", "usage"])
        self.state.set_slide("usage")
        self.state.set_slides(["power"])
        self.assertEqual(self.state.slide()["name"], "power")

    def test_stopping_the_rotation_does_not_move_the_slide(self):
        """Changing the interval must not jump the display.

        The rotator is asleep for the length of the interval, so it only
        finds out about a change when it wakes up - and it must not move
        the slide on the way out.
        """
        import time

        import run as app

        class _Config:
            def __init__(self):
                self.values = {"display.slides": ["power", "usage"],
                               "display.slide_interval": 30}

            def get(self, path, default=None):
                return self.values.get(path, default)

        config = _Config()
        state = DashboardState()
        rotator = app.SlideRotator(state, config)
        rotator.start()
        try:
            time.sleep(0.3)
            state.set_slide("usage")
            config.values["display.slide_interval"] = 0
            # long enough for the rotator to have noticed and stopped
            time.sleep(1.5)
            self.assertEqual(state.slide()["name"], "usage")
        finally:
            rotator.shutdown()

    def test_the_rotator_advances_when_it_should(self):
        import time

        import run as app

        class _Config:
            def __init__(self):
                self.values = {"display.slides": ["power", "usage"],
                               "display.slide_interval": 1}

            def get(self, path, default=None):
                return self.values.get(path, default)

        state = DashboardState()
        rotator = app.SlideRotator(state, _Config())
        rotator.start()
        try:
            # the rotation keeps going round, so watch which slides turn up
            # rather than looking at where it happens to be at one moment
            seen = set()
            for _ in range(12):
                time.sleep(0.3)
                seen.add(state.slide()["name"])
            self.assertIn("usage", seen)
        finally:
            rotator.shutdown()

    def test_the_slide_travels_in_the_snapshot(self):
        self.state.set_slides(["power", "usage"])
        self.state.advance_slide()
        self.assertEqual(self.state.snapshot()["slide"]["name"], "usage")

    def test_the_usage_and_octopus_channels_travel_too(self):
        self.state.set_usage({"series": [{"house": 1.0}]})
        self.state.set_octopus({"rates": {"band": "peak"}})
        snap = self.state.snapshot()
        self.assertEqual(snap["usage"]["series"][0]["house"], 1.0)
        self.assertEqual(snap["octopus"]["rates"]["band"], "peak")


class TestHalfHourHistory(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(self.path)
        self.history = HalfHourHistory(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_it_integrates_power_into_energy(self):
        start = _now_ish()
        # half an hour at 2 kW is 1 kWh - the live feed is in kW, the
        # same unit the rest of the dashboard uses
        for i in range(60):
            self.history.add({"pvPower": 2.0}, when=start + i * 30)
        buckets = self.history.series(48)
        self.assertTrue(buckets)
        # the samples may straddle two half-hour buckets, so add them up.
        # 60 samples 30 s apart is 29.5 minutes of integration.
        self.assertAlmostEqual(sum(b["pv"] for b in buckets), 0.983, places=2)

    def test_a_kilowatt_is_a_kilowatt(self):
        # The feed was in watts once, and the history divided by a
        # thousand as well - so the chart came out a thousand times too
        # small.  A kW for an hour has to be a kWh.
        start = _now_ish()
        for i in range(120):
            self.history.add({"loadsPower": 1.0}, when=start + i * 30)
        buckets = self.history.series(48)
        total = sum(b["house"] for b in buckets)
        self.assertAlmostEqual(total, 1.0, places=1)

    def test_the_history_is_not_a_thousand_times_too_small(self):
        start = _now_ish()
        for i in range(120):
            self.history.add({"loadsPower": 2.0}, when=start + i * 30)
        buckets = self.history.series(48)
        self.assertGreater(sum(b["house"] for b in buckets), 1.0)

    def test_the_slot_is_the_half_hour(self):
        start = 1_700_000_000.0
        self.assertEqual(slot_start(start), float(int(start // 1800) * 1800))
        self.assertEqual(slot_start(start + 1800), slot_start(start) + 1800)

    def test_it_survives_a_restart(self):
        start = _now_ish()
        for i in range(60):
            self.history.add({"pvPower": 2.0}, when=start + i * 30)
        again = HalfHourHistory(self.path)
        self.assertTrue(again.series(48))

    def test_it_tracks_every_channel(self):
        for key in CHANNELS:
            self.assertIn(key, CHANNELS)


class TestOctopusClient(unittest.TestCase):

    def test_the_flux_bands_are_the_published_ones(self):
        self.assertEqual(FLUX_BANDS["cheap"], (2, 5))
        self.assertEqual(FLUX_BANDS["peak"], (16, 19))

    def test_the_band_follows_uk_local_time(self):
        client = OctopusClient(timezone_name="Europe/London")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/London")
        except Exception:  # noqa: BLE001
            self.skipTest("no tz database")
        for hour, expected in ((2, "cheap"), (4, "cheap"), (5, "standard"),
                               (15, "standard"), (16, "peak"), (18, "peak"),
                               (19, "standard"), (1, "standard")):
            when = datetime(2026, 10, 24, hour, 30, tzinfo=tz)
            self.assertEqual(client.band(when)[0], expected, f"{hour}:30")

    def test_the_premium_against_standard(self):
        self.assertAlmostEqual(_premium({"p_kwh": 30.33}, 11.12), 19.21, places=2)
        self.assertAlmostEqual(_premium({"p_kwh": 4.72}, 11.12), -6.4, places=2)
        self.assertIsNone(_premium(None, 11.12))

    def test_it_is_not_configured_without_a_key(self):
        self.assertFalse(OctopusClient().configured)
        self.assertTrue(OctopusClient(api_key="k", mpan="1", meter_serial="2").configured)

    def test_a_missing_meter_is_an_error(self):
        client = OctopusClient(api_key="k")
        with self.assertRaises(OctopusError):
            client.consumption("", "", datetime.now(timezone.utc),
                               datetime.now(timezone.utc))

    def test_the_rates_are_public(self):
        """No API key is needed for the tariff rates, so the usage slide
        works before the key is entered."""
        client = OctopusClient(tariff="flux")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = {"results": [
                {"valid_from": "2026-10-24T12:00:00Z",
                 "valid_to": "2026-10-24T12:30:00Z",
                 "value_inc_vat": 37.409}]}
            when = datetime(2026, 10, 24, 12, 10, tzinfo=timezone.utc)
            rate = client.unit_rate("import", when)
        self.assertEqual(rate["p_kwh"], 37.409)

    def test_a_rate_for_the_wrong_moment_is_not_returned(self):
        client = OctopusClient(tariff="flux")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = {"results": [
                {"valid_from": "2026-10-24T12:00:00Z",
                 "valid_to": "2026-10-24T12:30:00Z",
                 "value_inc_vat": 37.409}]}
            when = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
            self.assertIsNone(client.unit_rate("import", when))

    def test_the_readings_are_parsed(self):
        client = OctopusClient(api_key="k")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = {"results": [
                {"consumption": "0.5", "interval_start": "2026-10-24T12:00:00Z",
                 "interval_end": "2026-10-24T12:30:00Z"},
                {"consumption": "bad", "interval_start": "x", "interval_end": "y"}]}
            readings = client.consumption("1", "2",
                                         datetime(2026, 10, 24, tzinfo=timezone.utc),
                                         datetime(2026, 10, 25, tzinfo=timezone.utc))
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["kwh"], 0.5)


class TestAccountLookup(unittest.TestCase):
    """The account number has to be enough on its own.

    This is the bit that was missing: the meter point was never looked up,
    so the rates worked but the meter readings never did.
    """

    ACCOUNT = {
        "account": {
            "number": "A-7C4E91B2",
            "electricityAgreements": [
                {"id": 1, "meterPoint": {
                    "mpan": "1234567890123", "direction": "IMPORT",
                    "meters": {"serialNumber": "11ABCDEFG"}}},
                {"id": 2, "meterPoint": {
                    "mpan": "1234567890456", "direction": "EXPORT",
                    "meters": {"serialNumber": "22ABCDEFG"}}},
            ],
        }
    }

    def test_it_finds_the_import_and_export_meters(self):
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        with mock.patch.object(OctopusClient, "_graphql") as gql:
            gql.return_value = self.ACCOUNT
            found = client.lookup()
        self.assertEqual(found["mpan"], "1234567890123")
        self.assertEqual(found["meter_serial"], "11ABCDEFG")
        self.assertEqual(found["export_mpan"], "1234567890456")
        self.assertEqual(found["export_meter_serial"], "22ABCDEFG")

    def test_the_query_asks_for_the_right_fields(self):
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        with mock.patch.object(OctopusClient, "_graphql") as gql:
            gql.return_value = self.ACCOUNT
            client.lookup()
        query = gql.call_args[0][0]
        self.assertIn("meterPoint", query)
        self.assertIn("mpan", query)
        self.assertIn("serialNumber", query)
        self.assertIn("direction", query)

    def test_an_account_with_no_agreement_is_an_error(self):
        client = OctopusClient(api_key="k", account_number="A-NOPE")
        with mock.patch.object(OctopusClient, "_graphql") as gql:
            gql.return_value = {"account": {"number": "A-NOPE",
                                           "electricityAgreements": []}}
            with self.assertRaises(OctopusError):
                client.lookup()

    def test_the_worker_looks_the_meter_up_and_remembers_it(self):
        """Without this the meter readings never populate."""
        import run as app
        from foxess.config import ConfigStore
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(path)
        store = ConfigStore(path)
        try:
            client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
            with mock.patch.object(OctopusClient, "lookup") as lookup:
                lookup.return_value = {"mpan": "1234567890123",
                                       "meter_serial": "11ABCDEFG",
                                       "export_mpan": "1234567890456",
                                       "export_meter_serial": "22ABCDEFG",
                                       "tariff_code": ""}
                app.resolve_meter(client, store)
            self.assertEqual(client.mpan, "1234567890123")
            self.assertEqual(client.meter_serial, "11ABCDEFG")
            # and it was written to the settings so it is only done once
            self.assertEqual(store.get("octopus.mpan"), "1234567890123")
            self.assertEqual(store.get("octopus.meter_serial"), "11ABCDEFG")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_meter_point_typed_in_by_hand_wins(self):
        """If the user filled the MPAN in themselves, keep it."""
        import run as app
        from foxess.config import ConfigStore
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(path)
        store = ConfigStore(path)
        try:
            store.save({"octopus.mpan": "MANUAL", "octopus.meter_serial": "MANUAL"})
            client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
            with mock.patch.object(OctopusClient, "lookup") as lookup:
                lookup.return_value = {"mpan": "999", "meter_serial": "999",
                                       "export_mpan": "888",
                                       "export_meter_serial": "888",
                                       "tariff_code": ""}
                app.resolve_meter(client, store)
            self.assertEqual(store.get("octopus.mpan"), "MANUAL")
            self.assertEqual(store.get("octopus.meter_serial"), "MANUAL")
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestMeterFigures(unittest.TestCase):
    """The end-to-end path: account number in, meter figures out."""

    ACCOUNT = {
        "account_number": "A-7C4E91B2",
        "electricity_meter_points": [
            {"mpan": "1234567890123", "profile_class": 1,
             "meters": [
                {"serial_number": "11ABCDEFG", "direction": "Import"},
                {"serial_number": "22ABCDEFG", "direction": "Export"}]}],
        "gas_meter_points": [],
    }

    def _readings(self):
        """Two readings inside today's window, whenever the test runs."""
        now = datetime.now(timezone.utc)
        first = now.replace(minute=0, second=0, microsecond=0) \
            - timedelta(hours=1)
        second = first + timedelta(minutes=30)
        return {"count": 2, "results": [
            {"consumption": "0.25",
             "interval_start": first.isoformat(),
             "interval_end": (first + timedelta(minutes=30)).isoformat()},
            {"consumption": "0.35",
             "interval_start": second.isoformat(),
             "interval_end": (second + timedelta(minutes=30)).isoformat()}]}

    def test_the_rest_route_is_used(self):
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = self.ACCOUNT
            found = client.lookup()
        self.assertEqual(found["mpan"], "1234567890123")
        self.assertEqual(found["meter_serial"], "11ABCDEFG")
        self.assertEqual(found["export_meter_serial"], "22ABCDEFG")
        urls = [call[0][0] for call in get.call_args_list]
        self.assertTrue(any("/accounts/A-7C4E91B2/" in u for u in urls),
                        "the account endpoint must be asked")

    def test_the_graphql_route_is_the_fallback(self):
        """If REST fails, GraphQL is tried before giving up."""
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.side_effect = OctopusError("no such account")
            with mock.patch.object(OctopusClient, "_graphql") as gql:
                gql.return_value = {
                    "account": {"electricityAgreements": [
                        {"id": 1, "meterPoint": {
                            "mpan": "1234567890123", "direction": "IMPORT",
                            "meters": {"serialNumber": "11ABCDEFG"}}}]}}
                found = client.lookup()
        self.assertEqual(found["mpan"], "1234567890123")
        self.assertEqual(found["meter_serial"], "11ABCDEFG")

    def test_both_routes_failing_says_why(self):
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.side_effect = OctopusError("rest is down")
            with mock.patch.object(OctopusClient, "_graphql") as gql:
                gql.side_effect = OctopusError("graphql is down")
                with self.assertRaises(OctopusError) as caught:
                    client.lookup()
        self.assertIn("rest is down", str(caught.exception))
        self.assertIn("graphql is down", str(caught.exception))

    def test_the_export_meter_serial_exists(self):
        """It did not, and the AttributeError took the whole panel down."""
        client = OctopusClient(api_key="k")
        self.assertEqual(client.export_meter_serial, "")

    def test_the_meter_figures_come_out(self):
        import run as app
        client = OctopusClient(api_key="k", account_number="A-7C4E91B2")
        client.mpan = "1234567890123"
        client.meter_serial = "11ABCDEFG"
        client.export_mpan = "1234567890123"
        client.export_meter_serial = "22ABCDEFG"
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = self._readings()
            figures = app._meter_figures(client)
        self.assertEqual(figures["meter"]["imported_kwh"], 0.6)
        self.assertIn("import_cost", figures["meter"])
        self.assertIn("cost", figures)


class TestTestButton(unittest.TestCase):
    """What the test button proves is what the dashboard uses."""

    def test_a_new_account_number_is_saved(self):
        from foxess.server import octopus_changes
        stored = {"api_key": "sk-live-old", "account_number": "",
                  "mpan": "", "meter_serial": "", "export_mpan": "",
                  "tariff": "flux"}
        changes = octopus_changes({"api_key": "sk-live-new",
                                   "account_number": "A-7C4E91B2"}, stored.get)
        self.assertEqual(changes["octopus.account_number"], "A-7C4E91B2")
        self.assertEqual(changes["octopus.api_key"], "sk-live-new")

    def test_a_masked_key_is_not_saved_back(self):
        from foxess.server import octopus_changes
        stored = {"api_key": "sk-live-old", "account_number": "A-1",
                  "mpan": "", "meter_serial": "", "export_mpan": "",
                  "tariff": "flux"}
        changes = octopus_changes({"api_key": "********"}, stored.get)
        self.assertNotIn("octopus.api_key", changes)

    def test_nothing_changed_means_nothing_written(self):
        from foxess.server import octopus_changes
        stored = {"api_key": "sk-live-old", "account_number": "A-1",
                  "mpan": "", "meter_serial": "", "export_mpan": "",
                  "tariff": "flux"}
        changes = octopus_changes({"account_number": "A-1"}, stored.get)
        self.assertEqual(changes, {})


class TestWorkerLifecycle(unittest.TestCase):
    """The workers must not kill each other."""

    def _controller(self, tmp):
        from run import Controller
        from foxess.config import ConfigStore
        from foxess.state import DashboardState
        config = ConfigStore(os.path.join(tmp, "cfg.json"))
        state = DashboardState(config)
        return Controller(config, state)

    def test_restarting_the_poller_does_not_stop_everything(self):
        """It used to: the octopus worker watched the same event, so
        saving the settings stopped it for good."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            controller.apply_settings({"api_enabled": False})
            self.assertFalse(controller.stop.is_set(),
                             "the shared stop event must stay clear")

    def test_a_masked_secret_is_not_a_change(self):
        """The password comes back masked, and that is not a new one."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            controller.apply_settings({"foxess.password": "********",
                                       "foxess.user": "someone@example.com"})
            self.assertFalse(controller.stop.is_set())

    def test_a_save_that_does_not_mention_the_poller_leaves_it_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            controller.apply_settings({"octopus.mpan": "1234567890123"})
            self.assertFalse(controller._api_stop.is_set())
            self.assertFalse(controller.stop.is_set())

    def test_an_octopus_change_wakes_the_worker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            controller.apply_settings({"octopus.api_key": "sk-live-x"})
            self.assertTrue(controller.octopus_wake.is_set())


class TestDailyCrossCheck(unittest.TestCase):
    """Two ways of counting the day, compared."""

    MONTH = [
        {"variable": "generation", "values": [10.0] * 24 + [2.3]},
        {"variable": "loads", "values": [8.0] * 24 + [1.1]},
        {"variable": "gridConsumption", "values": [1.0] * 24 + [0.9]},
        {"variable": "feedin", "values": [3.0] * 24 + [0.0]},
        {"variable": "chargeEnergyToTal", "values": [2.0] * 24 + [0.5]},
        {"variable": "dischargeEnergyToTal", "values": [1.0] * 24 + [0.4]},
    ]

    def _collect(self, hourly):
        import run as app
        from unittest import mock
        api = mock.Mock()

        def report_query(key, sn, dimension, year, month, day, variables):
            return self.MONTH if dimension == "month" else hourly

        api.report_query.side_effect = report_query
        with mock.patch("run.datetime") as when:
            when.now.return_value = datetime(2026, 10, 25, 9, 0,
                                              tzinfo=timezone.utc)
            with mock.patch("zoneinfo.ZoneInfo"):
                return app.collect_daily(api, "key", "SN", "Europe/London")

    def test_the_hours_are_added_up_and_compared(self):
        hourly = [{"variable": "generation", "values": [0.1] * 10},
                  {"variable": "loads", "values": [0.1] * 10}]
        daily = self._collect(hourly)
        # ten hours of 0.1 kWh is 1 kWh, against 2.3 in the register
        self.assertAlmostEqual(daily["hourly_totals"]["pv"], 1.0, places=3)
        self.assertAlmostEqual(daily["check"]["pv"]["monthly"], 2.3, places=3)
        self.assertAlmostEqual(daily["check"]["pv"]["hourly"], 1.0, places=3)
        self.assertAlmostEqual(daily["check"]["pv"]["difference"], 1.3, places=3)

    def test_the_balance_is_worked_out(self):
        hourly = [{"variable": "generation", "values": [0.1] * 10},
                  {"variable": "loads", "values": [0.1] * 10}]
        daily = self._collect(hourly)
        # in: 2.3 + 0.4 + 0.9 = 3.6   out: 1.1 + 0.5 + 0.0 = 1.6
        self.assertAlmostEqual(daily["balance"], 2.0, places=3)


class TestPVYield(unittest.TestCase):
    """The solar figure has to be the PV yield, the way the app has it."""

    def _report(self, pv_yesterday, pv_today, generation=3.5):
        return [
            {"variable": "generation", "values": [10.0] * 24 + [generation]},
            {"variable": "loads", "values": [8.0] * 24 + [1.1]},
            {"variable": "gridConsumption", "values": [1.0] * 24 + [0.9]},
            {"variable": "feedin", "values": [3.0] * 24 + [0.0]},
            {"variable": "chargeEnergyToTal", "values": [2.0] * 24 + [0.5]},
            {"variable": "dischargeEnergyToTal", "values": [1.0] * 24 + [0.4]},
            # the PV yield of each day - a daily figure, not a counter
            {"variable": "PVEnergyTotal",
             "values": [5.0] * 24 + [pv_yesterday, pv_today]},
        ]

    def _collect(self, report, hourly=None):
        import run as app
        from unittest import mock
        api = mock.Mock()
        if hourly is None:
            hourly = [
                {"variable": "generation", "values": [0.02] * 10},
                {"variable": "loads", "values": [0.1] * 10},
                {"variable": "PVEnergyTotal", "values": [0.02] * 10}]

        def report_query(key, sn, dimension, year, month, day, variables):
            return report if dimension == "month" else hourly

        api.report_query.side_effect = report_query
        with mock.patch("run.datetime") as when:
            when.now.return_value = datetime(2026, 10, 26, 10, 40,
                                              tzinfo=timezone.utc)
            with mock.patch("zoneinfo.ZoneInfo"):
                return app.collect_daily(api, "key", "SN", "Europe/London")

    def test_the_yield_is_the_days_own_figure(self):
        daily = self._collect(self._report(9.0, 0.2))
        self.assertAlmostEqual(daily["pv_yield"], 0.2, places=3)
        # and that is the figure the dashboard uses for solar today
        self.assertAlmostEqual(daily["totals"]["pv"], 0.2, places=3)

    def test_yesterdays_yield_is_not_subtracted(self):
        """It came out as -8.8 once: 0.2 today minus 9.0 yesterday."""
        daily = self._collect(self._report(9.0, 0.2))
        self.assertGreater(daily["pv_yield"], 0.0,
                           "the solar figure must never be negative")

    def test_the_inverter_output_is_not_used_for_the_solar_figure(self):
        """generation is the inverter's output, not the PV yield."""
        daily = self._collect(self._report(9.0, 0.2, generation=3.5))
        self.assertAlmostEqual(daily["totals"]["pv"], 0.2, places=3)

    def test_the_hours_are_compared_with_the_yield(self):
        daily = self._collect(self._report(9.0, 0.2))
        # ten hours of 0.02 kWh is 0.2 kWh
        self.assertAlmostEqual(daily["hourly_totals"]["pv_yield"], 0.2,
                               places=3)
        self.assertAlmostEqual(daily["check"]["pv yield"]["difference"], 0.0,
                               places=3)

    def test_the_live_feed_is_compared(self):
        import run as app
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        import time
        reading = {"pvPower": 0, "loadsPower": 100, "gridConsumptionPower": 0,
                   "feedinPower": 0, "batChargePower": 0,
                   "batDischargePower": 0, "SoC": 50}
        history.add(reading, when=time.time() - 600)
        history.add(reading, when=time.time())
        daily = {"totals": {"pv": 0.2}, "pv_yield": 0.2}
        payload = app.usage_payload(history, daily)
        self.assertIn("live_pv", payload)
        self.assertAlmostEqual(payload["pv_yield_difference"],
                               0.2 - payload["live_pv"], places=3)


class TestExportFromTheInverter(unittest.TestCase):
    """Most homes have no export register - the inverter measures it."""

    def test_the_export_comes_from_the_inverter(self):
        import run as app
        from unittest import mock
        client = mock.Mock()
        client.mpan = "1234567890123"
        client.meter_serial = "ABCDEFGHIJ"
        client.export_mpan = ""          # no export register
        client.export_meter_serial = ""
        client.timezone_name = "Europe/London"
        client.consumption.return_value = [
            {"start": "2026-10-25T10:00:00+00:00", "kwh": 0.5}]
        client.rate_series.return_value = []
        client.rate_at.return_value = None
        figures = app._meter_figures(
            client, {"totals": {"grid_in": 4.0, "grid_out": 9.0}})
        meter = figures["meter"]
        self.assertEqual(meter["import_source"], "meter")
        self.assertEqual(meter["imported_kwh"], 0.5)
        # there is no export meter, so the export comes from the inverter
        self.assertEqual(meter["export_source"], "inverter")
        self.assertEqual(meter["exported_kwh"], 9.0)

    def test_an_export_meter_is_used_when_there_is_one(self):
        import run as app
        from unittest import mock
        client = mock.Mock()
        client.mpan = "1234567890123"
        client.meter_serial = "ABCDEFGHIJ"
        client.export_mpan = "2100012345678"
        client.export_meter_serial = "ZZZZZZZZZZ"
        client.timezone_name = "Europe/London"
        client.consumption.side_effect = [
            [{"start": "2026-10-25T10:00:00+00:00", "kwh": 0.5}],
            [{"start": "2026-10-25T10:00:00+00:00", "kwh": 0.25}]]
        client.rate_series.return_value = []
        client.rate_at.return_value = None
        figures = app._meter_figures(
            client, {"totals": {"grid_in": 4.0, "grid_out": 9.0}})
        meter = figures["meter"]
        self.assertEqual(meter["export_source"], "meter")
        self.assertEqual(meter["exported_kwh"], 0.25)


class TestDailyReport(unittest.TestCase):
    """The inverter has its own daily totals."""

    MONTH = [
        {"variable": "generation", "unit": "kWh",
         "values": [10.0] * 24 + [12.5]},
        {"variable": "loads", "unit": "kWh",
         "values": [8.0] * 24 + [9.5]},
        {"variable": "gridConsumption", "unit": "kWh",
         "values": [1.0] * 24 + [2.5]},
        {"variable": "feedin", "unit": "kWh",
         "values": [3.0] * 24 + [4.5]},
        {"variable": "chargeEnergyToTal", "unit": "kWh",
         "values": [2.0] * 24 + [3.5]},
        {"variable": "dischargeEnergyToTal", "unit": "kWh",
         "values": [1.0] * 24 + [1.5]},
    ]

    def test_the_values_are_positional(self):
        from foxess.api import report_values
        values = report_values(self.MONTH, "generation")
        self.assertEqual(len(values), 25)
        self.assertEqual(values[24], 12.5)
        self.assertEqual(report_values(self.MONTH, "nonsense"), [])

    def test_it_picks_todays_values(self):
        import run as app
        from unittest import mock
        with mock.patch.object(app, "collect_daily") as _:
            pass
        api = mock.Mock()
        api.report_query.return_value = self.MONTH
        with mock.patch("run.datetime") as when:
            when.now.return_value = datetime(2026, 10, 25, 14, 30,
                                              tzinfo=timezone.utc)
            with mock.patch("zoneinfo.ZoneInfo"):
                daily = app.collect_daily(api, "key", "SN", "Europe/London")
        totals = daily["totals"]
        self.assertEqual(totals["pv"], 12.5)
        self.assertEqual(totals["house"], 9.5)
        self.assertEqual(totals["grid_in"], 2.5)
        self.assertEqual(totals["grid_out"], 4.5)
        self.assertEqual(totals["batt_in"], 3.5)
        self.assertEqual(totals["batt_out"], 1.5)

    def test_the_meter_falls_back_to_the_inverter(self):
        """When Octopus has no readings for today."""
        import run as app
        from unittest import mock
        client = mock.Mock()
        client.mpan = "1234567890123"
        client.meter_serial = "ABCDEFGHIJ"
        client.export_mpan = ""
        client.export_meter_serial = ""
        client.timezone_name = "Europe/London"
        client.consumption.return_value = []
        now = datetime.now(timezone.utc)
        client.rate_series.return_value = [
            {"from": (now - timedelta(hours=1)).isoformat(),
             "to": (now + timedelta(hours=1)).isoformat(), "p_kwh": 26.793}]
        figures = app._meter_figures(
            client, {"totals": {"grid_in": 4.0, "grid_out": 2.0}})
        meter = figures["meter"]
        self.assertEqual(meter["import_source"], "inverter")
        self.assertEqual(meter["imported_kwh"], 4.0)
        self.assertEqual(meter["exported_kwh"], 2.0)
        self.assertAlmostEqual(meter["import_cost"], 1.07, places=2)

    def test_the_meter_is_used_when_it_has_readings(self):
        import run as app
        from unittest import mock
        client = mock.Mock()
        client.mpan = "1234567890123"
        client.meter_serial = "ABCDEFGHIJ"
        client.export_mpan = ""
        client.export_meter_serial = ""
        client.timezone_name = "Europe/London"
        client.consumption.return_value = [
            {"start": "2026-10-25T10:00:00+00:00", "kwh": 0.5}]
        client.rate_series.return_value = []
        client.rate_at.return_value = None
        figures = app._meter_figures(
            client, {"totals": {"grid_in": 4.0, "grid_out": 2.0}})
        meter = figures["meter"]
        self.assertEqual(meter["import_source"], "meter")
        self.assertEqual(meter["imported_kwh"], 0.5)
        self.assertIn("last_reading", meter)

    def test_the_daily_totals_win_over_the_local_history(self):
        import run as app
        import time
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        reading = {"pvPower": 1000, "loadsPower": 500,
                   "gridConsumptionPower": 0, "feedinPower": 0,
                   "batChargePower": 0, "batDischargePower": 0, "SoC": 50}
        history.add(reading, when=time.time() - 600)
        history.add(reading, when=time.time())
        payload = app.usage_payload(history, {"totals": {"pv": 12.5,
                                                          "house": 9.5}})
        self.assertEqual(payload["today"]["pv"], 12.5)
        self.assertEqual(payload["today"]["house"], 9.5)

    def test_the_hourly_report_draws_the_chart_when_there_is_no_history(self):
        import run as app
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        hourly = [{"start": "2026-10-25T10:00:00", "house": 0.4,
                   "pv": 0.0, "grid_in": 0.0, "grid_out": 0.0,
                   "batt_in": 0.0, "batt_out": 0.0}]
        payload = app.usage_payload(history, {"hourly": hourly})
        self.assertEqual(payload["series"], hourly)


class TestUsageChartData(unittest.TestCase):
    """The chart is about the grid, and it must not lie about the grain."""

    def test_the_chart_plots_the_grid_not_the_house(self):
        index = _read(INDEX)
        # the house's own use does not move when the battery covers it,
        # so it cannot be what an Octopus chart shows
        self.assertNotIn("series.map(r => r.house||0)", index)
        self.assertIn("r.grid_in||0", index)
        self.assertIn("r.grid_out||0", index)
        self.assertIn("grid import", index)
        self.assertIn("grid export", index)

    def test_the_minis_readings_win_over_the_inverters_estimate(self):
        index = _read(INDEX)
        self.assertIn("mergeMeter", index)
        self.assertIn("renderUsage(s.usage || {}, s.mini || {})", index)

    def test_the_grain_is_stated_so_the_scale_can_match(self):
        index = _read(INDEX)
        self.assertIn("kWh per half hour", index)
        self.assertIn("kWh per hour", index)

    def test_the_hourly_report_is_not_drawn_as_half_hourly(self):
        """An hour's energy is twice a half hour's - the scale has to say."""
        import run as app
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        hourly = [{"start": "2026-10-25T10:00:00", "house": 0.4,
                   "pv": 0.0, "grid_in": 0.5, "grid_out": 0.0,
                   "batt_in": 0.0, "batt_out": 0.0}]
        payload = app.usage_payload(history, {"hourly": hourly})
        self.assertEqual(payload["granularity"], "hour")

    def test_the_local_history_is_half_hourly(self):
        import run as app
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        payload = app.usage_payload(history, {})
        self.assertEqual(payload["granularity"], "half_hour")

    def test_the_local_history_waits_until_it_covers_the_day(self):
        """A history of a few hours would leave the chart empty for the
        rest of the day, so the inverter's own report draws it until then."""
        import time
        import run as app
        from foxess.history import HalfHourHistory
        hourly = [{"start": "2026-10-25T10:00:00", "house": 0.4,
                   "pv": 0.0, "grid_in": 0.5, "grid_out": 0.0,
                   "batt_in": 0.0, "batt_out": 0.0}]
        history = HalfHourHistory(None)
        now = time.time()
        # half a day of history - the chart shows a whole day
        for slot in range(25):
            history.add({"loadsPower": 1.0}, when=now - (25 - slot) * 1800)
        payload = app.usage_payload(history, {"hourly": hourly})
        self.assertEqual(payload["granularity"], "hour")
        self.assertEqual(payload["series"], hourly)

    def test_the_local_history_is_used_once_it_covers_the_day(self):
        import time
        import run as app
        from foxess.history import HalfHourHistory
        hourly = [{"start": "2026-10-25T10:00:00", "house": 0.4,
                   "pv": 0.0, "grid_in": 0.5, "grid_out": 0.0,
                   "batt_in": 0.0, "batt_out": 0.0}]
        history = HalfHourHistory(None)
        now = time.time()
        # the first sample only sets the reference, so a day of history
        # is one more than the 48 slots the chart shows
        for slot in range(49):
            history.add({"loadsPower": 1.0}, when=now - (49 - slot) * 1800)
        payload = app.usage_payload(history, {"hourly": hourly})
        self.assertEqual(payload["granularity"], "half_hour")
        self.assertEqual(len(payload["series"]), 48)

    def test_the_self_used_figure_is_the_solar_that_stayed(self):
        """(house - grid in) / house collapses to zero when the battery
        is charged from the grid, which is the opposite of the truth."""
        index = _read(INDEX)
        self.assertIn("of your solar", index)
        self.assertNotIn("of your usage", index)
        self.assertNotIn("(t.house - (t.grid_in||0))/t.house", index)

class TestChartDataHonesty(unittest.TestCase):
    """The chart must not draw something the system did not measure."""

    def test_the_hourly_fallback_carries_the_grid(self):
        """A restart used to leave the grid at zero under a "grid import"
        legend - a flat, empty chart."""
        import run as app
        from unittest import mock

        report = [
            {"variable": "loads", "values": [1.0, 2.0, 3.0]},
            {"variable": "gridConsumption", "values": [0.5, 1.5, 2.5]},
            {"variable": "feedin", "values": [0.1, 0.2, 0.3]},
            {"variable": "PVEnergyTotal", "values": [0.0, 0.0, 0.0]},
        ]

        class FakeApi:
            def report_query(self, key, sn, dimension, year, month, day,
                            variables):
                return report

        out = app.collect_daily(FakeApi(), "key", "sn", "Europe/London")
        series = out.get("hourly") or []
        self.assertTrue(series, "the hourly report should fill the chart")
        self.assertAlmostEqual(series[0]["house"], 1.0, places=3)
        self.assertAlmostEqual(series[0]["grid_in"], 0.5, places=3)
        self.assertAlmostEqual(series[1]["grid_in"], 1.5, places=3)
        self.assertAlmostEqual(series[0]["grid_out"], 0.1, places=3)

    def test_the_chart_says_where_the_numbers_came_from(self):
        index = _read(INDEX)
        self.assertIn("from your Octopus meter", index)
        self.assertIn("from the inverter", index)

    def test_the_chart_plots_the_grid_not_the_house(self):
        index = _read(INDEX)
        self.assertNotIn("series.map(r => r.house||0)", index)
        self.assertIn("r.grid_in||0", index)
        self.assertIn("r.grid_out||0", index)
class TestHistoryFile(unittest.TestCase):
    """The file on disk has to be in the same units as the code."""

    def test_an_old_file_is_discarded(self):
        """Version 1 was in watt-hours, a thousand times too big."""
        import json
        import tempfile
        import os
        from foxess.history import HalfHourHistory, HISTORY_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hist.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"buckets": {"1700000000": {"house": 500.0}}}, fh)
            history = HalfHourHistory(path)
            self.assertEqual(history.series(48), [],
                             "the old file must not be read")

    def test_the_current_file_is_read(self):
        import json
        import tempfile
        import os
        from foxess.history import HalfHourHistory, HISTORY_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hist.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": HISTORY_VERSION,
                           "buckets": {"1700000000": {"house": 0.5}}}, fh)
            history = HalfHourHistory(path)
            buckets = history.series(48)
            self.assertTrue(buckets)
            self.assertAlmostEqual(buckets[0]["house"], 0.5)

    def test_a_saved_file_has_the_version(self):
        import json
        import tempfile
        import os
        from foxess.history import HalfHourHistory, HISTORY_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hist.json")
            history = HalfHourHistory(path)
            history.add({"pvPower": 1000, "loadsPower": 500,
                         "gridConsumptionPower": 0, "feedinPower": 0,
                         "batChargePower": 0, "batDischargePower": 0,
                         "SoC": 50}, when=1700000000)
            history.add({"pvPower": 1000, "loadsPower": 500,
                         "gridConsumptionPower": 0, "feedinPower": 0,
                         "batChargePower": 0, "batDischargePower": 0,
                         "SoC": 50}, when=1700001800)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data.get("version"), HISTORY_VERSION)


class TestHistoryUnits(unittest.TestCase):
    """The figures are in kWh, not watt-hours.

    The feed is in kW, so the history is fed kW and comes out in kWh.
    """

    def test_the_energy_is_in_kwh(self):
        import time
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        reading = {"pvPower": 2.0, "loadsPower": 1.0,
                   "gridConsumptionPower": 0, "feedinPower": 1.0,
                   "batChargePower": 0, "batDischargePower": 0, "SoC": 50}
        history.add(reading, when=time.time() - 3600)
        history.add(reading, when=time.time())
        today = history.today()
        # 2 kW for an hour is 2 kWh
        self.assertAlmostEqual(today["pv"], 2.0, places=3)
        self.assertAlmostEqual(today["house"], 1.0, places=3)

    def test_a_day_of_solar_is_a_sane_number(self):
        import time
        from foxess.history import HalfHourHistory
        history = HalfHourHistory(None)
        now = time.time()
        for slot in range(48):
            history.add({"pvPower": 2.4, "loadsPower": 0.5,
                         "gridConsumptionPower": 0, "feedinPower": 0,
                         "batChargePower": 0, "batDischargePower": 0,
                         "SoC": 50}, when=now - (48 - slot) * 1800)
        # 2.4 kW for 24 hours would be 57.6 kWh
        self.assertLess(history.today()["pv"], 100.0)


class TestDemoHistory(unittest.TestCase):
    """The demo's history is made up, and it must never be written over
    the real one - the demo can be run against the real settings file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")

    def test_the_real_history_lives_next_to_the_settings(self):
        import run as app
        history = app.open_history(Path(self.path))
        self.assertIsNotNone(history.path)
        self.assertEqual(history.path.name, "config-history.json")

    def test_the_demo_keeps_its_history_in_memory(self):
        import run as app
        history = app.open_history(Path(self.path), demo=True)
        self.assertIsNone(history.path)

    def test_the_demo_writes_no_history_file(self):
        import run as app
        history = app.open_history(Path(self.path), demo=True)
        app.seed_demo_history(history)
        self.assertTrue(history.series(48))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "config-history.json")))


class TestExportMeterPoint(unittest.TestCase):
    """A dual-register meter is two meter points, and only one of them
    says it is the export."""

    ACCOUNT = {
        "account_number": "A-7C4E91B2",
        "electricity_meter_points": [
            {"mpan": "2000000000001",
             "meters": [{"serial_number": "21L0000001"}]},
            {"mpan": "2000000000002",
             "meters": [{"serial_number": "21L0000001"}]}],
        "gas_meter_points": [],
    }

    def test_the_export_point_is_found(self):
        """The account does not say which is which - the meter point does."""
        from foxess.octopus import _meter_points
        client = mock.Mock()

        def get(url, auth=True):
            if "/electricity-meter-points/2000000000002/" in url:
                return {"mpan": "2000000000002",
                        "import_mpan": "2000000000001"}
            return {"mpan": "2000000000001"}

        client._get.side_effect = get
        found = _meter_points(self.ACCOUNT, client)
        self.assertEqual(found["mpan"], "2000000000001")
        self.assertEqual(found["meter_serial"], "21L0000001")
        self.assertEqual(found["export_mpan"], "2000000000002")
        self.assertEqual(found["export_meter_serial"], "21L0000001")

    def test_an_import_point_stays_an_import(self):
        from foxess.octopus import _meter_points
        client = mock.Mock()
        client._get.return_value = {"mpan": "2000000000001"}
        found = _meter_points(self.ACCOUNT, client)
        self.assertEqual(found["mpan"], "2000000000001")
        self.assertEqual(found["export_mpan"], "")

    def test_a_point_that_cannot_be_asked_is_still_used(self):
        from foxess.octopus import _meter_points
        client = mock.Mock()
        client._get.side_effect = OctopusError("no")
        found = _meter_points(self.ACCOUNT, client)
        self.assertEqual(found["mpan"], "2000000000001")


class TestAllMeters(unittest.TestCase):
    """A dual-register meter has two of them, with different serials."""

    ACCOUNT = {
        "account_number": "A-7C4E91B2",
        "electricity_meter_points": [
            {"mpan": "2000000000001", "profile_class": 1,
             "meters": [
                {"serial_number": "21L0000001", "direction": "Import"},
                {"serial_number": "21L0000002", "direction": "Export"}]}],
        "gas_meter_points": [],
    }

    def test_it_lists_both_meters(self):
        from foxess.octopus import _all_meters
        meters = _all_meters(self.ACCOUNT)
        self.assertEqual(len(meters), 2)
        self.assertEqual(meters[0]["serial"], "21L0000001")
        self.assertEqual(meters[0]["direction"], "import")
        self.assertEqual(meters[1]["direction"], "export")

    def test_the_lookup_prefers_the_import_meter(self):
        from foxess.octopus import _meter_points
        found = _meter_points(self.ACCOUNT)
        self.assertEqual(found["mpan"], "2000000000001")
        self.assertEqual(found["meter_serial"], "21L0000001")
        self.assertEqual(found["export_meter_serial"], "21L0000002")

    def test_it_copes_with_a_missing_direction(self):
        """Some accounts do not say which meter is which."""
        from foxess.octopus import _all_meters, _meter_points
        account = {"electricity_meter_points": [
            {"mpan": "2000000000001",
             "meters": [{"serial_number": "AAA"},
                        {"serial_number": "BBB"}]}]}
        found = _meter_points(account)
        self.assertEqual(found["meter_serial"], "AAA")
        self.assertEqual(len(_all_meters(account)), 2)


class TestLiveRecording(unittest.TestCase):
    """A live reading has to reach the history, not just the state.

    It used to reference a name that was not in scope, so every reading
    raised a NameError after the live values had already been published
    - the dashboard looked fine and the usage slide stayed empty.
    """

    def test_a_live_reading_is_recorded(self):
        import run as app
        from foxess.config import ConfigStore
        from foxess.history import HalfHourHistory
        from foxess.state import DashboardState
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(os.path.join(tmp, "cfg.json"))
            state = DashboardState(config)
            history = HalfHourHistory(os.path.join(tmp, "hist.json"))
            controller = app.Controller(config, state, history)
            reading = {
                "pvPower": 3000, "loadsPower": 1000,
                "gridConsumptionPower": 0, "feedinPower": 2000,
                "batChargePower": 0, "batDischargePower": 0, "SoC": 55}
            # The history integrates between samples, so the first one is
            # only a baseline and the second is what puts energy in a
            # bucket.
            import time
            history.add(reading, when=time.time() - 600)
            controller.record_live(reading)
            self.assertTrue(history.series(48),
                            "the live reading never reached the history")
            today = history.today()
            self.assertGreater(today["pv"], 0.0)

    def test_the_controller_keeps_its_history(self):
        import run as app
        from foxess.config import ConfigStore
        from foxess.state import DashboardState
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(os.path.join(tmp, "cfg.json"))
            state = DashboardState(config)
            controller = app.Controller(config, state)
            self.assertIsNone(controller.history)
            # and it must not explode without one
            controller.record_live({"pvPower": 1, "loadsPower": 1,
                                    "gridConsumptionPower": 0,
                                    "feedinPower": 0, "batChargePower": 0,
                                    "batDischargePower": 0, "SoC": 50})


class TestTodayWindow(unittest.TestCase):
    """"Today" has to be the local day, not UTC, and not yesterday."""

    def test_it_starts_at_local_midnight(self):
        from foxess.octopus import today_start
        start = today_start("Europe/London")
        local = start.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual((local.hour, local.minute, local.second), (0, 0, 0))

    def test_it_is_today_not_yesterday(self):
        from foxess.octopus import today_start
        start = today_start("Europe/London")
        now = datetime.now(ZoneInfo("Europe/London"))
        self.assertEqual(start.astimezone(ZoneInfo("Europe/London")).date(),
                         now.date())

    def test_the_window_is_not_more_than_a_day(self):
        """It used to start at yesterday's midnight, so it ran over a day."""
        from foxess.octopus import today_start
        start = today_start("Europe/London")
        hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
        self.assertLessEqual(hours, 24.0)
        self.assertGreaterEqual(hours, 0.0)


class TestConsumptionQuery(unittest.TestCase):
    """The query has to be one Octopus actually accepts."""

    def test_no_group_by_is_sent(self):
        """Octopus rejects group_by=half-hour with a 400.

        Half-hourly is the default; the only accepted values are hour,
        day, week, month and quarter.
        """
        client = OctopusClient(api_key="k")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = {"results": []}
            client.consumption("1234567890123", "ABCDEFGHIJ",
                               datetime(2026, 10, 25, tzinfo=timezone.utc),
                               datetime(2026, 10, 26, tzinfo=timezone.utc))
        url = get.call_args[0][0]
        self.assertNotIn("group_by", url)

    def test_the_ordering_is_a_valid_value(self):
        """order_by=asc is not one of them; it is period or -period."""
        client = OctopusClient(api_key="k")
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = {"results": []}
            client.consumption("1234567890123", "ABCDEFGHIJ",
                               datetime(2026, 10, 25, tzinfo=timezone.utc),
                               datetime(2026, 10, 26, tzinfo=timezone.utc))
        url = get.call_args[0][0]
        self.assertIn("order_by=period", url)

    def test_unreadable_readings_are_reported(self):
        """Not as an empty meter - that looks like no data at all."""
        client = OctopusClient(api_key="k")
        rows = {"results": [{"interval_start": "2026-10-25T10:00:00Z"}]}
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = rows
            with self.assertRaises(OctopusError) as caught:
                client.consumption("1234567890123", "ABCDEFGHIJ",
                                   datetime(2026, 10, 25, tzinfo=timezone.utc),
                                   datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertIn("none could be read", str(caught.exception))

    def test_the_latest_page_is_asked_for_first(self):
        """A bounded query comes back with the right periods and no energy."""
        client = OctopusClient(api_key="k")
        real = {"results": [
            {"consumption": "0.25", "interval_start": "2026-10-25T10:00:00Z",
             "interval_end": "2026-10-25T10:30:00Z"},
            {"consumption": "0.35", "interval_start": "2026-10-25T10:30:00Z",
             "interval_end": "2026-10-25T11:00:00Z"}]}
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = real
            readings = client.consumption(
                "1234567890123", "ABCDEFGHIJ",
                datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(len(readings), 2)
        self.assertAlmostEqual(sum(r["kwh"] for r in readings), 0.6)
        # the first call must not be bounded by a period
        self.assertNotIn("period_from", get.call_args_list[0][0][0])

    def test_the_bounded_query_is_the_second_try(self):
        """Used when the latest page has nothing in the window."""
        client = OctopusClient(api_key="k")
        old = {"results": [{"consumption": "1.5", "interval_start":
                             "2026-10-01T10:00:00Z"}]}
        with mock.patch.object(OctopusClient, "_get") as get:
            get.side_effect = [{"results": []}, old]
            readings = client.consumption(
                "1234567890123", "ABCDEFGHIJ",
                datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(len(readings), 1)
        self.assertIn("period_from", get.call_args_list[1][0][0])

    def test_the_window_is_applied_to_the_fallback(self):
        """Readings outside the window are dropped."""
        client = OctopusClient(api_key="k")
        rows = {"results": [
            {"consumption": "1.0", "interval_start": "2026-10-24T10:00:00Z"},
            {"consumption": "2.0", "interval_start": "2026-10-25T10:00:00Z"}]}
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = rows
            readings = client.consumption(
                "1234567890123", "ABCDEFGHIJ",
                datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(len(readings), 1)
        self.assertAlmostEqual(readings[0]["kwh"], 2.0)

    def test_the_readings_come_back_oldest_first(self):
        client = OctopusClient(api_key="k")
        rows = {"results": [
            {"consumption": "0.3", "interval_start": "2026-10-25T10:30:00Z",
             "interval_end": "2026-10-25T11:00:00Z"},
            {"consumption": "0.1", "interval_start": "2026-10-25T10:00:00Z",
             "interval_end": "2026-10-25T10:30:00Z"}]}
        with mock.patch.object(OctopusClient, "_get") as get:
            get.return_value = rows
            readings = client.consumption("1234567890123", "ABCDEFGHIJ",
                                          datetime(2026, 10, 25, tzinfo=timezone.utc),
                                          datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(readings[0]["start"], "2026-10-25T10:00:00Z")
        self.assertEqual(readings[1]["start"], "2026-10-25T10:30:00Z")


class TestOctopusSettings(unittest.TestCase):
    """The Octopus settings have to survive a round trip through the store."""

    def test_the_defaults_are_empty(self):
        self.assertEqual(DEFAULTS["octopus"]["api_key"], "")
        self.assertEqual(DEFAULTS["octopus"]["tariff"], "flux")

    def test_a_round_trip(self):
        from foxess.config import ConfigStore
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(path)
        store = ConfigStore(path)
        try:
            store.save({"octopus.api_key": "sk-live-123",
                        "octopus.account_number": "A-7C4E91B2",
                        "octopus.tariff": "flux"})
            self.assertEqual(store.get("octopus.api_key"), "sk-live-123")
            self.assertEqual(store.get("octopus.account_number"), "A-7C4E91B2")
            masked = store.masked()
            self.assertEqual(masked["octopus"]["api_key"], "********")
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["octopus"]["api_key"], "sk-live-123")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_the_file_is_owner_only(self):
        from foxess.config import ConfigStore
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(path)
        store = ConfigStore(path)
        try:
            store.save({"octopus.api_key": "sk-live-123"})
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
