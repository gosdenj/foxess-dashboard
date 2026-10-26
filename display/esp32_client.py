"""MicroPython client for an ESP32 driving a small LCD.

The ESP32 is a **thin display terminal**: it does not talk to FoxESS itself,
it polls the dashboard server's ``/api/state`` endpoint and renders what it
finds.  That keeps the ESP32 free of the WebSocket client, the TLS session
and the signing logic - all of which stay on the Pi (or wherever the server
runs).

Hardware
--------
* any ESP32 with Wi-Fi (ESP32-WROOM-32 is plenty)
* an SPI LCD - ST7789 240x240/320x240, ILI9341 320x240, SSD1351...
* ``micropython-lcd-driver`` for the panel, and ``urequests`` for HTTP

Copy to the device
------------------
    # font5x7.py and this file, then:
    >>> import esp32_client
    >>> esp32_client.main()          # or put it in main.py

Edit the three settings below first.
"""

import json
import time

try:
    import network
    import urequests as requests
except ImportError:                                   # CPython, for testing
    import urllib.request as _urllib
    network = None

    class _Requests:
        def get(self, url, timeout=5):
            with _urllib.urlopen(url, timeout=timeout) as r:
                return r

    requests = _Requests()

try:
    import lcd                                        # micropython-lcd-driver
except ImportError:                                   # not on a device
    lcd = None                                         # only used in main()


def require_lcd():
    if lcd is None:
        raise RuntimeError(
            "the 'lcd' module is missing - install micropython-lcd-driver "
            "on the device (see display/README.md)"
        )

# ---------------------------------------------------------------------------
# CONFIG - edit these
# ---------------------------------------------------------------------------
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"

#: Where the dashboard server is running.
SERVER_URL = "http://192.168.1.50:8765"

#: Panel geometry.
WIDTH = 320
HEIGHT = 240
ROTATION = 0          # 0 = portrait, 1 = landscape (ST7789 240x240)
SCALE = 1             # font scale

#: How often to poll, in seconds.
POLL_INTERVAL = 2

# ---------------------------------------------------------------------------
# Colours (RGB565)
# ---------------------------------------------------------------------------
BG = 0x0841
PANEL = 0x0861
LINE = 0x2949
TEXT = 0xEF7B
MUTED = 0x7BEF
DIM = 0x4208
PV = 0xFD20
LOAD = 0x1D4F
GRID = 0xD41F
BAT = 0x2F8F
ALARM = 0xF811


