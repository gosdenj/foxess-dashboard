"""The phone layout.

A phone is another display medium, so the dashboard has to work on one:
narrow, tall, held in a hand, and allowed to scroll.  These tests check
the rules that make that work, and - just as important - that they are
rules which cannot reach the fixed panels on the wall.

The phone band is narrow *and* tall, so it can never match the Pi's
800x480, the small LCDs or the ESP32's 320x240.  A phone in portrait is
the only thing that matches it.
"""

from __future__ import annotations

import os
import re
import unittest

INDEX = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")


def _read() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read()


def _phone_block() -> str:
    """The text of the phone media query in the stylesheet."""
    page = _read()
    start = page.index("@media (max-width:560px) and (min-height:600px)")
    # to the closing brace of the block
    depth = 0
    for i in range(start, len(page)):
        if page[i] == "{":
            depth += 1
        elif page[i] == "}":
            depth -= 1
            if depth == 0:
                return page[start:i + 1]
    raise AssertionError("the phone media query has no closing brace")


class TestTheBand(unittest.TestCase):

    def test_the_band_is_narrow_and_tall(self):
        """A phone is narrow, and taller than it is wide."""
        page = _read()
        self.assertIn("@media (max-width:560px) and (min-height:600px)", page)

    def test_the_band_cannot_match_the_fixed_panels(self):
        """The Pi is 800x480, the small LCD 480x320, the ESP32 320x240.

        None of them is narrower than 560 px *and* taller than 600 px, so
        the phone rules cannot reach them.
        """
        block = _phone_block()
        self.assertIn("max-width:560px", block)
        self.assertIn("min-height:600px", block)
        for name, (w, h) in (("pi", (800, 480)), ("small-lcd", (480, 320)),
                             ("esp32", (320, 240)), ("narrow", (320, 480))):
            self.assertFalse(w <= 560 and h >= 600,
                             f"the phone band would match the {name} screen")


class TestThePhoneLayout(unittest.TestCase):

    def setUp(self) -> None:
        self.block = _phone_block()

    def test_the_browser_may_scroll_the_slide(self):
        """The slide scrolls; the page itself does not."""
        self.assertIn("overflow-y:auto", self.block)
        self.assertIn(".stage{", self.block)

    def test_the_vertical_pan_is_the_browsers_and_the_swipe_is_ours(self):
        """pan-y gives the browser the vertical drag and keeps the
        horizontal one, so a sideways swipe still turns the deck."""
        self.assertIn("touch-action:pan-y", self.block)

    def test_the_fixed_panels_still_cannot_scroll(self):
        """The kiosk must not be able to scroll the dashboard."""
        page = _read()
        self.assertIn("touch-action:none", page)
        # and the phone's rule is inside the phone band, not on top of it
        self.assertNotIn("body{touch-action:none}", self.block)

    def test_the_diagram_is_not_brought_back(self):
        """At this width the drawing's own type is about eleven pixels."""
        self.assertNotIn(".flow{display:flex", self.block)
        self.assertNotIn(".flow{display:block", self.block)

    def test_the_battery_ring_and_the_live_figures_are_the_hero(self):
        self.assertIn(".gauge", self.block)
        self.assertIn(".side .tiles", self.block)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", self.block)

    def test_the_figures_are_larger_than_the_lcds(self):
        """A phone is read at arm's length."""
        self.assertRegex(self.block, r"--fsb:2\d px|--fsb:2\dpx")

    def test_the_panels_the_tiny_layout_drops_are_brought_back(self):
        """Today's figures, the Octopus figures and the solar forecast."""
        self.assertIn(".totals{display:flex}", self.block)
        self.assertIn(".octopus", self.block)
        self.assertIn(".today .tiles", self.block)
        self.assertIn(".sun{display:flex}", self.block)

    def test_the_panels_are_as_tall_as_they_need(self):
        """A panel that shrinks clips its own last row."""
        self.assertIn(".dash>.panel{flex:none}", self.block)

    def test_the_slide_is_not_a_grid_item(self):
        """The slide has to be as tall as its content.

        A grid hands its items a definite height, so a slide that is a grid
        item is given the height of the stage - and the rest of it is
        clipped by its own ``overflow:hidden``.  Clipped is not the same
        as scrollable: the content is gone, and there is nothing the reader
        can do about it.  So on a phone the stage is a plain block, the
        slide grows with its content, and the stage does the scrolling.
        """
        self.assertIn(".stage{display:block}", self.block)

    def test_the_stage_is_not_left_as_a_grid(self):
        """A grid is what gave the slide a height it could not keep."""
        self.assertNotIn(".stage{display:grid", self.block)

    def test_the_slide_still_covers_the_screen_when_it_is_short(self):
        """A slide with little in it still fills the screen."""
        self.assertIn("min-height:100%", self.block)

    def test_a_new_slide_starts_at_the_top(self):
        """A slide that opens half way down has lost its beginning.

        The reader may have scrolled the last slide down, and the next one
        has to start where it really starts.
        """
        self.assertIn("stage.scrollTop = 0", _read())


class TestTheGesture(unittest.TestCase):

    """The swipe that turns the deck is shared with the kiosk, so it must
    not have been touched to make the phone work."""

    def setUp(self) -> None:
        self.page = _read()

    def test_the_swipe_is_still_measured(self):
        self.assertIn("const SWIPE_DISTANCE = 60;", self.page)
        self.assertIn("const SWIPE_SLOP = 1.4;", self.page)

    def test_the_pointer_is_still_listened_to(self):
        self.assertIn('window.addEventListener("pointerdown", swipeStart', self.page)
        self.assertIn('window.addEventListener("pointermove", swipeMove', self.page)
        self.assertIn('window.addEventListener("pointerup", swipeEnd)', self.page)

    def test_a_cancelled_gesture_is_forgotten(self):
        """A browser that takes the gesture for a scroll cancels the
        pointer, and the swipe must not be left half-read."""
        self.assertIn('window.addEventListener("pointercancel"', self.page)

    def test_the_slide_is_still_asked_for_by_name(self):
        self.assertIn('body: JSON.stringify({step:step})', self.page)


if __name__ == "__main__":
    unittest.main()
