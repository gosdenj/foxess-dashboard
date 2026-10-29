"""Render the dashboard at several viewport sizes and assert it never scrolls.

    python3 layout_check.py [port] [--diagrams flow,rings] [--sizes a,b]

Both diagram styles are checked - the linear flow and the rings hub - because
they lay the page out differently and one can fit where the other does not.

Checks that documentElement.scrollHeight <= clientHeight (no vertical page
scroll) and scrollWidth <= clientWidth (no horizontal page scroll) at each
size, and that the key values are actually on screen.

A phone is the one size that is allowed to scroll, because it is held in a
hand rather than screwed to a wall: there the slide scrolls inside the
stage, the page does not, and the figures have to be legible at arm's
length.
"""

import os
import sys

from playwright.sync_api import sync_playwright

#: The chromium the Pi image carries.  A local Chrome is used instead when
#: there is one, so the check can be run on a laptop as well.
LINUX_CHROME = "/root/.cache/ms-playwright/chromium-1243/chrome-linux/chrome"
#: Where the photographs go.
DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
PORT = sys.argv[1] if len(sys.argv) > 1 else "8770"
DIAGRAMS = ["flow", "rings"]
#: --sizes laptop,pi-lcd-800x480 narrows the sweep, for a quick check.
SIZES_FILTER: list[str] = []
OUT_DIR = DOCS
for _i, _a in enumerate(sys.argv):
    if _a == "--diagrams" and _i + 1 < len(sys.argv):
        DIAGRAMS = sys.argv[_i + 1].split(",")
    if _a == "--sizes" and _i + 1 < len(sys.argv):
        SIZES_FILTER = sys.argv[_i + 1].split(",")
    if _a == "--out" and _i + 1 < len(sys.argv):
        OUT_DIR = sys.argv[_i + 1]
URL = f"http://127.0.0.1:{PORT}/"

#: Every slide has to fit on one screen, so all of them are checked.
SLIDES = ["power", "usage", "weather"]
SIZES = [
    ("wide-1920x1080", 1920, 1080),
    ("desktop", 1440, 900),
    ("laptop", 1280, 720),
    ("tablet-1024x600", 1024, 600),
    ("pi-lcd-800x480", 800, 480),
    ("small-lcd-480x320", 480, 320),
    ("esp32-320x240", 320, 240),
    ("narrow-320x480", 320, 480),
    # A phone, held in a hand: narrow, and taller than it is wide.  It is
    # allowed to scroll, so the checks are different - see "phone" below.
    ("phone-390x844", 390, 844),
    ("phone-360x800", 360, 800),
]

failures = []
os.makedirs(OUT_DIR, exist_ok=True)


def launch(p):
    """The Pi's chromium if it is there, the local Chrome if it is not."""
    if os.path.exists(LINUX_CHROME):
        return p.chromium.launch(executable_path=LINUX_CHROME,
                                 args=["--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage"])
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch()