# ---------------------------------------------------------------------------
# Font (5x7, column-major) - kept inline so this file is standalone
# ---------------------------------------------------------------------------
FONT = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00), "!": (0x00, 0x00, 0x5F, 0x00, 0x00),
    '"': (0x00, 0x07, 0x00, 0x07, 0x00), "#": (0x14, 0x7F, 0x14, 0x7F, 0x14),
    "$": (0x24, 0x2A, 0x7F, 0x2A, 0x12), "%": (0x23, 0x13, 0x08, 0x64, 0x62),
    "&": (0x36, 0x49, 0x55, 0x22, 0x50), "'": (0x00, 0x05, 0x03, 0x00, 0x00),
    "(": (0x00, 0x1C, 0x22, 0x41, 0x00), ")": (0x00, 0x41, 0x22, 0x1C, 0x00),
    "*": (0x14, 0x08, 0x3E, 0x08, 0x14), "+": (0x08, 0x08, 0x3E, 0x08, 0x08),
    ",": (0x00, 0x50, 0x30, 0x00, 0x00), "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ".": (0x00, 0x60, 0x60, 0x00, 0x00), "/": (0x20, 0x10, 0x08, 0x04, 0x02),
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E), "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x42, 0x61, 0x51, 0x49, 0x46), "3": (0x21, 0x41, 0x45, 0x4B, 0x31),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10), "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30), "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36), "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00), ";": (0x00, 0x56, 0x36, 0x00, 0x00),
    "<": (0x00, 0x08, 0x14, 0x22, 0x41), "=": (0x14, 0x14, 0x14, 0x14, 0x14),
    ">": (0x41, 0x22, 0x14, 0x08, 0x00), "?": (0x02, 0x01, 0x51, 0x09, 0x06),
    "@": (0x32, 0x49, 0x79, 0x41, 0x3E), "A": (0x7E, 0x11, 0x11, 0x11, 0x7E),
    "B": (0x7F, 0x49, 0x49, 0x49, 0x36), "C": (0x3E, 0x41, 0x41, 0x41, 0x22),
    "D": (0x7F, 0x41, 0x41, 0x22, 0x1C), "E": (0x7F, 0x49, 0x49, 0x49, 0x41),
    "F": (0x7F, 0x09, 0x09, 0x09, 0x01), "G": (0x3E, 0x41, 0x49, 0x49, 0x7A),
    "H": (0x7F, 0x08, 0x08, 0x08, 0x7F), "I": (0x00, 0x41, 0x7F, 0x41, 0x00),
    "J": (0x20, 0x40, 0x41, 0x3F, 0x01), "K": (0x7F, 0x08, 0x14, 0x22, 0x41),
    "L": (0x7F, 0x40, 0x40, 0x40, 0x40), "M": (0x7F, 0x02, 0x0C, 0x02, 0x7F),
    "N": (0x7F, 0x04, 0x08, 0x10, 0x7F), "O": (0x3E, 0x41, 0x41, 0x41, 0x3E),
    "P": (0x7F, 0x09, 0x09, 0x09, 0x06), "Q": (0x3E, 0x41, 0x51, 0x21, 0x5E),
    "R": (0x7F, 0x09, 0x19, 0x29, 0x46), "S": (0x46, 0x49, 0x49, 0x49, 0x31),
    "T": (0x01, 0x01, 0x7F, 0x01, 0x01), "U": (0x3F, 0x40, 0x40, 0x40, 0x3F),
    "V": (0x1F, 0x20, 0x40, 0x20, 0x1F), "W": (0x3F, 0x40, 0x38, 0x40, 0x3F),
    "X": (0x63, 0x14, 0x08, 0x14, 0x63), "Y": (0x07, 0x08, 0x70, 0x08, 0x07),
    "Z": (0x61, 0x51, 0x49, 0x45, 0x43), "[": (0x00, 0x7F, 0x41, 0x41, 0x00),
    "]": (0x00, 0x41, 0x41, 0x7F, 0x00), "_": (0x40, 0x40, 0x40, 0x40, 0x40),
    "a": (0x20, 0x54, 0x54, 0x54, 0x78), "b": (0x7F, 0x48, 0x44, 0x44, 0x38),
    "c": (0x38, 0x44, 0x44, 0x44, 0x20), "d": (0x38, 0x44, 0x44, 0x48, 0x7F),
    "e": (0x38, 0x54, 0x54, 0x54, 0x18), "f": (0x08, 0x7E, 0x09, 0x01, 0x02),
    "g": (0x0C, 0x52, 0x52, 0x52, 0x3E), "h": (0x7F, 0x08, 0x04, 0x04, 0x78),
    "i": (0x00, 0x44, 0x7D, 0x40, 0x00), "j": (0x20, 0x40, 0x44, 0x3D, 0x00),
    "k": (0x7F, 0x10, 0x28, 0x44, 0x00), "l": (0x00, 0x41, 0x7F, 0x40, 0x00),
    "m": (0x7C, 0x04, 0x18, 0x04, 0x78), "n": (0x7C, 0x08, 0x04, 0x04, 0x78),
    "o": (0x38, 0x44, 0x44, 0x44, 0x38), "p": (0x7C, 0x14, 0x14, 0x14, 0x08),
    "q": (0x08, 0x14, 0x14, 0x18, 0x7C), "r": (0x7C, 0x08, 0x04, 0x04, 0x08),
    "s": (0x48, 0x54, 0x54, 0x54, 0x20), "t": (0x04, 0x3F, 0x44, 0x40, 0x20),
    "u": (0x3C, 0x40, 0x40, 0x20, 0x7C), "v": (0x1C, 0x20, 0x40, 0x20, 0x1C),
    "w": (0x3C, 0x40, 0x30, 0x40, 0x3C), "x": (0x44, 0x28, 0x10, 0x28, 0x44),
    "y": (0x0C, 0x50, 0x50, 0x50, 0x3C), "z": (0x44, 0x64, 0x54, 0x4C, 0x44),
    "%": (0x23, 0x13, 0x08, 0x64, 0x62), "k": (0x7F, 0x10, 0x28, 0x44, 0x00),
}
GLYPH_W = 5
GLYPH_H = 7


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def text_width(s, scale=1, spacing=1):
    if not s:
        return 0
    return len(s) * (GLYPH_W + spacing) * scale - spacing * scale


def draw_text(disp, x, y, s, color=TEXT, scale=SCALE, spacing=1):
    """Draw *s* with the built-in font.

    Each glyph row is emitted as horizontal runs rather than per-pixel
    rectangles - roughly 8x fewer draw calls, which matters on an ESP32.
    """
    cx = x
    for ch in s:
        cols = FONT.get(ch)
        if cols is None:
            cx += (GLYPH_W + spacing) * scale
            continue
        for row in range(GLYPH_H):
            run_start = -1
            for col in range(GLYPH_W + 1):
                on = col < GLYPH_W and (cols[col] >> row) & 1
                if on and run_start < 0:
                    run_start = col
                elif not on and run_start >= 0:
                    disp.fill_rect(cx + run_start * scale, y + row * scale,
                                   (col - run_start) * scale, scale, color)
                    run_start = -1
        cx += (GLYPH_W + spacing) * scale
    return cx


def fill(disp, x, y, w, h, color):
    disp.fill_rect(x, y, w, h, color)


