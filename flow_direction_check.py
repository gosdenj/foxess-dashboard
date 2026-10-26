"""Check that the animated flow lines point the way the power is going.

    python3 flow_direction_check.py [port]

The dashes on a flow line move along the path when the animation runs
"normal", and against it when it runs "reverse". Whether that matches the
power depends on which way the path was *drawn* - and the two diagrams draw
their wires differently:

    linear   inverter -> grid      inverter -> battery
    rings    grid     -> hub       hub        -> battery

So the same "reverse" flag means opposite things on different wires. This
script works out the direction the power is actually going, works out the
direction the dashes are actually moving, and compares the two.

Nothing here trusts the source code: the path direction comes from the SVG
geometry and the dash direction from the rendered animation.
"""

import sys

from playwright.sync_api import sync_playwright

CHROME = "/root/.cache/ms-playwright/chromium-1243/chrome-linux/chrome"
PORT = sys.argv[1] if len(sys.argv) > 1 else "8770"
URL = f"http://127.0.0.1:{PORT}/"

#: Node centres in SVG user units, per diagram.
NODES = {
    "flow": {
        "pv": (130, 64), "inv": (450, 64),
        "load": (770, 64), "grid": (330, 196), "batt": (570, 196),
    },
    "rings": {
        "solar": (135, 62), "hub": (450, 124),
        "house": (765, 62), "grid": (135, 186), "batt": (765, 186),
    },
}

#: For each wire: the path id, and which nodes it joins.
#: "a" is the node the path is drawn FROM, "b" the node it is drawn TO.
#: The power flow between them depends on the state - see FLOWERS below.
WIRES = {
    "flow": [
        ("wPv", "pv", "inv", "pv"),
        ("wLoad", "inv", "load", "load"),
        ("wGrid", "inv", "grid", "grid"),
        ("wBat", "inv", "batt", "batt"),
    ],
    "rings": [
        ("rLinePv", "solar", "hub", "solar"),
        ("rLineLoad", "hub", "house", "house"),
        ("rLineGrid", "grid", "hub", "grid"),
        ("rLineBat", "hub", "batt", "batt"),
    ],
}

#: For a wire that joins "a" and "b", which one is the source in each state.
#: "mode" is one of: always, grid, batt.
FLOWERS = {
    "pv":    "always",
    "solar": "always",
    "load":  "always",
    "house": "always",
    "grid":  "grid",
    "batt":  "batt",
}


def source_for(kind: str, state: dict) -> str:
    """Which node the power is coming FROM in this state."""
    if kind == "always":
        return None            # the caller uses the drawn direction
    if kind == "grid":
        return "grid" if state["imp"] > state["exp"] else "inv_or_hub"
    if kind == "batt":
        return "batt" if state["dis"] > state["chg"] else "inv_or_hub"
    raise ValueError(kind)


