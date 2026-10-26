"""Render the ESP32 client's output offline, as an image.

The ESP32 client cannot be run here, but its drawing code is plain Python -
so we mock the ``lcd`` module and the HTTP layer, render a frame, and write
a PNG.  If the device draws the wrong thing, this shows it.

    python3 tests/test_esp32_render.py
"""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "display"))

from PIL import Image  # noqa: E402

# --- mock the micropython-lcd-driver module -----------------------------
lcd = types.ModuleType("lcd")


class MockLcd:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.buf = [[0] * width for _ in range(height)]

    def fill_rect(self, x, y, w, h, color):
        for yy in range(max(0, y), min(self.height, y + h)):
            row = self.buf[yy]
            for xx in range(max(0, x), min(self.width, x + w)):
                row[xx] = color

    def to_image(self):
        img = Image.new("RGB", (self.width, self.height))
        px = img.load()
        for y in range(self.height):
            for x in range(self.width):
                c = self.buf[y][x]
                px[x, y] = (((c >> 11) & 0x1F) << 3,
                            ((c >> 5) & 0x3F) << 2,
                            (c & 0x1F) << 3)
        return img


lcd.init = lambda **kwargs: None
lcd.MOCK = MockLcd(320, 240)
sys.modules["lcd"] = lcd

import esp32_client as ec  # noqa: E402

# --- sample states -------------------------------------------------------
DAY = {
    "connected": True, "live_age_s": 1.2,
    "live": {"pvPower": 4.12, "loadsPower": 1.56, "gridConsumptionPower": 0.0,
             "feedinPower": 0.61, "batChargePower": 2.51,
             "batDischargePower": 0.0, "SoC": 88.0},
    "meta": {"plant_name": "PO4 - Gosden", "min_soc": 10},
}
EVENING = {
    "connected": True, "live_age_s": 0.8,
    "live": {"pvPower": 0.0, "loadsPower": 2.98, "gridConsumptionPower": 2.98,
             "feedinPower": 0.0, "batChargePower": 0.0,
             "batDischargePower": 0.0, "SoC": 10.0},
    "meta": {"plant_name": "PO4 - Gosden", "min_soc": 10},
}
OFFLINE = None


def render_to_png(state, path):
    disp = MockLcd(320, 240)
    ec.render(disp, state)
    disp.to_image().save(path)
    return disp


def main() -> int:
    out = os.path.join(os.path.dirname(__file__), "..", "docs")
    os.makedirs(out, exist_ok=True)

    for name, state in (("esp32-day", DAY), ("esp32-evening", EVENING),
                        ("esp32-offline", OFFLINE)):
        render_to_png(state, os.path.join(out, f"{name}.png"))
        print(f"  wrote docs/{name}.png")

    # text metrics sanity
    assert ec.text_width("PV") == 11, ec.text_width("PV")
    assert ec.text_width("") == 0
    print("\ntext metrics OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
