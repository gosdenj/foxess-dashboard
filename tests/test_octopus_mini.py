"""Tests for the Octopus Mini - the live meter data.

The Mini reads the meter itself, so its numbers are live rather than a
day behind.  They only come from the GraphQL API, and that wants the
account's email and password rather than the API key.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import DEFAULTS, EDITABLE, SECRET_PATHS  # noqa: E402
from foxess.octopus import (  # noqa: E402
    GRAPHQL,
    OctopusError,
    OctopusMini,
    _add_deltas,
)
from foxess.state import DashboardState  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
SETTINGS = os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class FakeResponse:
    """Stands in for the urlopen result."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


class TestMiniClient(unittest.TestCase):

    def test_needs_an_account_number_and_a_way_in(self):
        self.assertFalse(OctopusMini().configured)
        self.assertFalse(OctopusMini(email="a@b.com").configured)
        self.assertFalse(OctopusMini(email="a@b.com", password="x").configured)
        self.assertTrue(OctopusMini(email="a@b.com", password="x",
                                    account_number="A-1").configured)

    def test_the_api_key_alone_is_enough(self):
        """The API key gets a session token, so no password is needed."""
        self.assertTrue(OctopusMini(api_key="sk-live-X",
                                    account_number="A-1").configured)
        self.assertFalse(OctopusMini(api_key="sk-live-X").configured)

    def test_the_token_comes_from_the_api_key(self):
        """The API key gets a token on its own - no password needed."""
        mini = OctopusMini(api_key="sk-live-SECRET", account_number="A-1")
        seen = {}

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            seen["query"] = body["query"]
            seen["variables"] = body["variables"]
            seen["auth"] = request.get_header("Authorization")
            return FakeResponse({"data": {"obtainKrakenToken":
                                          {"token": "kraken-123"}}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(mini.token(), "kraken-123")
        self.assertIn("obtainKrakenToken", seen["query"])
        self.assertEqual(seen["variables"]["input"]["APIKey"],
                         "sk-live-SECRET")
        # the token request itself must not carry a token
        self.assertNotIn("kraken-123", seen["auth"] or "")

    def test_the_api_key_is_tried_before_the_email_and_password(self):
        mini = OctopusMini(email="a@b.com", password="secret",
                           account_number="A-1", api_key="sk-live-SECRET")
        asked = []

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            asked.append(body["variables"]["input"])
            return FakeResponse({"data": {"obtainKrakenToken":
                                          {"token": "kraken-123"}}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            mini.token()
        self.assertEqual(len(asked), 1)
        self.assertIn("APIKey", asked[0])

    def test_the_email_and_password_are_a_fallback(self):
        """If the API key is not accepted, the login is tried next."""
        mini = OctopusMini(email="a@b.com", password="secret",
                           account_number="A-1", api_key="sk-live-SECRET")
        asked = []

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            asked.append(body["variables"]["input"])
            if "APIKey" in body["variables"]["input"]:
                return FakeResponse({"errors": [{"message": "Invalid data."}]})
            return FakeResponse({"data": {"obtainKrakenToken":
                                          {"token": "kraken-123"}}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(mini.token(), "kraken-123")
        self.assertEqual(len(asked), 2)
        self.assertIn("APIKey", asked[0])
        self.assertEqual(asked[1]["email"], "a@b.com")

    def test_the_token_comes_from_the_email_and_password(self):
        mini = OctopusMini(email="a@b.com", password="secret",
                           account_number="A-1")
        seen = {}

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            seen["query"] = body["query"]
            seen["variables"] = body["variables"]
            seen["auth"] = request.get_header("Authorization")
            return FakeResponse({"data": {"obtainKrakenToken":
                                          {"token": "kraken-123"}}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(mini.token(), "kraken-123")
        self.assertIn("obtainKrakenToken", seen["query"])
        self.assertEqual(seen["variables"]["input"]["email"], "a@b.com")
        self.assertEqual(seen["variables"]["input"]["password"], "secret")
        # the token request itself must not carry a token
        self.assertNotIn("kraken-123", seen["auth"] or "")

    def test_the_token_is_kept_and_not_fetched_again(self):
        mini = OctopusMini(email="a@b.com", password="secret",
                           account_number="A-1")
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return FakeResponse({"data": {"obtainKrakenToken":
                                          {"token": "kraken-123"}}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            mini.token()
            mini.token()
        self.assertEqual(len(calls), 1)

    def test_a_bad_password_is_reported(self):
        mini = OctopusMini(email="a@b.com", password="wrong",
                           account_number="A-1")
        with mock.patch("urllib.request.urlopen", lambda *a, **k: FakeResponse(
                {"errors": [{"message": "Invalid email or password"}]})):
            with self.assertRaises(OctopusError) as caught:
                mini.token()
        self.assertIn("Invalid email or password", str(caught.exception))

    def test_nothing_to_log_in_with_says_so_plainly(self):
        mini = OctopusMini(email="a@b.com", account_number="A-1")
        with self.assertRaises(OctopusError) as caught:
            mini.token()
        self.assertIn("API key", str(caught.exception))

    def test_the_error_says_what_went_wrong(self):
        """Octopus's "Invalid data." on its own tells you nothing."""
        mini = OctopusMini(api_key="sk-live-WRONG", account_number="A-1")
        with mock.patch("urllib.request.urlopen", lambda *a, **k: FakeResponse(
                {"errors": [{"message": "Invalid data.",
                             "extensions": {"validationErrors": [
                                 {"message": "Authentication failed."}]}}]})):
            with self.assertRaises(OctopusError) as caught:
                mini.token()
        self.assertIn("Authentication failed", str(caught.exception))

    def test_the_device_id_comes_from_the_smart_meter(self):
        """The old "octocare" query is retired, so the meter is asked."""
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        seen = {}

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            seen["query"] = body["query"]
            seen["variables"] = body["variables"]
            seen["auth"] = request.get_header("Authorization")
            payload = {
                "data": {
                    "account": {
                        "electricityAgreements": [
                            {
                                "meterPoint": {
                                    "mpan": "2000000000001",
                                    "direction": "IMPORT",
                                    "meters": {
                                        "serialNumber": "21L0000001",
                                        "smartImportElectricityMeter": {
                                            "deviceId": "12-34-5A-FF-FF-6B-C7-89",
                                            "id": "node-1",
                                        },
                                    },
                                },
                            },
                        ],
                    },
                },
            }
            return FakeResponse(payload)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(mini.meter_device_id(),
                             "12-34-5A-FF-FF-6B-C7-89")
        self.assertIn("smartImportElectricityMeter", seen["query"])
        self.assertNotIn("octocareUsageInfo", seen["query"])
        self.assertEqual(seen["variables"]["account"], "A-1")
        self.assertEqual(seen["auth"], "kraken-123")

    def test_no_smart_meter_is_reported(self):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        with mock.patch("urllib.request.urlopen", lambda *a, **k: FakeResponse(
                {"data": {"account": {"electricityAgreements": []}}})):
            with self.assertRaises(OctopusError) as caught:
                mini.meter_device_id()
        self.assertIn("smart meter", str(caught.exception).lower())

    def test_the_telemetry_query_asks_for_what_we_need(self):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        seen = {}

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            seen.update(body)
            return FakeResponse({"data": {"smartMeterTelemetry": [
                {"readAt": "2026-10-26T10:00:00Z", "demand": 420,
                 "consumption": 12345, "export": 678,
                 "consumptionDelta": 250, "costDeltaWithTax": 7},
            ]}})

        start = datetime(2026, 10, 26, tzinfo=timezone.utc)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            rows = mini.telemetry(start, start + timedelta(hours=1))
        self.assertIn("smartMeterTelemetry", seen["query"])
        self.assertEqual(seen["variables"]["meterDeviceId"], "device-9")
        self.assertEqual(seen["variables"]["grouping"], "HALF_HOURLY")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["demand"], 420)
        self.assertEqual(rows[0]["consumption"], 12345)
        self.assertEqual(rows[0]["export"], 678)
        self.assertEqual(rows[0]["cost"], 7)

    def test_a_graphql_error_is_raised_not_swallowed(self):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        with mock.patch("urllib.request.urlopen", lambda *a, **k: FakeResponse(
                {"errors": [{"message": "Field 'nope' is not defined"}]})):
            with self.assertRaises(OctopusError):
                mini.telemetry(datetime.now(timezone.utc),
                               datetime.now(timezone.utc))


class TestMiniTokenRefresh(unittest.TestCase):

    def test_an_expired_token_is_refreshed_once(self):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "old-token"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        calls = []

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            calls.append(request.get_header("Authorization"))
            if "obtainKrakenToken" in body["query"]:
                return FakeResponse({"data": {"obtainKrakenToken":
                                              {"token": "new-token"}}})
            if request.get_header("Authorization") == "old-token":
                return FakeResponse({"errors": [{"message": "token expired"}]})
            return FakeResponse({"data": {"smartMeterTelemetry": []}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            rows = mini.telemetry(datetime.now(timezone.utc),
                                  datetime.now(timezone.utc))
        self.assertEqual(rows, [])
        # the old token was tried, then a new one was fetched and used
        self.assertIn("old-token", calls)
        self.assertIn("new-token", calls)
        self.assertEqual(mini._token, "new-token")

    def test_a_real_error_is_not_retried_forever(self):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "old-token"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        calls = []

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            calls.append(body["query"])
            if "obtainKrakenToken" in body["query"]:
                return FakeResponse({"data": {"obtainKrakenToken":
                                              {"token": "new-token"}}})
            return FakeResponse({"errors": [{"message": "no such field"}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(OctopusError):
                mini.telemetry(datetime.now(timezone.utc),
                               datetime.now(timezone.utc))
        # one token fetch, two attempts at the query - not an endless loop
        self.assertEqual(len(calls), 3)


class TestTolerantParsing(unittest.TestCase):
    """Octopus's GraphQL is not consistent about what is a list."""

    @contextlib.contextmanager
    def _mini(self, agreements):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        payload = {"data": {"account": {"electricityAgreements": agreements}}}
        with mock.patch("urllib.request.urlopen",
                        lambda *a, **k: FakeResponse(payload)):
            yield mini

    def test_a_meter_point_given_as_a_list_is_found(self):
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": {"serialNumber": "21L0000001",
                            "smartImportElectricityMeter": {
                                "deviceId": "eui64-1"}}}
        with self._mini([{"meterPoint": [point]}]) as mini:
            self.assertEqual(mini.meter_device_id(), "eui64-1")

    def test_meters_given_as_a_list_are_found(self):
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": [{"serialNumber": "21L0000001",
                             "smartImportElectricityMeter": {
                                 "deviceId": "eui64-2"}}]}
        with self._mini([{"meterPoint": point}]) as mini:
            self.assertEqual(mini.meter_device_id(), "eui64-2")

    def test_the_smart_meter_given_as_a_list_is_found(self):
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": {"serialNumber": "21L0000001",
                            "smartImportElectricityMeter": [
                                {"deviceId": "eui64-3"}]}}
        with self._mini([{"meterPoint": point}]) as mini:
            self.assertEqual(mini.meter_device_id(), "eui64-3")

    def test_the_export_meter_is_used_when_there_is_no_import_one(self):
        point = {"mpan": "2000000000002", "direction": "EXPORT",
                 "meters": {"serialNumber": "21L0000001",
                            "smartExportElectricityMeter": {
                                "deviceId": "eui64-4"}}}
        with self._mini([{"meterPoint": point}]) as mini:
            self.assertEqual(mini.meter_device_id(), "eui64-4")

    def test_the_smart_devices_list_is_used_as_well(self):
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": {"serialNumber": "21L0000001",
                            "smartDevices": [{"deviceId": "eui64-6"}]}}
        with self._mini([{"meterPoint": point}]) as mini:
            self.assertEqual(mini.meter_device_id(), "eui64-6")

    def test_the_error_says_what_the_reply_looked_like(self):
        """So the next attempt shows what actually came back."""
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": {"serialNumber": "21L0000001"}}
        with self._mini([{"meterPoint": point}]) as mini:
            with self.assertRaises(OctopusError) as caught:
                mini.meter_device_id()
        message = str(caught.exception)
        self.assertIn("no smart meter", message)
        self.assertIn("the reply was:", message)
        # the shape, not the data
        self.assertIn("electricityAgreements", message)
        self.assertNotIn("2000000000001", message)

    def test_a_list_of_agreements_that_are_not_dicts_is_survived(self):
        with self._mini([None, "nonsense", {"meterPoint": None}]) as mini:
            with self.assertRaises(OctopusError):
                mini.meter_device_id()

    def test_the_account_given_as_a_list_is_survived(self):
        point = {"mpan": "2000000000001", "direction": "IMPORT",
                 "meters": {"smartImportElectricityMeter": {
                     "deviceId": "eui64-5"}}}
        agreement = {"electricityAgreements": [{"meterPoint": point}]}
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        payload = {"data": {"account": [agreement]}}
        with mock.patch("urllib.request.urlopen",
                        lambda *a, **k: FakeResponse(payload)):
            self.assertEqual(mini.meter_device_id(), "eui64-5")


class TestMiniDeltas(unittest.TestCase):

    def test_the_counters_become_the_energy_of_each_period(self):
        rows = [
            {"consumption": 1000.0, "export": 500.0},
            {"consumption": 1500.0, "export": 500.0},
            {"consumption": 1800.0, "export": 900.0},
        ]
        _add_deltas(rows)
        # the period after each reading
        self.assertEqual(rows[0]["consumption_delta"], 500.0)
        self.assertEqual(rows[1]["consumption_delta"], 300.0)
        self.assertEqual(rows[2]["consumption_delta"], 0.0)
        # the export comes from the difference to the next reading
        self.assertEqual(rows[0]["export_delta"], 0.0)
        self.assertEqual(rows[1]["export_delta"], 400.0)
        self.assertEqual(rows[2]["export_delta"], 0.0)

    def test_the_meters_own_period_delta_is_preferred(self):
        """Octopus says how much energy each period used - use it."""
        rows = [
            {"consumption": 1000.0, "export": 500.0, "delta": 480.0},
            {"consumption": 1480.0, "export": 500.0, "delta": 310.0},
        ]
        _add_deltas(rows)
        self.assertEqual(rows[0]["consumption_delta"], 480.0)
        self.assertEqual(rows[1]["consumption_delta"], 310.0)

    def test_a_counter_that_goes_backwards_does_not_go_negative(self):
        rows = [{"consumption": 100.0, "export": 0.0},
                {"consumption": 50.0, "export": 0.0}]
        _add_deltas(rows)
        self.assertEqual(rows[1]["consumption_delta"], 0.0)


class TestMiniToday(unittest.TestCase):

    def _mini_with(self, rows):
        mini = OctopusMini(api_key="sk-live-X", account_number="A-1")
        mini._token = "kraken-123"
        mini._device_id = "device-9"
        mini._device_ids = ["device-9"]
        mini.telemetry = mock.Mock(return_value=rows)
        return mini

    def test_today_is_the_local_day_not_the_utc_one(self):
        """The Mini's counters are read from local midnight onwards."""
        mini = self._mini_with([])
        with mock.patch("foxess.octopus.today_start") as fake_start:
            fake_start.return_value = datetime(2026, 10, 26, 0, 0,
                                               tzinfo=timezone.utc)
            mini.today()
        self.assertEqual(fake_start.call_args[0][0], "Europe/London")
        # the query starts an hour early, so the counter just before
        # midnight is caught and the day's energy is a true difference
        start = mini.telemetry.call_args_list[0][0][0]
        self.assertEqual(start, datetime(2026, 10, 25, 23, 0,
                                          tzinfo=timezone.utc))

    def test_today_start_is_local_midnight(self):
        from foxess.octopus import today_start
        when = today_start("Europe/London")
        local = when.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual(local.hour, 0)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.date(), datetime.now(
            ZoneInfo("Europe/London")).date())

    def test_today_totals_come_from_the_deltas(self):
        rows = [
            {"read_at": "2026-10-26T00:00:00Z", "consumption": 1000.0,
             "export": 100.0, "demand": 100.0, "cost": 5.0},
            {"read_at": "2026-10-26T00:30:00Z", "consumption": 1300.0,
             "export": 100.0, "demand": 200.0, "cost": 7.0},
            {"read_at": "2026-10-26T01:00:00Z", "consumption": 1600.0,
             "export": 400.0, "demand": 300.0, "cost": 9.0},
        ]
        mini = self._mini_with(rows)
        today = mini.today()
        # 600 Wh of consumption, 300 Wh of export, 21p of cost
        self.assertAlmostEqual(today["consumption_kwh"], 0.6, places=3)
        self.assertAlmostEqual(today["export_kwh"], 0.3, places=3)
        self.assertAlmostEqual(today["cost"], 0.21, places=2)
        self.assertEqual(len(today["series"]), 3)

    def test_the_live_reading_is_the_last_one(self):
        rows = [
            {"read_at": "2026-10-26T00:00:00Z", "demand": 100.0,
             "consumption": 1000.0, "export": 0.0},
            {"read_at": "2026-10-26T00:01:00Z", "demand": 420.0,
             "consumption": 1010.0, "export": 0.0},
        ]
        mini = self._mini_with(rows)
        live = mini.live()
        self.assertEqual(live["demand_w"], 420.0)

    def test_no_readings_is_not_a_crash(self):
        mini = self._mini_with([])
        self.assertEqual(mini.live(), {})
        self.assertEqual(mini.today(), {"series": [], "readings": 0,
                                       "partial": True})

    def test_a_shorter_window_is_marked_as_partial(self):
        """A part-day total must not be passed off as a whole day."""
        mini = self._mini_with([])
        with mock.patch("foxess.octopus.datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 26, 12, 0,
                                             tzinfo=timezone.utc)
            fake.side_effect = datetime
            today = mini.today()
        self.assertTrue(today.get("partial"))

    def test_a_shorter_window_is_tried_when_the_day_comes_back_empty(self):
        """The Mini may not keep a whole day of half-hourly readings."""
        mini = self._mini_with([])
        with mock.patch("foxess.octopus.datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 26, 12, 0,
                                             tzinfo=timezone.utc)
            fake.side_effect = datetime
            mini.today()
        # the first call is from midnight, the second is the fallback
        self.assertEqual(len(mini.telemetry.call_args_list), 2)
        self.assertEqual(mini.telemetry.call_args_list[1][0][0],
                         datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc))


class TestMiniSettings(unittest.TestCase):

    def test_the_password_is_a_secret(self):
        self.assertIn("octopus.password", SECRET_PATHS)

    def test_the_email_and_password_are_editable(self):
        self.assertIn("octopus.email", EDITABLE)
        self.assertIn("octopus.password", EDITABLE)

    def test_the_defaults_are_there(self):
        self.assertEqual(DEFAULTS["octopus"]["email"], "")
        self.assertEqual(DEFAULTS["octopus"]["password"], "")

    def test_the_settings_page_has_the_fields(self):
        page = _read(SETTINGS)
        self.assertIn("octopus_email", page)
        self.assertIn("octopus_password", page)
        self.assertIn('octopus_email:"octopus.email"', page)
        self.assertIn('octopus_password:"octopus.password"', page)

    def test_the_password_field_is_masked(self):
        page = _read(SETTINGS)
        self.assertIn('type="password" id="octopus_password"', page)

    def test_the_environment_can_seed_them(self):
        source = _read(os.path.join(os.path.dirname(__file__), "..",
                                    "run.py"))
        self.assertIn('"octopus.api_key": "OCTOPUS_API_KEY"', source)
        self.assertIn('"octopus.password": "OCTOPUS_PASSWORD"', source)


class TestMiniWorker(unittest.TestCase):

    def test_the_worker_polls_the_mini_and_keeps_going(self):
        import run

        state = DashboardState()
        config = mock.Mock()
        config.get.side_effect = lambda key, default=None: {
            "octopus.email": "a@b.com",
            "octopus.password": "secret",
            "octopus.account_number": "A-1",
        }.get(key, default)

        calls = []

        def fake_live(self):
            calls.append("live")
            return {"demand_w": 420.0}

        def fake_today(self):
            calls.append("today")
            return {"consumption_kwh": 1.5, "export_kwh": 0.5,
                    "cost": 0.2, "series": []}

        stop = mock.Mock()
        stop.is_set.side_effect = [False, True, True]
        wake = mock.Mock()
        wake.wait.return_value = True

        with mock.patch.object(OctopusMini, "live", fake_live), \
                mock.patch.object(OctopusMini, "today", fake_today):
            run.octopus_mini_worker(state, config, stop, wake)

        self.assertIn("live", calls)
        self.assertIn("today", calls)
        self.assertEqual(state.mini()["live"]["demand_w"], 420.0)

    def test_an_error_does_not_kill_the_worker(self):
        import run

        state = DashboardState()
        config = mock.Mock()
        config.get.side_effect = lambda key, default=None: {
            "octopus.email": "a@b.com",
            "octopus.password": "secret",
            "octopus.account_number": "A-1",
        }.get(key, default)

        stop = mock.Mock()
        stop.is_set.side_effect = [False, True, True]
        wake = mock.Mock()
        wake.wait.return_value = True

        def boom(self):
            raise OctopusError("no token came back")

        with mock.patch.object(OctopusMini, "live", boom), \
                mock.patch.object(OctopusMini, "today", boom):
            run.octopus_mini_worker(state, config, stop, wake)

        self.assertIn("error", state.mini())


class TestMiniOnThePanel(unittest.TestCase):

    def test_the_octopus_panel_shows_the_live_reading(self):
        index = _read(INDEX)
        self.assertIn("renderOctopus(s.octopus || {}, s.mini || {})", index)
        # the live reading goes in the panel header, so it does not push
        # the tiles out of the panel
        self.assertIn('id="octLive"', index)
        self.assertIn(" now\"", index)
        self.assertIn("live from your Octopus Mini", index)

    def test_a_small_live_draw_is_shown_in_watts(self):
        """23 W reads better as watts than as 0.02 kW."""
        index = _read(INDEX)
        self.assertIn("demand>=1000", index)
        self.assertIn('Math.round(demand) + " W"', index)

    def test_the_mini_figures_win_over_the_estimated_ones(self):
        index = _read(INDEX)
        self.assertIn("miniImport!=null ? miniImport : meter.imported_kwh", index)
        self.assertIn("miniExport!=null ? miniExport : meter.exported_kwh", index)

    def test_the_snapshot_carries_the_mini_data(self):
        source = _read(os.path.join(os.path.dirname(__file__), "..",
                                    "foxess", "state.py"))
        self.assertIn('"mini": self.mini()', source)

    def test_the_graphql_endpoint_is_the_octopus_one(self):
        self.assertEqual(GRAPHQL, "https://api.octopus.energy/v1/graphql/")


if __name__ == "__main__":
    unittest.main()
