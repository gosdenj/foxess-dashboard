# FoxESS Dashboard

A standalone web dashboard for a FoxESS (Foxconn) solar installation.

- **Live power and SoC** from the FoxESS WebSocket — ~5 second resolution
- **Today's figures** from the FoxESS Open API production report — ~300 second resolution
- **Octopus Flux rates and meter readings**, when you have an API key
- **Settings page** for credentials and display preferences, persisted to disk
- **Export and import** the settings, so credentials are never retyped
- **Fits one screen** from 1440×900 down to 320×240, with no scrolling
- **Runs on a Raspberry Pi**, or drives an **ESP32 LCD** as a thin terminal
- **Zero dependencies** — Python standard library only, no `pip install`

## Screenshots

| | |
|---|---|
| ![Rings at 800x480](preview-800x480-rings.png) | ![Rings on a desktop](preview-desktop-rings.png) |
| **Rings** diagram — the default. Inverter in the middle, solar / house / grid / battery around it. | The same diagram on a larger screen. |
| ![Flow at 800x480](preview-800x480-flow.png) | |
| **Flow** diagram — the compact left-to-right alternative. | |
| ![Weather at 800x480](preview-weather-slide.png) | |
| **Weather** slide — the conditions now, the solar forecast for your own roof, and the week ahead. | |

Switch between them in **Settings → Power flow diagram**, or set
`"display": {"diagram": "rings" | "flow"}` in `config.json`.
More at every screen size in [`docs/`](docs/).

The official Open API can only ever be as fresh as the datalogger's upload
cadence, which was measured at **exactly 300 seconds** on a real H1-G2
installation. The WebSocket bypasses that entirely and is the same endpoint the
FoxESS web dashboard uses.

---

## Quick start

### 1. Preview the UI with no credentials

```bash
python3 run.py --demo
# -> http://127.0.0.1:8765
```

### 2. Run it against your installation

```bash
export FOXESS_USER='you@example.com'
export FOXESS_PASS='your-password'
export FOXESS_API_KEY='your-open-api-key'

python3 run.py
```

Then open **http://<pi-ip>:8765/settings** and fill in your credentials — the
settings page is the recommended way to configure it, and it saves to disk so
it survives restarts.

The inverter serial number and power-station id are auto-detected from the
Open API, so you don't need to set them.

> **Prerequisite:** enable **Multi-device login** in the FoxESS web portal —
> Profile → System Settings → Advanced Settings. The WebSocket authenticates
> with a web-session token, not the Open API key.

---

## The settings page

`/settings` — linked from the ⚙ in the dashboard header.

| Section | What you can set |
|---|---|
| **FoxESS account** | email, password, Open API key, region domain, inverter SN, plant id |
| **Data sources** | enable/disable the live feed and the totals, poll interval |
| **Octopus Energy** | API key, account number, tariff — and **Save and test**, which checks them and fills the rest in |
| **Slides** | which slides to show, in what order, and how often |
| **Display** | theme (dark/light/auto), power units (kW/W), clock format, timezone, layout, panel visibility |
| **Detected** | live readout of what the cloud reports — serial, plant, datalogger, firmware, battery units, min SoC, the Octopus meters, and how the daily figures compare |

Changing the credentials re-wires the live connections immediately — no
restart needed. Changing a display setting applies instantly to every
connected screen, including the ESP32, because settings travel with every
snapshot.

