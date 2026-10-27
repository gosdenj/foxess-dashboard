"""Tests for the two power flow diagram styles.

The dashboard can draw the power flow as a linear diagram (flow) or as a hub
with satellites (rings). Both live in ``static/index.html`` and share the same
live data, so a missing element id or a missing setting would leave a panel
blank on the Pi with no error anywhere.
"""

from __future__ import annotations

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import DEFAULTS, EDITABLE  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
SETTINGS = os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestDiagramSetting(unittest.TestCase):

    def test_default_is_rings(self):
        self.assertEqual(DEFAULTS["display"]["diagram"], "rings")

    def test_editable(self):
        self.assertIn("display.diagram", EDITABLE)

    def test_settings_page_offers_both_styles(self):
        page = _read(SETTINGS)
        self.assertIn('id="diagram"', page)
        self.assertIn('value="flow"', page)
        self.assertIn('value="rings"', page)

    def test_settings_page_maps_the_field_to_the_right_path(self):
        page = _read(SETTINGS)
        match = re.search(r"const\s+TEXT_FIELDS\s*=\s*\{(.*?)\}\s*;", page, re.S)
        self.assertIsNotNone(match)
        self.assertIn('diagram:"display.diagram"', match.group(1))


class TestRingsMarkup(unittest.TestCase):

    def setUp(self) -> None:
        self.index = _read(INDEX)

    def test_rings_svg_exists(self):
        self.assertIn('id="ringsSvg"', self.index)

    def test_every_ring_element_the_script_uses_exists(self):
        """The renderer looks these up by id - a typo would blank a node."""
        ids = set(re.findall(r'id="(r[A-Za-z]+)"', self.index))
        used = set(re.findall(r'\$\("(r[A-Za-z]+)"\)', self.index))
        used |= set(re.findall(r'setAttr\(\$\("(r[A-Za-z]+)"\)', self.index))
        missing = sorted(u for u in used if u not in ids)
        self.assertEqual(missing, [], "ring elements used by the script but "
                                      "not present in the markup")

    def test_all_four_satellites_and_the_hub_are_drawn(self):
        for node in ("Pv", "Load", "Grid", "Bat", "Hub"):
            self.assertIn(f'id="rRing{node}"', self.index)
            self.assertIn(f'id="rIco{node}"', self.index)

    def test_flow_lines_exist_for_all_four_paths(self):
        for line in ("Pv", "Load", "Grid", "Bat"):
            self.assertIn(f'id="rLine{line}"', self.index)

    def test_lines_have_a_colour(self):
        """An SVG path with no stroke is invisible - a silent blank diagram."""
        for line in ("Pv", "Load", "Grid", "Bat"):
            match = re.search(rf'<path id="rLine{line}"[^>]*>', self.index)
            self.assertIsNotNone(match)
            self.assertIn("stroke=", match.group(0))

    def test_icons_are_drawn_as_paths_not_emoji(self):
        """Emoji would need a colour emoji font, which a Pi may not have."""
        rings = self.index[self.index.index('id="ringsSvg"'):]
        rings = rings[:rings.index("</svg>")]
        self.assertNotIn("\U0001f31e", rings)
        self.assertNotIn("\U0001f50b", rings)
        self.assertIn("<path", rings)
        self.assertIn("<circle", rings)


class TestBatteryLevelRing(unittest.TestCase):
    """The rings show the state of charge as an arc, coloured by health."""

    def setUp(self) -> None:
        self.index = _read(INDEX)

    def test_both_rings_are_level_gauges(self):
        """A pathLength of 100 lets the dasharray be a percentage."""
        for ring in ("rRingHub", "rRingBat"):
            match = re.search(rf'<circle[^>]*id="{ring}"[^>]*>', self.index, re.S)
            self.assertIsNotNone(match, f"{ring} is missing")
            self.assertIn('pathLength="100"', match.group(0))
            self.assertIn("stroke-dasharray", match.group(0))

    def test_the_gauges_start_at_the_top(self):
        for ring in ("rRingHub", "rRingBat"):
            match = re.search(rf'<circle[^>]*id="{ring}"[^>]*>', self.index, re.S)
            self.assertIn("rotate(-90", match.group(0))

    def test_each_gauge_has_a_track(self):
        """Without a track the empty part of the gauge is invisible."""
        for track in ("rTrackHub", "rTrackBat"):
            self.assertIn(f'id="{track}"', self.index)

    def test_health_colour_helper_exists(self):
        self.assertIn("function battHealth(soc, floor)", self.index)

    def test_health_colours_cover_the_three_states(self):
        for token in ("var(--bat)", "var(--pv)", "var(--alarm)"):
            self.assertIn(token, self.index)

    def test_the_level_reaches_the_icon_and_the_live_panel(self):
        """One source of truth for the state of charge."""
        self.assertIn('setAttr($("rBatFill"), "width"', self.index)
        self.assertIn('$("socFill").style.background = h.colour', self.index)
        self.assertIn('setAttr($("sw4"), "style"', self.index)

    def test_the_summary_line_moves_to_the_live_panel(self):
        """The hub shows the battery now, so self-sufficiency moves down."""
        self.assertIn('body[data-diagram="rings"] #flowHint{display:none}', self.index)
        self.assertIn('body[data-diagram="rings"] .socbar .cap{display:none}', self.index)


