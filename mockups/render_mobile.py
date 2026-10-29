"""Render the dashboard at phone sizes, with the mock-up layouts applied.

    python3 mockups/render_mobile.py [--port 8808] [--out docs/mockups]

This is a design tool, not a test.  It loads the real dashboard, injects
the proposal stylesheet from ``mockups/mobile.css`` and photographs each
slide at each candidate, so the options can be judged as pictures
before any of them is written into ``static/index.html``.

Nothing in the shipped code is touched: the stylesheet is added to the
page at render time and thrown away with it.  The one thing it does
change on the server is the slide interval - it stops the rotation so
that the photographs are of the slide that was asked for - and it puts
that back at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = os.path.join(HERE, "mobile.css")

#: The Linux chromium the Pi image carries, used if the local Chrome is
#: not there.
LINUX_CHROME = "/root/.cache/ms-playwright/chromium-1243/chrome-linux/chrome"

#: The phone sizes worth drawing: a modern iPhone, a common Android,
#: and the same phone turned sideways.
VIEWPORTS = [
    ("phone-390x844", 390, 844),
    ("phone-360x800", 360, 800),
    ("phone-landscape-844x390", 844, 390),
]

SLIDES = ["power", "usage", "weather"]

#: The candidates: the first is the real page, the others are the proposals
#: that were drawn before the layout was written into ``static/index.html``.
#: The photographs of the old, pre-phone layout are kept as the record of
#: what a phone used to get; they cannot be re-rendered, because the code
#: that drew them has changed.
CANDIDATES = [
    ("shipped", None, "What a phone gets now - the shipped layout"),
    ("a", "mobile.css", "A - portrait, the numbers are the hero"),
    ("b", "mobile.css", "B - landscape, laid out like the Pi"),
    ("c", "mobile.css", "C - portrait, with the diagram kept"),
]

#: The longest values the display ever shows, so the photographs are of
#: the worst case and not of a quiet afternoon.
WORST_CASE = """() => {
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
}"""

#: What is measured while the picture is taken, so the options can be
#: compared on numbers as well as on looks.
MEASURE = """() => {
  const d = document.documentElement;
  const stage = document.querySelector('.stage');
  const shown = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const px = el => el ? parseFloat(getComputedStyle(el).fontSize) : 0;
  const svgH = sel => { const e = document.querySelector(sel);
                        if (!e) return 0;
                        return e.getBoundingClientRect().height; };
  // only the tiles that are actually on screen: the other slides' tiles
  // are in the page but hidden, and they would measure the wrong thing.
  const tiles = [...document.querySelectorAll('.tile')].filter(shown);
  const valueOf = tiles.length ? tiles[0].querySelector('.v') : null;
  const labelOf = tiles.length ? tiles[0].querySelector('.k') : null;
  const truncated = tiles.filter(t => {
    const v = t.querySelector('.v'), k = t.querySelector('.k');
    return (v && v.scrollWidth > v.clientWidth + 1) ||
           (k && k.scrollWidth > k.clientWidth + 1);
  }).map(t => (t.querySelector('.k') || {}).textContent || '?');
  return {
    vScroll: d.scrollHeight - d.clientHeight,
    stageScroll: stage ? stage.scrollHeight - stage.clientHeight : 0,
    hScroll: d.scrollWidth - d.clientWidth,
    body: px(document.body),
    tileLabel: px(labelOf),
    tileValue: px(valueOf),
    svgLabel: Math.max(svgH('.flowsvg .lbl'), svgH('.ringsvg .rname')),
    svgValue: Math.max(svgH('.flowsvg .val'), svgH('.ringsvg .rval')),
    // the diagram panel, not the svg inside it: the panel is what is
    // hidden on a small screen.
    diagram: shown(document.querySelector('.dash[data-slide="power"] .flow')),
    tiles: tiles.length,
    truncated: truncated,
  };
}"""


def set_settings(port: int, settings: dict) -> None:
    """Push settings to the demo, the way the settings page does."""
    body = json.dumps({"settings": settings}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/settings", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()


def set_slide(port: int, name: str) -> None:
    body = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/slide", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()


def launch(p):
    """The local Chrome if it is there, the Pi's chromium if it is not."""
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch(executable_path=LINUX_CHROME,
                                 args=["--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="8808")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "mockups"))
    ap.add_argument("--only", default="", help="one candidate letter, or 'shipped'")
    args = ap.parse_args()
    port = int(args.port)
    url = f"http://127.0.0.1:{port}/"
    out = args.out
    os.makedirs(out, exist_ok=True)

    css = open(CSS, encoding="utf-8").read() if os.path.exists(CSS) else ""

    # Stop the rotation, so a photograph is of the slide that was asked
    # for rather than of the one that came next.
    set_settings(port, {"display.slide_interval": 0})

    rows = []
    with sync_playwright() as p:
        browser = launch(p)
        for letter, stylesheet, caption in CANDIDATES:
            if args.only and letter != args.only:
                continue
            for size_name, w, h in VIEWPORTS:
                for slide in SLIDES:
                    page = browser.new_page(
                        viewport={"width": w, "height": h}, device_scale_factor=2)
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(url, wait_until="networkidle")
                    set_slide(port, slide)
                    page.reload(wait_until="networkidle")
                    try:
                        page.wait_for_function(
                            "s => document.body.dataset.slide === s",
                            arg=slide, timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(900)
                    page.evaluate(WORST_CASE)
                    page.wait_for_timeout(250)
                    if stylesheet:
                        page.add_style_tag(content=css)
                        page.evaluate(
                            "c => document.body.setAttribute('data-candidate', c)",
                            letter)
                        page.wait_for_timeout(500)
                    m = page.evaluate(MEASURE)
                    name = f"mobile-{letter}-{slide}-{size_name}.png"
                    # The phone scrolls, so the whole slide is worth
                    # photographing: a viewport-only picture would show the
                    # top of it and nothing else.
                    page.screenshot(path=os.path.join(out, name),
                                    full_page=(letter in ("shipped", "a")))
                    rows.append({"candidate": letter, "size": size_name,
                                 "slide": slide, "file": name,
                                 "caption": caption, **m,
                                 "errors": errors})
                    print(f"  {letter:5s} {slide:7s} {size_name:24s} "
                          f"scroll={m['vScroll']:>4}/{m['stageScroll']:>4} "
                          f"body={m['body']:.0f} "
                          f"tile={m['tileValue']:.0f} svg={m['svgValue']:.0f} "
                          f"diagram={'yes' if m['diagram'] else 'no ':3s} "
                          f"tiles={m['tiles']} "
                          f"trunc={','.join(m['truncated']) or '-'}")
                    page.close()
        browser.close()

    # Put the rotation back the way it was.
    set_settings(port, {"display.slide_interval": 20})

    # The measurements of every candidate live in one file.  When only some
    # of them have been re-rendered, the others are kept rather than lost.
    measured = os.path.join(out, "measurements.json")
    rows_all = []
    if os.path.exists(measured):
        try:
            with open(measured, encoding="utf-8") as fh:
                rows_all = json.load(fh)
        except (OSError, ValueError):
            rows_all = []
    drawn = {r["candidate"] for r in rows}
    rows_all = [r for r in rows_all if r["candidate"] not in drawn] + rows
    with open(measured, "w", encoding="utf-8") as fh:
        json.dump(rows_all, fh, indent=2)
    print(f"\n{len(rows)} photographs written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