**Secrets are never sent back to the browser.** The API returns a mask; leaving
a secret field blank keeps the stored value. The settings file is written with
`0600` permissions. The one exception is the deliberate **Export** button,
which hands over the whole file — see [Keeping your credentials](#keeping-your-credentials).

---

## Two ways to draw the power flow

The dashboard ships with two diagram styles, switched from the settings page
(**Display → Diagram**):

**Flow** — the linear diagram: solar on the left, the inverter in the middle
feeding the house, with the grid and the battery branching off below. The
nodes are rings in the same language as the rings diagram — icon, value and
status inside — so the two themes look like one family.

### The Live panel follows the diagram

On a large screen the Live panel shows **what the diagram does not**, so the
two never say the same thing twice:

| | Flow diagram | Rings diagram |
|---|---|---|
| The panel | a battery **ring** on the left, four stats in a 2×2 grid | four large stats across the full width |
| The battery level | in the panel — the diagram has none | in the diagram — the hub and the ring around the battery node |

The ring is a full circle in the same language as the diagram's, coloured by
the health of the pack, with a tick at the inverter's minimum. On the small
panels both diagrams fall back to the compact number strip.

Both themes give the diagram the full width of the top row, so a linear
diagram gets a panel it can actually fill rather than a stamp in a corner.
The self-sufficiency line appears once, in whichever panel is not already
showing something else.

**Rings** — a hub and its satellites, the way Home Assistant draws it. The
inverter sits in the middle, with solar, house, grid and battery around it.
Each node has an icon, a coloured ring, the live value, and what it is doing
right now. The flow lines are dotted and carry moving dashes in the direction
the power is actually going, and a line only appears when there is power on
it — so an idle system stays calm instead of flickering.

**The battery level is a ring.** The hub and the battery node both show the
state of charge as an arc that fills clockwise from the top, and the colour
tells you how healthy the pack is:

| State | Colour | Meaning |
|---|---|---|
| 40 % and above | green | comfortably charged |
| below 40 % | amber | getting low |
| at or below the inverter's own minimum | red | the grid is taking over |

One helper decides the colour, and everything follows from it: the two ring
arcs, the big percentage in the middle, the battery icon, the SoC gauge, the
SoC bar and the SoC tile. The battery icon fills up to the level as well.

Self-sufficiency moves down into the LIVE panel in this style, because the hub
is busy showing the battery.

| Style | 800×480 |
|---|---|
| Flow | values at 26 px, one row of nodes |
| Rings | values at 21 px, a hub and four rings |

Both styles are checked at every viewport size by `layout_check.py`, so
neither can quietly break the other.

### Which way do the dashes move?

The moving dashes on a wire show which way the power is going. What makes
this fiddly is that the two diagrams draw their wires differently:

| Wire | Linear | Rings |
|---|---|---|
| solar | solar → inverter | solar → hub |
| house | inverter → house | hub → house |
| grid | inverter → grid | grid → hub |
| battery | inverter → battery | hub → battery |

The dashes always travel *along* the path as drawn, so the grid wire has to
be reversed when importing in the linear diagram but when exporting in the
rings one, and the battery wire has to be reversed while discharging in both.

### Which way do the wires bend?

In the rings diagram the four wires do not all curve the same way. **Solar
and house bow away from the hub's horizontal axis**, while **grid and battery
bow towards it**, so the diagram reads as two mirrored pairs rather than four
wires sagging together.

The bend is a property of the path, not of the animation — it does not change
which way the dashes travel, only the shape of the wire they travel along.
`tests/test_diagram.py` measures it out of the markup and checks that the
two pairs stay opposite, that the diagram stays mirror-symmetric about the
hub, and that every wire still lands on its node's ring.

Two checks guard this:

```bash
python3 flow_direction_check.py       # in a real browser: measures the
                                       # rendered animation against the
                                       # power flow, in 6 states
python3 -m unittest discover -s tests  # reads the path geometry out of the
                                       # markup and checks the conditions
```

---

## Slides

The dashboard rotates through a list of views, so one screen can show more
than one thing:

| Slide | What it shows |
|---|---|
| **Power** | the diagram, the live figures and today's totals |
| **Usage** | the half-hourly grid import and export chart, the Octopus rates and today's totals |
| **Weather** | the conditions right now, a seven-day solar forecast for your own roof, and the week ahead |

The **server** owns which slide is showing, so the Pi screen, a phone and
anything else open on it show the same slide at the same moment. The slide
*list* and the *interval* live in the settings; the current slide is kept in
memory, so the settings file is not rewritten every time the display moves.

The ESP32 client ignores the rotation and keeps showing its compact summary.

Set the list, the order and the interval in **Settings → Slides**. `0` turns
the rotation off.

## Weather and the solar forecast

The **Weather** slide shows the conditions right now, a seven-day solar
forecast for your own roof, and the week ahead. The weather comes from
[Open-Meteo](https://open-meteo.com), which needs no API key for personal
use — the credit on the slide is all that is asked for. It is polled every
fifteen minutes, which is as often as a forecast changes much.

Set the place in **Settings → Weather & solar forecast**. Type a town and
press **Find**; the gazetteer only knows towns and villages, so a hamlet
or a street will not be found — use the town nearby. The latitude and
longitude are filled in for you, and **Save and test the forecast** says
what came back *and saves it*, so the slide starts working straight away.

### The units you chose

**Temperature** and **Wind speed** are carried through to the slide. The
forecast is fetched in the unit you asked for, so the numbers *are* in
that unit, and the slide labels them with it:

| You chose | The slide says |
|---|---|
| Celsius | `17°C` |
| Fahrenheit | `63°F` |
| Kilometres per hour | `20 km/h` |
| Metres per second | `5.6 m/s` |
| Miles per hour | `12 mph` |
| Knots | `10 kn` |

The weather service writes its units in its own way — it calls miles per
hour `mp/h` — so the unit cannot be worked out from the reply; it is
taken from the settings and carried the whole way through.

### The array's size, worked out from your own yield

The forecast knows how much sun lands on the roof. It does not know how
much of that your particular array collects — that depends on the panels,
the angle, the dirt, the heat and the inverter. So the dashboard measures
it:

```
system factor  =  the kWh your inverter made  ÷  the kWh per m² of sun that fell
```

The yield comes from the inverter, in one of two ways:

- **The FoxESS production report** — the same daily PV figure the Fox app
  shows. It is the better source, and it is used whenever it is there.
- **The live feed.** The WebSocket carries the solar power, so integrating
  it over the day gives that day's yield. This works *without* the Open
  API, and it is what the fit uses when the report is not available.

The sun comes from the forecast's *global tilted irradiance* for those same
days. The two are compared day for day over the last three weeks, and the
**median** of the daily ratios is the system factor. The median, so that
one odd day does not throw the whole thing out.

That factor is what turns tomorrow's forecast into tomorrow's yield:

```
tomorrow's kWh  =  tomorrow's kWh per m² of sun  ×  the system factor
```

The size of the array in **kWp** follows from it, by dividing by the
performance ratio — the share of the sun the array collects once the
inverter, the cabling, the heat and the dirt have taken their cut:

```
kWp  =  the system factor  ÷  the performance ratio
```

The performance ratio (0.85 by default) is an **assumption**, so the kWp is
an **estimate**; the system factor itself is a **measurement**. Both are shown
in the Detected table on the settings page, along with the days that went
into them, and how today's half-day is comparing with the fit.

> If you know the size of your array, put it in **Array size (kWp)**. It
> is optional — the dashboard works it out for itself — but then the fit
> can be checked against it, and the **performance ratio measured rather
> than assumed**: the factor divided by your real kWp.

> The window reaches back past the turn of the month, so the fit does not
> reset on the first of every month — the month before is fetched once and
> kept.

### The roof

The forecast's sun is for a plane at a given **tilt** and **azimuth**, so
the settings have both. Azimuth 0 is **south**, and Open-Meteo counts
anticlockwise from there, so +90 is west and −90 is east. A tilt of about
35° collects the most in Britain.

### Before it has been fitted

Until there are enough days to compare, the panel shows the sun in kWh/m²
rather than a yield it cannot stand behind, and says so. The Detected
table says how many days have gone into the fit so far.

---

## Octopus Energy

An optional integration for Octopus customers. Two things are read:

- **The tariff rates** — public, no API key needed. For Octopus Flux that
  means the import and export rate for the current half hour, and which band
  you are in.
- **Your meter** — the half-hourly import and export readings, and what they
  cost. This needs an API key.

Flux has three bands, in UK local time: **super cheap 02:00–05:00**, the
standard rate, and **peak 16:00–19:00**. The export rate at peak is several
times the cheap-window rate, so the dashboard says when it is worth
discharging the battery.

The API key goes in **Settings → Octopus Energy**, together with your account
number — the meter point and meter serial are looked up from it, so it is the
only thing you have to find. The key is stored on the machine with `0600`
permissions and never leaves it. **Save and test** checks the credentials
straight away, saves them, and fills the fields in with what it found.

> Most homes measure their export with the inverter, not with a second
> meter register, so there is no export MPAN to find. The Detected table
> on the settings page says whether your account has one.

**When the meter has no readings for today** — Octopus only gets them
when the meter sends them, which can be a day or two behind — the
inverter's own figures are used instead and marked as an estimate. Each
direction is taken from the meter when the meter has something to say,
and from the inverter when it does not.

> The Octopus meter measures the **grid boundary**, so its "consumption" is
> your grid import, not your house usage. The chart on the Usage slide
> shows the grid, in both directions: the import above the line and the
> export below it. The house's own use is in the Today row underneath,
> because it does not move when the battery covers the house — which is
> exactly what you would not want to see on a chart about your tariff.

---

## The Octopus Mini

If you have an **Octopus Mini** — the little device that reads your meter
itself — the dashboard can show **live** figures instead of waiting for
Octopus to collect the half-hourly readings, which can be a day or two
behind.

The Mini's data comes from Octopus's **GraphQL** API. The good news is
that the **API key you already have gets a session token on its own** —
there is nothing extra to enter.

If the API key is not accepted, the account's **email and password** are
tried instead, but that route sometimes wants a CAPTCHA as well, so it
only works for some accounts. The email and password fields are
therefore optional, and the password is stored like the other secrets and
is only ever used to get a token.

The readings themselves come from three GraphQL calls:

| Call | What it is for |
|---|---|
| `obtainKrakenToken` | a session token, from the API key |
| the account's meter | `smartImportElectricityMeter.deviceId` — the smart meter's own id |
| `smartMeterTelemetry` | the readings, grouped ten seconds, one minute, five minutes, half hourly or hourly |

> Octopus retired the old `octocareUsageInfo` query in April 2026 — the
> Octopus Care feature behind it is no longer supported, and it now
> answers "Account is not currently supported."  The device id comes
> from the meter instead, which is how the Home Assistant integration
> does it.

> The Octopus API allows about a hundred calls an hour for the whole
> account, shared with the Octopus app, so the Mini is polled every two
> minutes and the half-hourly readings every five. If the Mini cannot be
> reached the poll backs off, so a broken Mini cannot eat the whole
> allowance.

What it adds to the **Usage** slide:

| | |
|---|---|
| **● 0.42 kW now** | the power the meter is showing right now, in the panel header |
| **Imported / Exported** | today's figures from the meter's own counters, so they are not a day behind |
| **Cost today** | what you have spent today, counted by the meter rather than guessed from the inverter |

The meter's counters are cumulative, so today's energy is the difference
between the counter now and the counter at local midnight. The counters
also cover the **export** direction, which the half-hourly API does not
give you unless your account has a separate export meter point.

Everything here is optional: without the Mini the slide falls back to the
half-hourly readings and the inverter's own daily register, exactly as
before.

---

## The usage chart

The chart on the **Usage** slide is a timeline of the last 24 hours, with
**the grid** in it:

- **import above the line** — what you took from the grid
- **export below the line** — what you put back
- the shaded **cheap** (02:00–05:00) and **peak** (16:00–19:00) windows

It is the grid, because that is what the tariff is about. The house's
own use is in the Today row underneath instead: it does not move when the
battery covers the house, so a chart of it would look the same whether
you were running on solar or on the grid.

The panel says **where the numbers came from** — "from your Octopus
meter" when the Mini's readings cover the chart, and "from the inverter"
otherwise — so there is never any doubt about what you are looking at.

### Checking the numbers

Two figures are printed on every poll and in the settings page:

```
mini: 4.21 kWh imported today, the biggest half hour 2.71 kWh at 02:00
  against the inverter: 4.08 kWh (difference +0.13 kWh)
```

The **biggest half hour** is the largest amount of energy the meter saw
cross the grid boundary in one half hour. If that does not match the
Octopus app, the chart is wrong and the difference is worth chasing.

The second line compares the meter's total for the day with the
inverter's own count of the grid. They measure the same thing by two
different routes, so they should be close; a large difference means one
of them is.

> The inverter and the meter do not count quite the same thing — the
> inverter sees what flows through its current transformers, the meter
> sees the whole grid boundary — so a few per cent is normal, and a
> large gap is not.

Where the two sources overlap, the **meter wins** — the Mini's own
half-hourly readings are what crossed the grid boundary, so they are used
in preference to the inverter's estimate of the same thing.

The scale says what the bars are. The chart is normally half-hourly,
drawn from the half-hourly history the dashboard records itself. Until
that history has filled the whole day — which takes a day of the
dashboard running — the bars come from the inverter's hourly report
instead, and the scale says "kWh per hour" rather than "kWh per half
hour": an hour's energy is twice a half hour's, and drawing it as a
half hour would double it.

---

## Today's figures

The **Today** panel on the Power slide, and the Today row on the Usage
slide, come from the inverter's own **production report** — the same
register the Fox app reads.

Two things are worth knowing about it:

- The report is asked for by **month**, which gives one value per day of
  the month, and by **day**, which gives one value per hour. The month
  report is used for the day's figures, because the daily one is known
  to be a little wrong at the edges of the day.
- It has **two solar figures**: `generation`, which is the inverter's AC
  output — for a hybrid that includes what the battery sent out — and
  `PVEnergyTotal`, which is the PV yield. **The PV yield is the one the
  Fox app shows**, and that is the one the dashboard uses.

The two are cross-checked against each other on every poll, and the
result is printed to the console and shown on the settings page:

```
daily report: pv 0.20, house 1.10, grid_in 0.90, ...
  pv: month 0.20 vs hours 0.20 (difference +0.00)
  pv yield: month 0.20 vs hours 0.20 (difference +0.00)
  pv from the live feed: 0.24 kWh (difference -0.04)
  balance: +0.02 kWh
```

The **balance** is the sun, the battery and the grid coming in, less the
house, the battery and the grid taking out. It should be near zero —
what is left over is the battery's own losses.

> The daily figures are also cross-checked against the half-hourly
> history the dashboard records itself, so a disagreement between the
> inverter's registers and the live feed shows up rather than being
> taken on trust.

---

## Keeping your credentials

Typing an API key into a settings page on a touchscreen is no fun, and
you should not have to do it twice. The settings page has:

| Button | What it does |
|---|---|
| **Copy the API key** | Puts the Octopus API key on the clipboard |
| **Copy the account number** | Puts the Octopus account number on the clipboard |
| **Export** | Downloads the whole settings file, secrets included |
| **Import** | Reads a settings file and fills the form in |

Export asks first, because the file it writes contains your API key and
your FoxESS password in plain text. Import fills the form in but does
not save it — press **Save settings** to keep it.

---

## Display targets

| Target | Needs | See |
|---|---|---|
| **Raspberry Pi + LCD** | Chromium, an LCD | [`display/README.md`](display/README.md) |
| **ESP32 + SPI LCD** | MicroPython, a panel | [`display/README.md`](display/README.md) |

The architecture is the same for both: **the server always runs on the Pi** and
holds the WebSocket session, TLS and credentials. The ESP32 is just a display
terminal that polls `/api/state`, so none of that has to fit in 320 KB of RAM.

```
  ESP32 + LCD  ──► /api/state  ──┐
                                 ├──► dashboard server (the Pi)
  Pi + LCD     ──► /events (SSE) ─┘
```

---

## One screen, no scrolling

**The target is a Raspberry Pi 3 with the official 7" touch display, 800×480.**
The layout has a dedicated breakpoint for exactly that panel, so the type can
be sized for it rather than compromised for a range of unknown screens:

| Size | Layout |
|---|---|
| **800×480 (the target)** | diagram across the full width, live tiles and totals below; body 15 px, values 24 px, diagram text 14/26 px |
| ≥ 1000×600 | flow diagram + gauge, totals and system panels below |
| ~480×320 (3.5" LCD) | numbers only — the diagram is unreadable at this size |
| ~320×240 (2.8" LCD) | one row of five tiles plus the SoC bar |

Verify it against your actual panel:

```bash
python3 layout_check.py          # needs playwright
```

It asserts, at eight viewport sizes, that:

- `scrollHeight <= clientHeight` — no page scrolling
- every value is fully on screen, and **no number is truncated** (a value
  silently ellipsised to `GRID -0…` still "fits", so it needs its own check)
- **type stays legible** — body ≥ 12 px, headings ≥ 10 px, tile values
  ≥ 15 px, and the rendered SVG label/value heights are measured, not assumed

That last check is the important one. The layout adapts by *dropping content*
first — the diagram on small panels, the captions, the optional rows — and only
then by adjusting the grid. It never buys space by shrinking the text into
illegibility.

---

## Running on a Pi 3

The Pi 3 is a 1.2 GHz Cortex-A53 with 1 GB of RAM, so the dashboard is built
to do as little work as possible per update:

- **Live data arrives over Server-Sent Events**, not polling — the fallback
  poll only runs while the event stream is down
- **The DOM is built once.** Tiles and table rows are created on the first
  snapshot; afterwards only cells whose value actually changed are written.
  Measured: **0 `innerHTML` writes** during continuous updates
- **Only active wires animate** — the dash-offset animation is removed from
  any path carrying no power, so an idle system costs nothing

See [`display/README.md`](display/README.md) for the kiosk setup, systemd unit
and Pi-specific Chromium flags.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FOXESS_USER` | – | FoxESS account email — enables the live feed |
| `FOXESS_PASS` | – | FoxESS account password — enables the live feed |
| `FOXESS_API_KEY` | – | Open API key — enables totals and setup |
| `FOXESS_SN` | auto | Inverter serial number |
| `FOXESS_PLANT_ID` | auto | Power station id |
| `FOXESS_DOMAIN` | `https://www.foxesscloud.com` | Use `https://portal.foxesscloud.us` for US accounts |
| `FOXESS_HOST` | `0.0.0.0` | Bind address |
| `FOXESS_PORT` | `8765` | Bind port |
| `FOXESS_API_INTERVAL` | `300` | Seconds between Open API polls |
| `FOXESS_DASHBOARD_CONFIG` | `./config.json` | Where the settings file lives |
| `OCTOPUS_API_KEY` | – | Octopus API key — enables the meter readings |
| `OCTOPUS_ACCOUNT_NUMBER` | – | Octopus account number — the meter is looked up from it |
| `OCTOPUS_TARIFF` | `flux` | `flux` or `agile` |
| `OCTOPUS_EMAIL` | – | Octopus account email — needed for the Mini's live data |
| `OCTOPUS_PASSWORD` | – | Octopus account password — needed for the Mini's live data |
| `FOXESS_DEMO_HOUR` | `13` | Demo mode: which hour of the day to preview |
| `FOXESS_DEBUG` | – | `1` logs every client disconnect instead of swallowing it |

Environment variables only **seed** values that are still empty — anything you
set in the settings page wins from then on.

> Keep the credentials in the settings file or environment variables, never in
> a committed file. `FOXESS_PASS` is your real FoxESS password; the client
> MD5s it locally, the same way Fox's own client does.

> `config.json` is in `.gitignore`, because it holds the password, the Open
> API key and the Octopus API key in plain text. `config.example.json` is the
> same file with the secrets blanked out — copy it to `config.json` to start,
> or just use the settings page.

### Client disconnects are not errors

A browser that reloads the page, reconnects the event stream or loses Wi-Fi
closes its socket with a TCP reset. On a keep-alive connection the server
then reads the *next* request on a socket that no longer exists, and
`socketserver` reports that as a full traceback:

```
Exception occurred during processing of request from ('127.0.0.1', 60401)
...
  File ".../http/server.py", line 442, in handle_one_request
    self.raw_requestline = self.rfile.readline(65537)
ConnectionResetError: [Errno 54] Connection reset by peer
```

That is a closed door, not a broken server, and the dashboard handles it
quietly — the handler exits cleanly and the connection is closed. Nothing is
logged unless `FOXESS_DEBUG=1` is set. Genuine bugs still print a traceback.

---

## Routes

| Route | Purpose |
|---|---|
| `/` | The dashboard |
| `/settings` | The settings page |
| `/api/state` | JSON snapshot of everything on screen |
| `/api/settings` | GET = current settings (secrets masked), POST = save |
| `/api/settings/export` | GET = the full settings, secrets included, for backup |
| `/api/slide` | POST = jump to a named slide |
| `/api/octopus/test` | POST = try the Octopus credentials and say what was found |
| `/api/weather/locate` | POST = look a place up by name |
| `/api/weather/test` | POST = try the weather settings and say what came back |
| `/events` | Server-Sent Events stream of live updates |
| `/healthz` | Liveness probe |

---

## Layout

```
foxess-dashboard/
├── run.py                  # entry point, wiring, demo mode
├── foxess/
│   ├── api.py              # FoxESS Open API client (MD5-signed)
│   ├── ws.py               # WebSocket client + background live feed
│   ├── state.py            # thread-safe shared state
│   ├── config.py           # persistent settings store
│   ├── octopus.py          # Octopus rates, meter readings, rate bands
│   ├── weather.py          # Open-Meteo weather, and the solar calibration
│   ├── history.py          # half-hourly history of the live power
│   └── server.py           # HTTP server + SSE
├── static/
│   ├── index.html          # dashboard (CSS + JS inline)
│   └── settings.html       # settings page
├── display/
│   ├── esp32_client.py     # MicroPython ESP32 + LCD client
│   ├── font5x7.py          # embedded 5x7 font
│   ├── pi_kiosk.py         # Pi kiosk launcher
│   └── README.md           # hardware + deployment guide
├── tests/
│   ├── test_mapping.py     # parsing, signing, state
│   ├── test_config.py      # settings store
│   ├── test_font.py        # renders the font as ASCII art
│   ├── test_esp32_render.py # renders the ESP32 screen offline
│   ├── test_slides_octopus.py # slides, Octopus, history, daily figures
│   ├── test_octopus_mini.py   # the Mini: GraphQL, counters, live figures
│   └── test_settings_page.py  # the settings page and its fields
├── docs/                   # screenshots
├── layout_check.py         # asserts the UI fits every screen size
└── README.md
```

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

306 tests covering unit conversion (W → kW, kW passthrough), charge/discharge
direction, grid import vs export, the device-level timestamp quirk of the v1
real/query endpoint, the Open API signature, secret masking and file
permissions — and the parts that are easy to get wrong: watts against
kilowatt-hours, the day the meter readings are missing, the export meter
point, which of FoxESS's two solar figures is the one the app shows, the
Octopus Mini's GraphQL calls and running counters, and the solar
calibration — the weather codes, the daily irradiation, the day boundary
and the fitting of the array against the inverter's own yield.

The font and the ESP32 renderer are verified visually — `test_font.py` prints
every glyph as ASCII art, and `test_esp32_render.py` renders the ESP32 screen
to a PNG.

---

## Notes on the FoxESS API

Two endpoint families, with subtly different signing rules:

| | Open API | Web API |
|---|---|---|
| Paths | `/op/v1/...` | `/basic/v0/...`, `/dew/v0/...` |
| Auth | long-lived API key | session token from a password login |
| Signature separator | **real CRLF** (`\r\n`) | **literal** `\r\n` (4 characters) |

Mixing them up produces `errno 40256` ("illegal signature"). The client tries
the web-API convention first and falls back, so it works either way.

The login endpoint validates the account *before* the signature, and the
WebSocket handshake needs no signature at all — just the token in the query
string. That is why no `signature.wasm` module is required here, unlike most
Home Assistant integrations for FoxESS.

The **production report** (`/op/v0/device/report/query`) is a third family
of request, and it is the one that knows about days. Two things about it
catch people out:

- `dimension: "month"` gives one value per day of the month and
  `dimension: "day"` gives one value per hour. The month report is the
  one to trust for a day's figures.
- It carries **two solar figures** — `generation` (the inverter's AC
  output, which for a hybrid includes the battery's discharge) and
  `PVEnergyTotal` (the PV yield). The Fox app shows the PV yield, so
  that is the one the dashboard uses. See [Today's figures](#todays-figures).

---

## Migrating to Home Assistant

The pieces map onto HA cleanly:

| Module | HA equivalent |
|---|---|
| `foxess/ws.py` | a `DataUpdateCoordinator` using `async_update` |
| `foxess/state.py` | `CoordinatorData` |
| `foxess/api.py` | the Open API client for cumulative totals |
| `static/index.html` | a Lovelace dashboard card |

The WebSocket client is currently blocking, so for HA it should be ported to
`websockets`/`aiohttp` with the same `parse_message()` logic — that function is
already pure and unit-tested, so it ports without changes.