class TestLivePanelVariants(unittest.TestCase):
    """The Live panel follows the diagram: it shows what the diagram does not.

    With the rings diagram the battery level is already on screen - in the
    hub and around the battery node - so the panel drops it. With the
    linear flow diagram there is no battery level in the diagram at all, so
    there the panel carries it, as a ring in the same language.
    """

    def setUp(self) -> None:
        self.index = _read(INDEX)

    def test_the_soc_tile_is_marked(self):
        """So the big-screen layout can drop it."""
        self.assertIn('i===4?" soc":""', self.index)

    def test_the_gauge_is_a_full_level_ring(self):
        """The old half-arc is gone - it did not match the diagram."""
        gauge = self.index[self.index.index('class="gauge"'):]
        gauge = gauge[:gauge.index("</div>")]
        self.assertIn('pathLength="100"', gauge)
        self.assertIn('rotate(-90 60 60)', gauge)
        self.assertNotIn("A 80 80", gauge)

    def test_the_floor_marker_sits_on_the_ring(self):
        self.assertIn("60+50*Math.sin(a)", self.index)
        self.assertIn("60-50*Math.cos(a)", self.index)

    def test_the_rings_variant_drops_the_level(self):
        self.assertIn('body[data-diagram="rings"] .side .gauge{display:none}',
                      self.index)
        self.assertIn('.side .tiles .tile.soc{display:none}', self.index)

    def test_the_flow_variant_keeps_the_ring(self):
        self.assertIn('body[data-diagram="flow"] .side .gauge{grid-column:1',
                      self.index)

    def test_both_variants_have_four_stats(self):
        for variant in ("rings", "flow"):
            self.assertIn(f'body[data-diagram="{variant}"] .side .tiles{{',
                          self.index.replace("\n", ""))

    def test_the_value_size_steps_with_the_width(self):
        """The widest value needs three times the font size, and the tiles
        get narrower as the panel does."""
        self.assertIn("--tval:34px", self.index)
        self.assertIn("@media(min-width:1280px) and (min-height:561px){ .side{--tval:40px} }",
                      self.index)
        self.assertIn("@media(min-width:1600px) and (min-height:561px){ .side{--tval:48px} }",
                      self.index)

    def test_the_compact_layouts_are_untouched(self):
        """The big-screen rules must not reach the small panels."""
        self.assertIn("@media(min-width:1001px) and (min-height:561px){", self.index)


class TestFlowDiagram(unittest.TestCase):
    """The linear flow diagram, rebuilt in the same language as the rings."""

    def setUp(self) -> None:
        self.index = _read(INDEX)

    def test_the_nodes_are_rings_with_the_value_inside(self):
        """Not the old style, where the value sat below a small circle."""
        svg = self.index[self.index.index('id="flowSvg"'):]
        svg = svg[:svg.index("</svg>")]
        self.assertIn('class="fnode"', svg)
        self.assertNotIn('class="node"', svg)
        self.assertIn('r="48"', svg)

    def test_every_node_has_an_icon_and_a_status(self):
        for node in ("fNodePv", "fNodeInv", "fNodeLoad", "fNodeGrid", "fNodeBat"):
            self.assertIn(f'id="{node}"', self.index)
        for status in ("fPvSt", "fLoadSt", "fGridSt", "fBatSt"):
            self.assertIn(f'id="{status}"', self.index)

    def test_the_wire_labels_became_node_statuses(self):
        """The value is in the node now, so a wire label would repeat it."""
        self.assertNotIn("wPvLbl", self.index)
        self.assertNotIn("wGridLbl", self.index)
        self.assertIn('$("fGridSt").textContent = imp>0.02 ? "Importing"', self.index)

    def test_the_wires_still_leave_the_inverter(self):
        """The direction logic depends on it: forward means exporting."""
        for wire in ("wPv", "wLoad", "wGrid", "wBat"):
            self.assertIn(f'id="{wire}"', self.index)
        self.assertIn('setWire("wGrid","var(--grid)", (imp+exp)>0.02, imp>exp ? "rev" : "fwd")',
                      self.index)
        self.assertIn('setWire("wBat","var(--bat)", (chg+dis)>0.02, dis>chg ? "rev" : "fwd")',
                      self.index)

    def test_both_themes_get_the_full_width_diagram(self):
        """A linear diagram needs a wide panel or it is a stamp in a corner.

        The slides own the grid now, and the power slide gives the diagram
        the full width of the top row for both themes."""
        self.assertIn('.dash[data-slide="power"]{', self.index)
        self.assertIn('grid-template-areas:"flow flow" "side totals"', self.index)

    def test_the_summary_line_appears_once(self):
        """The flow panel has it in the flow theme, the Live panel in rings."""
        self.assertIn('body[data-diagram="rings"] #flowHint{display:none}', self.index)
        self.assertIn('body[data-diagram="flow"] .side .flowhint.tiny-only{display:none}',
                      self.index)