with sync_playwright() as p:
    browser = launch(p)
    # Stop the rotation while the check runs, or the server moves on to the
    # next slide half way through measuring one.
    page = browser.new_page(viewport={"width": 800, "height": 480})
    page.goto(URL, wait_until="networkidle")
    page.evaluate("""async () => {
        await fetch("/api/settings", {method:"POST",
          headers:{"Content-Type":"application/json"},
          body: JSON.stringify({settings:{"display.slide_interval": 0}})});
    }""")
    # The rotation has to be stopped before the first measurement, or the
    # server moves on to the next slide half way through measuring one.
    # The rotator notices the change within half a second.
    page.wait_for_timeout(1500)

    for diagram in DIAGRAMS:
      for slide in SLIDES:
        for name, w, h in SIZES:
          if SIZES_FILTER and name not in SIZES_FILTER:
            continue
          page = browser.new_page(viewport={"width": w, "height": h},
                                  device_scale_factor=2)
          errors = []
          page.on("pageerror", lambda e: errors.append(str(e)))
          page.goto(URL, wait_until="networkidle")
          page.evaluate("""async (slide) => {
              await fetch("/api/slide", {method:"POST",
                headers:{"Content-Type":"application/json"},
                body: JSON.stringify({name: slide})});
          }""", slide)
          # switch diagram style through the settings API, the way a user would
          page.evaluate("""async (d) => {
              await fetch("/api/settings", {method:"POST",
                headers:{"Content-Type":"application/json"},
                body: JSON.stringify({settings:{"display.diagram": d}})});
          }""", diagram)
          page.reload(wait_until="networkidle")
          # wait for the page to actually be showing the slide we asked
          # for, otherwise the measurement can catch the previous one
          try:
              page.wait_for_function(
                  "s => document.body.dataset.slide === s", arg=slide, timeout=15000)
          except Exception:
              pass
          page.wait_for_timeout(1200)
          # The demo generates random numbers, so a check would only ever
          # catch the layouts that happen to be short.  Measure the worst
          # case instead: the longest values the display ever shows.
          page.evaluate("""() => {
              const WC = {
                live: {pvPower:12.34, loadsPower:12.34,
                       gridConsumptionPower:12.34, feedinPower:12.34,
                       batChargePower:12.34, batDischargePower:12.34, SoC:100},
                octopus: {rates:{band:"peak",
                                 band_until:"2026-10-25T18:00:00Z",
                                 import_p_kwh:99.99, export_p_kwh:99.99,
                                 import_standard_p_kwh:99.99,
                                 export_standard_p_kwh:99.99,
                                 export_premium_p_kwh:99.99, tariff:"flux"},
                          meter:{imported_kwh:9999.99, exported_kwh:9999.99,
                                 import_cost:9999.99, export_earning:9999.99}},
                usage: {today:{pv:9999.99, house:9999.99, batt_out:9999.99,
                               batt_in:9999.99, grid_in:9999.99,
                               grid_out:9999.99, cost:9999.99}},
                weather: {
                  location:{name:"Somewhere"},
                  units:{temperature:"°C", wind:"km/h"},
                  current:{temperature:12.5, apparent:10.0, text:"Overcast",
                           icon:"cloud", wind:22.0, wind_text:"SSW",
                           precipitation:0.0, humidity:88.0, cloud:90.0},
                  days:[
                    {date:"2026-10-25", weekday:"Sat", code:3, text:"Overcast",
                     icon:"cloud", tmax:15.0, tmin:7.0, precip:0.4,
                     precip_prob:100, wind:32.0, wind_text:"SSW",
                     sunrise:"07:12", sunset:"17:54", uv:2.1, poa:1.2, pv:5.0},
                    {date:"2026-10-26", weekday:"Sun", code:0, text:"Clear sky",
                     icon:"sun", tmax:16.0, tmin:7.0, precip:0.0,
                     precip_prob:0, wind:22.0, wind_text:"SW",
                     sunrise:"07:13", sunset:"17:52", uv:2.4, poa:3.3, pv:13.7},
                    {date:"2026-10-27", weekday:"Mon", code:2, text:"Partly cloudy",
                     icon:"partly", tmax:14.0, tmin:6.0, precip:0.0,
                     precip_prob:10, wind:21.0, wind_text:"SW",
                     sunrise:"07:15", sunset:"17:50", uv:2.0, poa:2.9, pv:12.4},
                    {date:"2026-10-28", weekday:"Tue", code:45, text:"Fog",
                     icon:"fog", tmax:11.0, tmin:7.0, precip:0.1,
                     precip_prob:20, wind:10.0, wind_text:"S",
                     sunrise:"07:16", sunset:"17:48", uv:1.8, poa:1.6, pv:7.1},
                    {date:"2026-10-29", weekday:"Wed", code:61, text:"Light rain",
                     icon:"rain", tmax:14.0, tmin:8.0, precip:2.1,
                     precip_prob:90, wind:12.0, wind_text:"SE",
                     sunrise:"07:18", sunset:"17:46", uv:1.5, poa:1.0, pv:4.5},
                    {date:"2026-10-30", weekday:"Thu", code:3, text:"Overcast",
                     icon:"cloud", tmax:14.0, tmin:7.0, precip:0.2,
                     precip_prob:40, wind:9.0, wind_text:"ESE",
                     sunrise:"07:19", sunset:"17:45", uv:1.9, poa:2.5, pv:10.7},
                    {date:"2026-10-31", weekday:"Fri", code:1, text:"Mainly clear",
                     icon:"sun", tmax:11.0, tmin:6.0, precip:0.0,
                     precip_prob:10, wind:10.0, wind_text:"NE",
                     sunrise:"07:21", sunset:"17:43", uv:1.9, poa:2.6, pv:11.1},
                  ],
                  solar:{calibrated:true, kwp:4.94, days:27, factor:4.2,
                         tomorrow:13.7, today:5.0,
                         note:"", performance_ratio:0.85},
                  attribution:"Weather by Open-Meteo.com",
                },
              };
              const orig = window.render;
              window.render = (s2) => {
                const m = Object.assign({}, s2, WC);
                m.live = Object.assign({}, s2.live, WC.live);
                m.usage = Object.assign({}, s2.usage, WC.usage);
                m.weather = Object.assign({}, s2.weather, WC.weather);
                orig(m);
              };
              const s0 = window.SNAP || {};
              const m0 = Object.assign({}, s0, WC);
              m0.live = Object.assign({}, s0.live, WC.live);
              m0.usage = Object.assign({}, s0.usage, WC.usage);
              m0.weather = Object.assign({}, s0.weather, WC.weather);
              orig(m0);
          }""")
          page.wait_for_timeout(250)

          m = page.evaluate("""() => {
              const d = document.documentElement;
              const b = document.body;
              const vis = sel => {
                  const el = document.querySelector(sel);
                  if (!el) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 &&
                         r.top < innerHeight && r.bottom > 0 &&
                         r.left < innerWidth && r.right > 0;
              };
              const onScreen = sel => {
                  const el = document.querySelector(sel);
                  if (!el) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 &&
                         r.bottom <= innerHeight + 1 && r.right <= innerWidth + 1 && r.top >= -1;
              };
              const tiles = [...document.querySelectorAll('.tile')].map(e => {
                  const r = e.getBoundingClientRect();
                  const v = e.querySelector('.v');
                  const k = e.querySelector('.k');
                  const truncated = v ? v.scrollWidth > v.clientWidth + 1 : false;
                  const labelCut = k ? k.scrollWidth > k.clientWidth + 1 : false;
                  return {k: e.querySelector('.k').textContent.trim(),
                          // below the fold is fine on a phone, which scrolls;
                          // cut off at the side is not fine anywhere
                          cut: r.bottom > innerHeight + 1 || r.right > innerWidth + 1,
                          cutSide: r.right > innerWidth + 1 || r.left < -1,
                          truncated, labelCut};
              });
              // Rendered type sizes, so "does it fit" cannot be achieved by
              // shrinking the text into illegibility.
              const px = sel => { const e = document.querySelector(sel);
                  return e ? parseFloat(getComputedStyle(e).fontSize) : 0; };
              const svgH = sel => { const e = document.querySelector(sel);
                  if (!e) return 0;
                  const r = e.getBoundingClientRect();
                  return r.height; };
              return {
                  vScroll: d.scrollHeight - d.clientHeight,
                  hScroll: d.scrollWidth - d.clientWidth,
                  bodyOverflow: getComputedStyle(b).overflow,
                  // the phone scrolls: the stage is the thing that moves
                  stageScroll: (() => {
                      const s = document.querySelector('.stage');
                      if (!s) return 0;
                      return s.scrollHeight - s.clientHeight;
                  })(),
                  stageOverflow: (() => {
                      const s = document.querySelector('.stage');
                      return s ? getComputedStyle(s).overflowY : '';
                  })(),
                  touchAction: getComputedStyle(b).touchAction,
                  flow: vis(".flowsvg"),
                  // the battery level may be in the panel or in the diagram
                  // the battery level, wherever it is shown
                  soc: (vis("#socArc") || vis("#socBar")
                        || vis("#rRingHub") || vis("#rRingBat")
                        || vis(".side .tile.soc")),
                  tiles: tiles,
                  allTilesOnScreen: tiles.every(t => !t.cut),
                  allTilesOnScreenSide: tiles.every(t => !t.cutSide),
                  anyTruncated: tiles.some(t => t.truncated || t.labelCut),
                  type: {
                      body: px("body"),
                      heading: px(".panel h2"),
                      tileLabel: px(".tile .k"),
                      tileValue: px(".tile .v"),
                      svgLabel: Math.max(svgH(".flowsvg .lbl"), svgH(".ringsvg .rname")),
                      svgValue: Math.max(svgH(".flowsvg .val"), svgH(".ringsvg .rval")),
                      // the weather slide has its own figures
                      weatherValue: px(".nowtemp .big"),
                      weatherDay: px(".wday .t"),
                      weatherKv: px(".kv .v"),
                  },
                  // Content clipped inside a panel: overflow:hidden hides it
                  // silently, with no page scroll to give it away.
                  clippedPanels: [...document.querySelectorAll(".panel")]
                      .filter(p => p.scrollHeight > p.clientHeight + 1)
                      .map(p => p.className.replace(/\s+/g, ".")),
                  // The same, one level down - a day card can be cut off
                  // inside a panel that itself fits.
                  clippedCards: [...document.querySelectorAll(".wday, .kv, .sunrow")]
                      .filter(p => p.scrollHeight > p.clientHeight + 1)
                      .map(p => p.className.replace(/\s+/g, ".")),
                  // The text inside each ring has to fit inside the circle -
                  // measured against the chord at the text's own height.
                  ringText: (() => {
                      const out = [];
                      for(const [svgId, pairs] of [["ringsSvg",
                          [["rRingPv","rPvVal"],["rRingPv","rPvSt"],
                           ["rRingLoad","rLoadVal"],["rRingLoad","rLoadSt"],
                           ["rRingGrid","rGridVal"],["rRingGrid","rGridSt"],
                           ["rRingBat","rBatVal"],["rRingBat","rBatSt"],
                           ["rRingHub","rHubVal"],["rRingHub","rHubSt"]]],
                         ["flowSvg",
                          [["fNodePv","vPv"],["fNodePv","fPvSt"],
                           ["fNodeLoad","vLoad"],["fNodeLoad","fLoadSt"],
                           ["fNodeGrid","vGrid"],["fNodeGrid","fGridSt"],
                           ["fNodeBat","vBat"],["fNodeBat","fBatSt"],
                           ["fNodeInv","vModel"]]]]){
                      const svg = document.getElementById(svgId);
                      if(!svg || getComputedStyle(svg).display === 'none') continue;
                      const box = svg.getBoundingClientRect();
                      const vb = svg.viewBox.baseVal;
                      // preserveAspectRatio="meet" scales and centres the
                      // content, so the screen -> user-unit mapping is the
                      // real CTM, not a simple ratio.
                      const ctm = svg.getScreenCTM().inverse();
                      const toUser = (bb) => {
                          const x0 = ctm.a*bb.left + ctm.c*bb.top + ctm.e;
                          const y0 = ctm.b*bb.left + ctm.d*bb.top + ctm.f;
                          const x1 = ctm.a*bb.right + ctm.c*bb.bottom + ctm.e;
                          const y1 = ctm.b*bb.right + ctm.d*bb.bottom + ctm.f;
                          return {x0: Math.min(x0,x1), x1: Math.max(x0,x1),
                                  y0: Math.min(y0,y1), y1: Math.max(y0,y1)};
                      };
                      const out = [];
                      for(const [rid,tid] of pairs){
                          const c = document.getElementById(rid);
                          if(!c) continue;
                          const cx = +c.getAttribute('cx'), cy = +c.getAttribute('cy');
                          const r = +c.getAttribute('r');
                          {
                              const t = document.getElementById(tid);
                              if(!t) continue;
                              const range = document.createRange();
                              range.selectNodeContents(t);
                              const bb = range.getBoundingClientRect();
                              const u = toUser(bb);
                              const x0 = u.x0, x1 = u.x1, y0 = u.y0, y1 = u.y1;
                              const mid = (y0+y1)/2;
                              const half = Math.sqrt(Math.max(0, r*r-(mid-cy)*(mid-cy)));
                              const avail = half*2, need = x1-x0;
                              if(need > avail - 4 || y0 < cy-r || y1 > cy+r)
                                  out.push(tid + " " + t.textContent +
                                           " needs " + need.toFixed(0) +
                                           " of " + avail.toFixed(0));
                          }
                      }
                      }
                      return out;
                  })(),
                  pv: document.querySelector("#vPv").textContent,
                  load: document.querySelector("#vLoad").textContent,
                  grid: document.querySelector("#vGrid").textContent,
                  bat: document.querySelector("#vBat").textContent,
                  socTxt: document.querySelector("#socText").textContent,
                  clock: document.querySelector("#clockPill").textContent,
              };
          }""")

          suffix = f"-{diagram}" if diagram != "flow" else ""
          page.screenshot(path=os.path.join(OUT_DIR, f"layout-{name}{suffix}.png"))

          vscroll, hscroll = m["vScroll"], m["hScroll"]
          t = m["type"]
          # Legibility floors - a physical LCD has to be readable at a glance.
          tiny = name.startswith(("esp32", "small", "narrow"))
          # A phone is read closer than a panel on a wall, so its floors are
          # higher, not lower.
          phone = name.startswith("phone")
          floors = {
              "body": 14.0 if phone else 12.0,
              "heading": 11.0 if phone else 10.0,
              "tileLabel": 11.0 if phone else 9.5,
              "tileValue": 20.0 if phone else 15.0,
              "svgLabel": 8.0,
              "svgValue": 14.0,
              "weatherValue": 22.0 if phone else 20.0,
              "weatherDay": 12.0 if phone else 11.0,
              "weatherKv": 11.0 if phone else 10.0,
          }
          tooSmall = {k: (round(v, 1), floors[k]) for k, v in t.items() if v < floors[k]}
          # the tiny layout drops the diagram entirely, so SVG floors don't apply
          if tiny or phone:
              tooSmall = {k: v for k, v in tooSmall.items()
                          if not k.startswith("svg")}
          # a slide with no diagram (the energy one) has no SVG type to check
          if not m["flow"]:
              tooSmall = {k: v for k, v in tooSmall.items()
                          if not k.startswith("svg")}
          # the weather slide has no tiles - it has its own figures
          if slide == "weather":
              tooSmall = {k: v for k, v in tooSmall.items()
                          if not k.startswith("tile")}

          # The power slide is the one with the live tiles and the battery
          # gauge. The other slides have their own panels.
          power = (slide == "power")
          # A phone scrolls, so a tile below the fold is where it should be:
          # it must not be cut off at the side, and the stage must be the
          # thing that scrolls rather than the page.
          tilesOk = m["allTilesOnScreenSide"] if phone else m["allTilesOnScreen"]
          scrollsOk = True
          if phone:
            scrollsOk = (m["stageOverflow"] == "auto"
                         and m["touchAction"] == "pan-y"
                         and m["stageScroll"] >= 0)
          ok = (vscroll <= 0 and hscroll <= 0
                and (m["soc"] or not power)
                # four on a big screen (the SoC tile is folded into the
                # ring), five on a small one
                and (len(m["tiles"]) >= 4 or not power)
                and tilesOk
                and scrollsOk
                and not m["anyTruncated"] and not tooSmall
                and not m["clippedPanels"] and not m["clippedCards"]
                and not m["ringText"])
          status = "OK " if ok else "FAIL"
          print(f"  {status} {diagram + '/' + slide + '/' + name:<32} {w}x{h}  vScroll={vscroll} hScroll={hscroll} "
                f"trunc={m['anyTruncated']}  "
                f"type[body {t['body']:.0f} h2 {t['heading']:.0f} "
                f"lbl {t['tileLabel']:.0f} val {t['tileValue']:.0f} "
                f"svg {t['svgLabel']:.0f}/{t['svgValue']:.0f}]"
                + (f"  CLIPPED:{','.join(m['clippedPanels'])}" if m["clippedPanels"] else "")
                + (f"  CARD:{','.join(m['clippedCards'])}" if m["clippedCards"] else ""))
          if m["clippedPanels"]:
              print(f"       clipped inside: {', '.join(m['clippedPanels'])}")
          if m["clippedCards"]:
              print(f"       clipped card: {', '.join(m['clippedCards'])}")
          if m["ringText"]:
              print(f"       text outside the ring: {', '.join(m['ringText'])}")
          if not ok:
              failures.append(f"{diagram}/{slide}/{name}")
              if tooSmall:
                  print(f"       TOO SMALL: {tooSmall}")
              if not m["allTilesOnScreen"]:
                  print("       detail: tiles off screen")
              if m["anyTruncated"]:
                  print("       detail: values truncated")
              for tt in m["tiles"]:
                  if tt["truncated"] or tt["labelCut"]:
                      print(f"       truncated: {tt['k']}")
          if errors:
              failures.append(f"{name}: js errors {errors}")
              print(f"       JS ERRORS: {errors}")
          page.close()
    browser.close()

print()
if failures:
    print("FAILURES:", failures)
    raise SystemExit(1)
print("all viewports fit on one screen with no page scrolling")