# ---------------------------------------------------------------------------
# State fetch
# ---------------------------------------------------------------------------

def fetch_state():
    try:
        response = requests.get(SERVER_URL + "/api/state", timeout=5)
        try:
            return json.loads(response.text)
        finally:
            response.close()
    except Exception as exc:                       # noqa: BLE001
        print("fetch failed:", exc)
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def fmt_kw(v):
    if v is None:
        return "--"
    return "%.2f" % v


def render(disp, state):
    fill(disp, 0, 0, WIDTH, HEIGHT, BG)

    if state is None:
        draw_text(disp, 8, 8, "NO CONNECTION", ALARM, 2)
        draw_text(disp, 8, 30, SERVER_URL, MUTED, 1)
        return

    live = state.get("live") or {}
    meta = state.get("meta") or {}
    connected = state.get("connected")

    # ---- header ----------------------------------------------------
    name = (meta.get("plant_name") or "FoxESS")[:20]
    draw_text(disp, 6, 6, name, TEXT, 1)
    age = state.get("live_age_s")
    right = ("LIVE" if connected else "OFF") + ("" if age is None else " %.0fs" % age)
    draw_text(disp, WIDTH - text_width(right) - 6, 6, right,
              BAT if connected else ALARM, 1)
    fill(disp, 0, 17, WIDTH, 1, LINE)

    # ---- four power tiles -------------------------------------------
    pv = live.get("pvPower")
    load = live.get("loadsPower")
    imp = live.get("gridConsumptionPower") or 0.0
    exp = live.get("feedinPower") or 0.0
    chg = live.get("batChargePower") or 0.0
    dis = live.get("batDischargePower") or 0.0
    soc = live.get("SoC")

    col_w = WIDTH // 4
    tile_y = 24
    tile_h = 54
    tiles = (
        ("PV", fmt_kw(pv), PV),
        ("LOAD", fmt_kw(load), LOAD),
        ("GRID", fmt_kw(imp - exp), GRID),
        ("BATT", fmt_kw(dis - chg), BAT),
    )
    for i, (label, value, colour) in enumerate(tiles):
        x = i * col_w
        fill(disp, x + 2, tile_y, col_w - 4, tile_h, PANEL)
        draw_text(disp, x + 7, tile_y + 7, label, colour, 1)
        draw_text(disp, x + 7, tile_y + 22, value, TEXT, 2)

    # ---- SoC --------------------------------------------------------
    bar_x = 6
    bar_w = WIDTH - 12
    bar_y = 94
    bar_h = 14
    floor = meta.get("min_soc")
    soc_label = "SOC " + ("--" if soc is None else "%.0f%%" % soc)
    draw_text(disp, bar_x, bar_y - 14, soc_label, TEXT, 1)
    if floor is not None:
        floor_text = "FLOOR %d%%" % floor
        draw_text(disp, WIDTH - text_width(floor_text) - bar_x, bar_y - 14,
                  floor_text, MUTED, 1)
    fill(disp, bar_x, bar_y, bar_w, bar_h, PANEL)
    if soc is not None:
        pct = max(0.0, min(100.0, soc))
        fill(disp, bar_x, bar_y, int(bar_w * pct / 100), bar_h, BAT)
        if floor is not None:
            fx = bar_x + int(bar_w * floor / 100)
            fill(disp, fx, bar_y - 4, 3, bar_h + 8, ALARM)

    # ---- self-sufficiency block --------------------------------------
    if load and load > 0.05:
        self_suff = max(0.0, min(1.0, (load - imp) / load))
        head = "SELF-SUFFICIENCY %d%%" % int(self_suff * 100)
        draw_text(disp, 6, 130, head, TEXT, 2)
        if imp > 0.02:
            detail = "drawing %.2f kW from the grid" % imp
        elif exp > 0.02:
            detail = "exporting %.2f kW to the grid" % exp
        else:
            detail = "running on your own power"
        draw_text(disp, 6, 154, detail, MUTED, 1)
    else:
        draw_text(disp, 6, 132, "NO LOAD DETECTED", MUTED, 1)

    # ---- footer ------------------------------------------------------
    foot_y = HEIGHT - 18
    fill(disp, 0, foot_y - 6, WIDTH, 1, LINE)
    foot = "PV %s kW   LOAD %s kW" % (fmt_kw(pv), fmt_kw(load))
    draw_text(disp, 6, foot_y, foot, DIM, 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def connect_wifi():
    if network is None:
        return
    station = network.WLAN(network.STA_IF)
    if station.isconnected():
        return
    station.active(True)
    station.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(30):
        if station.isconnected():
            break
        time.sleep(1)
    print("wifi:", station.ifconfig() if station.isconnected() else "not connected")


def main():
    connect_wifi()
    require_lcd()
    lcd.init(width=WIDTH, height=HEIGHT, rotation=ROTATION)
    while True:
        render(lcd, fetch_state())
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
