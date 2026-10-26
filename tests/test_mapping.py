"""Tests for the FoxESS dashboard helpers.

    python3 -m unittest discover -s tests -v

Standard library only - no pytest required.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.api import build_headers, device_timestamp, variables_from_real_time  # noqa: E402
from foxess.state import DashboardState  # noqa: E402
from foxess.ws import parse_message, power_kw  # noqa: E402


def node(soc="50.0", bat_power="1000", bat_unit="W", charge="0",
         solar="2000", solar_unit="W", load="1500", load_unit="W",
         grid="500", grid_unit="W", grid_status="3"):
    return {"result": {"node": {
        "bat": {"soc": soc, "power": {"value": bat_power, "unit": bat_unit},
                "charge": charge},
        "solar": {"power": {"value": solar, "unit": solar_unit}},
        "load": {"power": {"value": load, "unit": load_unit}},
        "grid": {"power": {"value": grid, "unit": grid_unit},
                 "gridStatus": grid_status},
    }}}


class TestPowerConversion(unittest.TestCase):
    def test_watts_to_kilowatts(self):
        self.assertAlmostEqual(power_kw({"value": "3141", "unit": "W"}), 3.141)

    def test_kilowatts_pass_through(self):
        self.assertAlmostEqual(power_kw({"value": "3.5", "unit": "kW"}), 3.5)

    def test_missing_unit_treated_as_watts(self):
        self.assertAlmostEqual(power_kw({"value": "2000"}), 2.0)

    def test_numeric_value(self):
        self.assertAlmostEqual(power_kw({"value": 1500, "unit": "W"}), 1.5)

    def test_none_and_garbage(self):
        self.assertIsNone(power_kw(None))
        self.assertIsNone(power_kw({}))
        self.assertIsNone(power_kw({"value": None}))
        self.assertIsNone(power_kw({"value": "abc"}))


class TestParseMessage(unittest.TestCase):
    def test_discharging(self):
        d = parse_message(node(bat_power="3141", charge="0", solar="0",
                               load="3164", grid="0"))
        self.assertAlmostEqual(d["batDischargePower"], 3.141)
        self.assertAlmostEqual(d["batChargePower"], 0.0)
        self.assertAlmostEqual(d["loadsPower"], 3.164)
        self.assertAlmostEqual(d["pvPower"], 0.0)

    def test_charging(self):
        d = parse_message(node(bat_power="2500", charge="1"))
        self.assertAlmostEqual(d["batChargePower"], 2.5)
        self.assertAlmostEqual(d["batDischargePower"], 0.0)

    def test_grid_import(self):
        # load 1500 W, no solar, battery idle -> 1500 W must come from the grid
        d = parse_message(node(bat_power="0", charge="0", solar="0",
                               load="1500", grid="1500", grid_status="3"))
        self.assertAlmostEqual(d["gridConsumptionPower"], 1.5)
        self.assertAlmostEqual(d["feedinPower"], 0.0)

    def test_grid_export(self):
        # solar 2000 W, load 1500 W -> 500 W surplus exported
        d = parse_message(node(bat_power="0", charge="0", solar="2000",
                               load="1500", grid="500", grid_status="4"))
        self.assertAlmostEqual(d["feedinPower"], 0.5)
        self.assertAlmostEqual(d["gridConsumptionPower"], 0.0)

    def test_kilowatt_units_pass_through(self):
        d = parse_message(node(solar="3.5", solar_unit="kW",
                               load="2.4", load_unit="kW"))
        self.assertAlmostEqual(d["pvPower"], 3.5)
        self.assertAlmostEqual(d["loadsPower"], 2.4)

    def test_soc(self):
        d = parse_message(node(soc="23.0"))
        self.assertAlmostEqual(d["SoC"], 23.0)

    def test_empty_frame_is_ignored(self):
        self.assertEqual(parse_message({}), {})
        self.assertEqual(parse_message({"result": {}}), {})


class TestOpenApiResponse(unittest.TestCase):
    def test_device_level_timestamp(self):
        payload = {"errno": 0, "result": [{
            "deviceSN": "TEST00000000001",
            "time": "2026-10-24 19:58:19 BST+0100",
            "datas": [
                {"variable": "pvPower", "unit": "kW", "value": 0.0},
                {"variable": "SoC", "unit": "%", "value": 24.0},
            ]}]}
        flat = variables_from_real_time(payload["result"])
        self.assertEqual(flat["pvPower"], 0.0)
        self.assertEqual(flat["SoC"], 24.0)
        self.assertEqual(device_timestamp(payload["result"]),
                         "2026-10-24 19:58:19 BST+0100")

    def test_non_numeric_values_are_skipped(self):
        payload = [{"deviceSN": "x", "time": "t", "datas": [
            {"variable": "currentFault", "value": ""},
            {"variable": "SoC", "value": 50.0}]}]
        flat = variables_from_real_time(payload)
        self.assertNotIn("currentFault", flat)
        self.assertEqual(flat["SoC"], 50.0)


class TestSignature(unittest.TestCase):
    def test_open_api_uses_real_crlf(self):
        headers = build_headers("/op/v1/device/real/query", "TOKEN", "en")
        expected = hashlib.md5(
            f"/op/v1/device/real/query\r\nTOKEN\r\n{headers['timestamp']}".encode()
        ).hexdigest()
        self.assertEqual(headers["signature"], expected)

    def test_headers_present(self):
        headers = build_headers("/op/v1/device/real/query", "TOKEN")
        for key in ("token", "timestamp", "signature", "lang", "Content-Type"):
            self.assertIn(key, headers)


class TestState(unittest.TestCase):
    def test_snapshot_shape(self):
        state = DashboardState()
        state.set_live({"pvPower": 1.0, "SoC": 50.0})
        state.set_totals({"generation": 100.0})
        state.set_meta(plant_name="Test")
        snap = state.snapshot()
        self.assertEqual(snap["live"]["pvPower"], 1.0)
        self.assertEqual(snap["totals"]["generation"], 100.0)
        self.assertEqual(snap["meta"]["plant_name"], "Test")
        self.assertTrue(snap["connected"])
        self.assertIsNotNone(snap["live_age_s"])

    def test_json_serialisable(self):
        state = DashboardState()
        state.set_live({"pvPower": 1.0})
        json.dumps(state.snapshot(), default=str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