class TestRingWireBend(unittest.TestCase):
    """The four rings wires must not all bend the same way.

    Solar and house bow away from the hub's horizontal axis, while grid and
    battery bow towards it, so the diagram reads as two mirrored pairs rather
    than four wires sagging together.

    The bend is measured the same way for every wire: the signed distance
    from the control point to the chord, with the chord rotated a quarter
    turn anticlockwise on screen.  All four wires are drawn left to right, so
    a positive bend means "below the chord" and a negative one means "above
    it".
    """

    def setUp(self) -> None:
        self.index = _read(INDEX)
        rings = self.index[self.index.index('id="ringsSvg"'):]
        self.wires = {}
        for match in re.finditer(r'<path id="(rLine\w+)"[^>]*?\sd="([^"]+)"', rings):
            self.wires[match.group(1)] = self._parse(match.group(2))

    @staticmethod
    def _parse(d: str) -> tuple:
        """A quadratic Bezier: the start point, the control point, the end."""
        nums = [float(n) for n in re.findall(r"-?\d+\.?\d*", d)]
        return ((nums[0], nums[1]), (nums[2], nums[3]), (nums[4], nums[5]))

    @staticmethod
    def _bend(wire: tuple) -> float:
        """Signed perpendicular offset of the control point from the chord."""
        (x0, y0), (cx, cy), (x2, y2) = wire
        dx, dy = x2 - x0, y2 - y0
        length = math.hypot(dx, dy)
        # the chord rotated a quarter turn anticlockwise on screen
        nx, ny = -dy / length, dx / length
        return (cx - x0) * nx + (cy - y0) * ny

    def test_every_wire_was_found(self):
        for wire in ("rLinePv", "rLineLoad", "rLineGrid", "rLineBat"):
            self.assertIn(wire, self.wires, f"wire {wire} not found")

    def test_solar_and_house_bend_the_other_way_from_grid_and_battery(self):
        pv = self._bend(self.wires["rLinePv"])
        load = self._bend(self.wires["rLineLoad"])
        grid = self._bend(self.wires["rLineGrid"])
        bat = self._bend(self.wires["rLineBat"])

        self.assertLess(pv * grid, 0, "solar and grid bend the same way")
        self.assertLess(pv * bat, 0, "solar and battery bend the same way")
        self.assertLess(load * grid, 0, "house and grid bend the same way")
        self.assertLess(load * bat, 0, "house and battery bend the same way")

    def test_the_two_pairs_bend_alike(self):
        """Solar matches house, and grid matches battery."""
        pv = self._bend(self.wires["rLinePv"])
        load = self._bend(self.wires["rLineLoad"])
        grid = self._bend(self.wires["rLineGrid"])
        bat = self._bend(self.wires["rLineBat"])
        self.assertGreater(pv * load, 0, "solar and house bend differently")
        self.assertGreater(grid * bat, 0, "grid and battery bend differently")

    def test_the_bend_is_not_flat(self):
        """A straight wire would satisfy the signs but draw as a dead line."""
        for wire, bend in ((name, self._bend(geometry))
                           for name, geometry in self.wires.items()):
            self.assertGreater(abs(bend), 5.0, f"{wire} is nearly straight")

    def test_the_wires_still_join_their_nodes(self):
        """The bend must not move an endpoint off its ring.

        Every wire runs between two nodes - a satellite on one side and the
        hub on the other - so each of its two ends is checked against the
        ring it belongs to.

        The rings are read out of the markup rather than written out here,
        so that the hub can be made bigger without this test going stale -
        it would still catch a wire that no longer reaches the ring.
        """
        rings = self._rings()
        # wire id -> the node at each end, in the order the path is drawn
        ends = {"rLinePv": ("PV", "HUB"), "rLineLoad": ("HUB", "LOAD"),
                "rLineGrid": ("GRID", "HUB"), "rLineBat": ("HUB", "BATT")}
        for wire, (from_node, to_node) in ends.items():
            (x0, y0), _, (x2, y2) = self.wires[wire]
            for point, node in (((x0, y0), from_node), ((x2, y2), to_node)):
                cx, cy, radius = rings[node]
                distance = math.hypot(point[0] - cx, point[1] - cy)
                self.assertAlmostEqual(distance, radius, delta=0.5,
                                       msg=f"{wire} endpoint left the {node} ring")

    def _rings(self) -> dict:
        """Every ring in the diagram: centre and radius, from the markup."""
        names = {"rRingPv": "PV", "rRingLoad": "LOAD", "rRingGrid": "GRID",
                 "rRingBat": "BATT", "rRingHub": "HUB"}
        rings = self.index[self.index.index('id="ringsSvg"'):]
        out = {}
        for match in re.finditer(
                r'<circle[^>]*?\sid="(rRing\w+)"[^>]*?\scx="([\d.]+)"'
                r'\s+cy="([\d.]+)"[^>]*?\sr="([\d.]+)"', rings):
            name = names.get(match.group(1))
            if name:
                out[name] = (float(match.group(2)), float(match.group(3)),
                             float(match.group(4)))
        return out

    def test_the_hub_is_a_bigger_ring_than_the_satellites(self):
        """The inverter is the middle of the diagram, so it says so."""
        rings = self._rings()
        self.assertGreater(rings["HUB"][2], rings["PV"][2])
        for node in ("PV", "LOAD", "GRID", "BATT"):
            self.assertEqual(rings[node][2], rings["PV"][2],
                             f"{node} is a different size from the others")

    def test_the_hubs_level_is_the_biggest_number_on_the_diagram(self):
        """The state of charge is the point of the hub."""
        self.assertIn(".ringsvg .rval.rhub{font-size:31px}", self.index)
        self.assertIn('setAttr($("rHubVal"), "class", "rval rhub "+healthClass)',
                      self.index)

    def test_the_diagram_stays_mirror_symmetric(self):
        """Solar mirrors house, and grid mirrors battery, about x=450."""
        for left, right in (("rLinePv", "rLineLoad"), ("rLineGrid", "rLineBat")):
            left_ctrl = self.wires[left][1]
            right_ctrl = self.wires[right][1]
            self.assertAlmostEqual(900 - left_ctrl[0], right_ctrl[0], delta=0.05,
                                   msg=f"{left} and {right} are not mirrored")
            self.assertAlmostEqual(left_ctrl[1], right_ctrl[1], delta=0.05,
                                   msg=f"{left} and {right} are not mirrored")