def main() -> int:
    failures = []
    checks = 0

    states = [
        ("midday: solar surplus, charging, exporting",
         {"pv": 5.0, "load": 2.0, "imp": 0.0, "exp": 1.0, "chg": 2.0, "dis": 0.0}),
        ("evening: no solar, discharging, importing",
         {"pv": 0.0, "load": 3.0, "imp": 0.5, "exp": 0.0, "chg": 0.0, "dis": 2.5}),
        ("grid export only, battery idle",
         {"pv": 3.0, "load": 1.0, "imp": 0.0, "exp": 2.0, "chg": 0.0, "dis": 0.0}),
        ("grid import only, battery idle",
         {"pv": 0.0, "load": 2.0, "imp": 1.5, "exp": 0.0, "chg": 0.0, "dis": 0.0}),
        ("battery charging, grid idle",
         {"pv": 4.0, "load": 1.5, "imp": 0.0, "exp": 0.0, "chg": 2.5, "dis": 0.0}),
        ("battery discharging, no grid",
         {"pv": 0.0, "load": 2.0, "imp": 0.0, "exp": 0.0, "chg": 0.0, "dis": 2.0}),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME,
                                    args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(1500)

        for diagram in ("flow", "rings"):
            for title, state in states:
                live = {
                    "pvPower": state["pv"], "loadsPower": state["load"],
                    "gridConsumptionPower": state["imp"], "feedinPower": state["exp"],
                    "batChargePower": state["chg"], "batDischargePower": state["dis"],
                    "SoC": 60,
                }
                # Freeze the live feed first: the server pushes a snapshot
                # every couple of seconds and would overwrite the state we
                # are trying to test.
                page.evaluate("""() => {
                    if(!window.__patched){
                        const orig = window.render;
                        window.render = function(s){
                            if(window.__frozen) return;
                            return orig(s);
                        };
                        window.__patched = true;
                    }
                }""")
                page.evaluate(
                    """([live, diagram]) => {
                        const payload = {live: live, meta:{manager_version:"1.78",
                                          min_soc:10}, totals:{}};
                        window.__frozen = false;
                        document.body.dataset.diagram = diagram;
                        if(diagram === "rings") renderRings(payload);
                        else render(payload);
                        window.__frozen = true;
                    }""", [live, diagram])
                page.wait_for_timeout(120)

                for wire_id, a, b, kind in WIRES[diagram]:
                    info = page.evaluate(
                        """(wire_id) => {
                            const el = document.getElementById(wire_id);
                            if(!el) return null;
                            const len = el.getTotalLength();
                            const s = el.getPointAtLength(0);
                            const e = el.getPointAtLength(len);
                            const cs = getComputedStyle(el);
                            return {
                                sx: s.x, sy: s.y, ex: e.x, ey: e.y,
                                anim: cs.animationDirection,
                                running: cs.animationName,
                                shown: cs.display !== "none",
                                opacity: parseFloat(cs.opacity),
                                dashoffset: parseFloat(cs.strokeDashoffset),
                            };
                        }""", wire_id)
                    if info is None:
                        failures.append(f"{diagram}/{wire_id}: missing")
                        continue
                    if not info["shown"] or info["opacity"] < 0.5:
                        continue        # no power on this wire, nothing to check

                    checks += 1
                    nodes = NODES[diagram]
                    # The direction the dashes travel along the path.
                    if info["anim"] == "reverse":
                        fx, fy = info["ex"], info["ey"]
                        tx, ty = info["sx"], info["sy"]
                    else:
                        fx, fy = info["sx"], info["sy"]
                        tx, ty = info["ex"], info["ey"]

                    # Which way the power is actually going.
                    mode = FLOWERS[kind]
                    if mode == "always":
                        src, dst = a, b
                    elif mode == "grid":
                        if state["imp"] > state["exp"]:
                            src, dst = "grid", ("inv" if diagram == "flow" else "hub")
                        else:
                            src, dst = ("inv" if diagram == "flow" else "hub"), "grid"
                    else:  # batt
                        if state["dis"] > state["chg"]:
                            src, dst = "batt", ("inv" if diagram == "flow" else "hub")
                        else:
                            src, dst = ("inv" if diagram == "flow" else "hub"), "batt"

                    sx, sy = nodes[src]
                    ex, ey = nodes[dst]
                    # Project both onto the path's own axis: positive means the
                    # same way along the wire.
                    px, py = tx - fx, ty - fy
                    plen = (px * px + py * py) ** 0.5 or 1.0
                    ux, uy = px / plen, py / plen
                    dot = ((ex - sx) * ux + (ey - sy) * uy)
                    if dot <= 0:
                        failures.append(
                            f"{diagram}/{wire_id} [{title}] - the dashes travel "
                            f"the wrong way along the wire (power {src}->{dst})")

        browser.close()

    print(f"{checks} animated wires checked across "
          f"{len(states)} states and 2 diagrams")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("every animated wire points the way the power is going")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
