# Display clients

Two ways to put the dashboard on a screen.

| Target | Needs | Latency | Notes |
|---|---|---|---|
| **Raspberry Pi + LCD** | Chromium, an LCD | ~5 s | The full web dashboard, all settings |
| **ESP32 + SPI LCD** | MicroPython, a panel | ~2 s (poll) | A purpose-built native renderer |

Both read the same `/api/state` endpoint from the dashboard server. **The
server always runs on the Pi** (or anywhere on the LAN) — it holds the
WebSocket session, the TLS connection and the FoxESS credentials. The ESP32
is just a dumb display terminal, so none of that has to fit in 320 KB of
RAM.

```
  ┌──────────────────┐        HTTP /api/state        ┌──────────────┐
  │  ESP32 + LCD     │ ────────────────────────────► │              │
  │  (display only)  │ ◄─────── JSON snapshot ─────── │   dashboard  │
  └──────────────────┘                                │    server    │
  ┌──────────────────┐        HTTP /events (SSE)      │  (the Pi)    │
  │  Pi + LCD        │ ────────────────────────────► │              │
  │  (Chromium)      │ ◄────── live push ──────────── │              │
  └──────────────────┘                                └──────────────┘
```

---

## Raspberry Pi

**Target: Raspberry Pi 3 + official 7" touch display, 800x480.** The dashboard
has a dedicated layout for exactly that panel - larger type, the power-flow
diagram across the full width, and live tiles beside the energy totals.

### 1. Install the dashboard

```bash
git clone <your-repo> foxess-dashboard
cd foxess-dashboard
python3 run.py            # check it works
```

Then open the settings page in a browser and fill in your credentials:

```
http://<pi-ip>:8765/settings
```

### 2. Run it as a kiosk

```bash
python3 display/pi_kiosk.py
```

That starts the server (if it is not already running) and puts Chromium in
kiosk mode on the attached display.

For a 7" official Pi touchscreen:

```bash
export DISPLAY=:0
xset s off -dpms
xset -dpms s 0
python3 display/pi_kiosk.py --window-size 800,480
```

Useful flags:

| Flag | Why |
|---|---|
| `--no-sandbox` | Chromium's sandbox needs user namespaces, which some Pi kernels lack. Add it if the browser refuses to start. |
| `--window-size 800,480` | Match the panel exactly, with no letterboxing. |
| `--disable-gpu` | Usually **not** wanted - the VideoCore IV GPU makes the CSS flow animation smoother than software rendering. |
| `--no-server` | The server is already running elsewhere. |

### 3. Make it start on boot

`/etc/systemd/system/foxess-dashboard.service`:

```ini
[Unit]
Description=FoxESS dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/foxess-dashboard
Environment=DISPLAY=:0
ExecStart=/usr/bin/python3 /home/pi/foxess-dashboard/display/pi_kiosk.py --window-size 800,480
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
```

```bash
sudo systemctl enable --now foxess-dashboard
sudo systemctl status foxess-dashboard
```

> The settings file lives at `foxess-dashboard/config.json` (or
> `~/.foxess-dashboard/config.json`). It contains your password and API key
> in plaintext, so it is written with `0600` permissions. Keep it out of git.

### 4. Hide the mouse cursor

```bash
sudo apt install unclutter
unclutter -display :0 -idle 0.5 &
```

Add it to the systemd unit as a second `ExecStart=` line if you want it
managed.

### 5. Fonts

The dashboard uses a system font stack — no web fonts, so it works offline and
there is nothing to download at boot:

- **UI text** — `-apple-system`, `Segoe UI`, `Roboto`, `Helvetica Neue`,
  `Liberation Sans`, `DejaVu Sans`, `Noto Sans`, `Arial`, `sans-serif`
- **Numbers** — `ui-monospace`, `SF Mono`, `Menlo`, `Consolas`,
  `Liberation Mono`, `DejaVu Sans Mono`, `monospace`

Raspberry Pi OS ships DejaVu by default. On a minimal install, add it:

```bash
sudo apt install fonts-dejavu-core
```

If you install a nicer font — `fonts-roboto`, `fonts-open-sans`,
`fonts-liberation` — the browser will pick it up automatically if it is already
named in the stack above. For anything else, add it to `--font-ui` in
`static/index.html`.

> Numbers are deliberately monospaced so digits stay in column while they
> change — a live dashboard where the readout jitters is unreadable.

---

### 6. Performance on a Pi 3

The Pi 3 is a 1.2 GHz Cortex-A53 with 1 GB of RAM, so the dashboard is built
to do as little work as possible per update:

- **Live data arrives over Server-Sent Events**, not polling. The fallback
  poll only runs while the event stream is down.
- **The DOM is built once.** Tiles and table rows are created on the first
  snapshot; afterwards only cells whose value actually changed are written.
  Measured: **0 `innerHTML` writes** during continuous updates.
- **Only active wires animate** - the dash-offset animation is removed from
  any path carrying no power, so an idle system costs nothing.
- The panel is sized to the display, so Chromium never rescales a big canvas.

---

## ESP32

### Hardware

* ESP32-WROOM-32 (or any ESP32 with Wi-Fi)
* SPI LCD — ST7789 (240×240 / 320×320), ILI9341 (320×240), SSD1351
* A level shifter for the backlight if your panel needs it

### Software

* [MicroPython](https://micropython.org/)
* [`micropython-lcd-driver`](https://github.com/micropython/micropython-lcd-driver)
  — provides the `lcd` module

On the device:

```bash
# from a shell on the ESP32
mpremote fs cp display/esp32_client.py :main.py
mpremote fs cp display/font5x7.py :
mpremote reset
```

Or in the REPL:

```python
import esp32_client
esp32_client.main()
```

### Configure it

Edit the top of `esp32_client.py` before copying:

```python
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"
SERVER_URL = "http://192.168.1.50:8765"   # the Pi running the server
WIDTH = 320
HEIGHT = 240
ROTATION = 0      # 1 for landscape on a 240x240 ST7789
```

The client polls `/api/state` every 2 seconds and redraws. It shows the four
power flows, the state of charge with your minimum-SoC floor marked in red,
and the live self-sufficiency percentage.

![esp32](esp32-day.png)

### Without a Raspberry Pi?

The ESP32 needs *something* to hold the FoxESS session. If you want the Pi
optional, run the dashboard server on anything with a network connection —
another ESP32 is not realistic for TLS + WebSocket, but a cheap VPS, a NAS,
or your laptop all work.

---

## What the displays show

| | Web dashboard | ESP32 |
|---|---|---|
| Live power flow diagram | ✅ | — (numbers only) |
| PV / Load / Grid / Battery | ✅ | ✅ |
| State of charge + floor marker | ✅ | ✅ |
| Self-sufficiency % | ✅ | ✅ |
| Energy totals (lifetime) | ✅ | — |
| System details | ✅ | — |
| Settings page | ✅ | — |

---

## Tuning the display

The web dashboard is designed to fit without scrolling at 1440×900, 1280×720,
800×480, 480×320 and 320×240. Verify it on your actual panel with:

```bash
python3 layout_check.py          # needs playwright
```

The layout is driven by the settings page — pick `auto` to let it adapt, or
force `full` / `compact` / `tiny`.