class TestDiagramSwitching(unittest.TestCase):

    def setUp(self) -> None:
        self.index = _read(INDEX)

    def test_diagram_attribute_is_set_from_the_settings(self):
        self.assertIn('document.body.dataset.diagram = (d.diagram || "rings")', self.index)

    def test_the_fallback_matches_the_configured_default(self):
        """The front end falls back to the same style the settings default to.

        The settings always carry a diagram, so the fallback is only reached
        before the page has been told - but it must not contradict the
        documented default when it is.
        """
        default = DEFAULTS["display"]["diagram"]
        self.assertEqual(default, "rings")
        self.assertIn(f'(d.diagram || "{default}")', self.index)

    def test_rings_only_apply_to_the_automatic_layout(self):
        """An explicit layout choice from the settings page still wins."""
        block = self.index[self.index.index("rings diagram"):]
        block = block[:block.index("</style>")]
        self.assertIn('body[data-diagram="rings"][data-layout="auto"]', block)

    def test_small_screens_keep_their_own_layout(self):
        """The slide grids must not override the tiny-screen arrangement."""
        self.assertIn("@media(max-width:640px),(max-height:400px)", self.index)
        self.assertIn(".dash[data-slide=\"power\"] .flow{display:none}", self.index)

    def test_flow_lines_hide_when_there_is_no_power(self):
        self.assertIn('el.style.display = active ? "" : "none"', self.index)

    def test_directions_can_reverse(self):
        self.assertIn("ringWire(\"rLineGrid\"", self.index)
        self.assertIn("ringWire(\"rLineBat\"", self.index)
        self.assertIn('el.classList.toggle("rev", reverse)', self.index)


if __name__ == "__main__":
    unittest.main()
