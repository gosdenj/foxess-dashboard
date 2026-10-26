"""The animated flow lines must travel the way the power is going.

Whether "forward" along a wire means charging or discharging depends on
which way the path was *drawn* - and the two diagrams draw their wires
differently:

    linear   inverter -> grid      inverter -> battery
    rings    grid     -> hub       hub        -> battery

So the same "reverse" flag means opposite things on different wires, and the
grid wire is drawn the opposite way round in each diagram.

These tests read the geometry straight out of the markup and check that the
JavaScript reverses the animation exactly when the power runs against the
drawn direction. ``flow_direction_check.py`` does the same thing in a real
browser, by measuring the rendered animation.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse_path(d: str) -> tuple:
    """The start and end point of an SVG path.

    Only the commands this dashboard uses, but parsed properly - "H 688"
    carries one coordinate, not two.
    """
    tokens = re.findall(r"[A-Za-z]|-?\d+\.?\d*", d)
    i = 0
    cx = cy = 0.0
    sx = sy = 0.0
    while i < len(tokens):
        cmd = tokens[i]
        i += 1
        if cmd == "M":
            cx, cy = float(tokens[i]), float(tokens[i + 1])
            i += 2
            sx, sy = cx, cy
        elif cmd == "H":
            cx = float(tokens[i])
            i += 1
        elif cmd == "V":
            cy = float(tokens[i])
            i += 1
        elif cmd in ("L", "T"):
            cx, cy = float(tokens[i]), float(tokens[i + 1])
            i += 2
        elif cmd in ("C", "S", "Q"):
            n = 6 if cmd == "C" else 4
            cx, cy = float(tokens[i + n - 2]), float(tokens[i + n - 1])
            i += n
        elif cmd == "Z":
            cx, cy = sx, sy
    return (sx, sy), (cx, cy)


class FlowGeometry:
    """The wire paths and the node centres, parsed out of the markup.

    The two diagrams live in separate SVGs and use slightly different
    markup, so the geometry is read per diagram - otherwise a wire in one
    diagram gets compared against a node in the other.
    """

    #: Every node in both diagrams is a circle with an id; the names are
    #: spelled out below.
    NODE_IDS = {
        "rRingPv": "PV", "rRingLoad": "LOAD", "rRingGrid": "GRID",
        "rRingBat": "BATT", "rRingHub": "HUB",
        "fNodePv": "PV", "fNodeInv": "INV", "fNodeLoad": "LOAD",
        "fNodeGrid": "GRID", "fNodeBat": "BATT",
    }

    def __init__(self, index: str, diagram: str = "flow") -> None:
        self.index = index
        self.diagram = diagram
        self.section = self._section(index, diagram)
        self.paths = self._paths()
        self.nodes = self._nodes()

    @staticmethod
    def _section(index: str, diagram: str) -> str:
        split = index.index('id="ringsSvg"')
        if diagram == "rings":
            return index[split:]
        return index[:split]

    def _paths(self) -> dict:
        out = {}
        for m in re.finditer(
                r'<path id="(\w+)"[^>]*?\sd="([^"]+)"',
                self.section):
            out[m.group(1)] = _parse_path(m.group(2))
        return out

    def _nodes(self) -> dict:
        """Every node circle, found by its id."""
        out = {}
        for m in re.finditer(
                r'<circle[^>]*?\sid="(\w+)"[^>]*?\scx="([\d.]+)"\s+cy="([\d.]+)"',
                self.section):
            name = self.NODE_IDS.get(m.group(1))
            if name:
                out[name] = (float(m.group(2)), float(m.group(3)))
        return out

    def end_point(self, wire: str) -> tuple:
        return self.paths[wire][1]

    def drawn_towards(self, wire: str, node: str) -> bool:
        """Does this wire run towards the given node?

        The node sits at one end of the wire, so comparing along the x axis
        is enough.
        """
        (sx, _), (ex, _) = self.paths[wire]
        nx = self.nodes[node][0]
        return (ex - sx) * (nx - sx) > 0


class TestFlowDirection(unittest.TestCase):

    def setUp(self) -> None:
        self.index = _read(INDEX)
        self.flow = FlowGeometry(self.index, "flow")
        self.rings = FlowGeometry(self.index, "rings")

    # -- the geometry -----------------------------------------------------

    def test_the_nodes_were_found(self):
        for label in ("PV", "INV", "LOAD", "GRID", "BATT"):
            self.assertIn(label, self.flow.nodes, f"node {label} not found")
        for label in ("PV", "LOAD", "GRID", "BATT", "HUB"):
            self.assertIn(label, self.rings.nodes, f"node {label} not found")
        for wire in ("wPv", "wLoad", "wGrid", "wBat"):
            self.assertIn(wire, self.flow.paths, f"wire {wire} not found")
        for wire in ("rLinePv", "rLineLoad", "rLineGrid", "rLineBat"):
            self.assertIn(wire, self.rings.paths, f"wire {wire} not found")

    def test_both_battery_wires_are_drawn_towards_the_battery(self):
        """So a discharge - power leaving the battery - has to run backwards
        on both of them."""
        self.assertTrue(self.flow.drawn_towards("wBat", "BATT"))
        self.assertTrue(self.rings.drawn_towards("rLineBat", "BATT"))

    def test_the_two_grid_wires_are_drawn_in_opposite_directions(self):
        """This is the trap: the linear diagram draws inverter -> grid, the
        rings diagram draws grid -> hub."""
        self.assertTrue(self.flow.drawn_towards("wGrid", "GRID"))
        self.assertFalse(self.rings.drawn_towards("rLineGrid", "GRID"))

    # -- the JavaScript must match ---------------------------------------

    def test_the_battery_wires_reverse_while_discharging(self):
        self.assertIn('setWire("wBat","var(--bat)", (chg+dis)>0.02, dis>chg ? "rev" : "fwd")',
                      self.index)
        self.assertIn('ringWire("rLineBat", (chg+dis)>0.02, dis>chg)', self.index)

    def test_the_battery_wires_do_not_reverse_while_charging(self):
        """Charging runs from the inverter or hub into the battery, which is
        the direction the wire is drawn."""
        self.assertNotIn('dis>chg ? "fwd" : "rev"', self.index)
        self.assertNotIn("ringWire(\"rLineBat\", (chg+dis)>0.02, chg>", self.index)

    def test_the_linear_grid_wire_reverses_while_importing(self):
        """The wire points at the grid, so an import has to run backwards."""
        self.assertIn('setWire("wGrid","var(--grid)", (imp+exp)>0.02, imp>exp ? "rev" : "fwd")',
                      self.index)

    def test_the_rings_grid_wire_reverses_while_exporting(self):
        """The wire points at the hub, so an export has to run backwards."""
        self.assertIn('ringWire("rLineGrid", (imp+exp)>0.02, exp>imp)', self.index)

    def test_solar_and_house_never_reverse(self):
        """Solar always feeds the hub, and the hub always feeds the house."""
        self.assertIn('setWire("wPv","var(--pv)", pv>0.02, "fwd")', self.index)
        self.assertIn('setWire("wLoad","var(--load)", load>0.02, "fwd")', self.index)
        self.assertIn('ringWire("rLinePv", pv>0.02, false)', self.index)
        self.assertIn('ringWire("rLineLoad", load>0.02, false)', self.index)

    def test_the_reversal_flag_means_the_dashes_go_against_the_path(self):
        """Both implementations reverse the animation, and a negative
        stroke-dashoffset moves the dashes forward along the path - so
        "reverse" means against it."""
        self.assertIn('el.style.animationDirection = (dir === "rev") ? "reverse" : "normal"',
                      self.index)
        self.assertIn(".ringsvg .rwire.rev{animation-direction:reverse}", self.index)


if __name__ == "__main__":
    unittest.main()
