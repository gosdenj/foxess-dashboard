"""Swiping between the slides.

The dashboard is on a touch screen, so a sideways swipe turns the deck:
left for the next slide, right for the one before.  The gesture is
measured rather than guessed, and the server is the one that knows the
order of the rotation.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.server import QuietServer, handler_class  # noqa: E402
from foxess.state import DashboardState  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class TestStepSlide(unittest.TestCase):
    """Moving a step through the rotation."""

    def setUp(self) -> None:
        self.state = DashboardState()
        self.state.set_slides(["power", "usage", "weather"])

    def test_a_step_forward_moves_to_the_next_slide(self):
        self.state.step_slide(1)
        self.assertEqual(self.state.slide()["name"], "usage")

    def test_a_step_backward_moves_to_the_one_before(self):
        self.state.set_slide("weather")
        self.state.step_slide(-1)
        self.assertEqual(self.state.slide()["name"], "usage")

    def test_the_rotation_wraps_round_at_both_ends(self):
        """The deck turns the same way whichever end it is entered from."""
        self.state.step_slide(-1)
        self.assertEqual(self.state.slide()["name"], "weather")
        self.state.step_slide(1)
        self.assertEqual(self.state.slide()["name"], "power")

    def test_one_slide_is_not_moved(self):
        """There is nowhere to go, so it stays where it is."""
        self.state.set_slides(["power"])
        self.state.step_slide(1)
        self.assertEqual(self.state.slide()["name"], "power")


class SwipeFixture:
    """The real server, on a random port, with a rotation to move."""

    def __init__(self) -> None:
        self.state = DashboardState()
        self.state.set_slides(["power", "usage", "weather"])
        self.moved = []
        handler = handler_class(self.state, None,
                                on_slide_changed=lambda: self.moved.append(1))
        self.httpd = QuietServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True, name="test-server")
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def post(self, payload: dict) -> tuple:
        request = urllib.request.Request(
            f"{self.url}/api/slide", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TestSwipeEndpoint(unittest.TestCase):
    """The endpoint the swipe asks for."""

    def setUp(self) -> None:
        self.server = SwipeFixture()
        self.addCleanup(self.server.close)

    def test_a_step_forward_moves_to_the_next_slide(self):
        status, body = self.server.post({"step": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["slide"]["name"], "usage")

    def test_a_step_backward_moves_to_the_one_before(self):
        self.server.post({"name": "weather"})
        status, body = self.server.post({"step": -1})
        self.assertEqual(status, 200)
        self.assertEqual(body["slide"]["name"], "usage")

    def test_the_rotation_wraps_round(self):
        status, body = self.server.post({"step": -1})
        self.assertEqual(status, 200)
        self.assertEqual(body["slide"]["name"], "weather")

    def test_a_step_of_nowhere_is_refused(self):
        status, body = self.server.post({"step": 0})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = self.server.post({"step": 3})
        self.assertEqual(status, 400)

    def test_a_step_that_is_not_a_number_is_refused(self):
        status, _ = self.server.post({"step": "1"})
        self.assertEqual(status, 400)

    def test_the_rotation_is_told_the_slide_was_moved_by_hand(self):
        """So it gives the slide the user chose a full interval."""
        self.server.post({"step": 1})
        self.assertEqual(len(self.server.moved), 1)
        self.server.post({"name": "power"})
        self.assertEqual(len(self.server.moved), 2)

    def test_a_named_slide_still_works(self):
        status, body = self.server.post({"name": "weather"})
        self.assertEqual(status, 200)
        self.assertEqual(body["slide"]["name"], "weather")


class TestSwipeOnThePage(unittest.TestCase):
    """The gesture itself, in the page."""

    def setUp(self) -> None:
        self.index = INDEX.read_text()

    def test_the_gesture_is_measured(self):
        """Far enough sideways, and more sideways than up and down."""
        self.assertIn("const SWIPE_DISTANCE = 60", self.index)
        self.assertIn("const SWIPE_SLOP = 1.4", self.index)
        self.assertIn("Math.abs(g.dx) >= SWIPE_DISTANCE", self.index)
        self.assertIn("Math.abs(g.dx) > Math.abs(g.dy) * SWIPE_SLOP", self.index)

    def test_the_pointer_is_listened_to(self):
        for event in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
            self.assertIn(f'window.addEventListener("{event}"', self.index)

    def test_left_is_the_next_slide_and_right_the_one_before(self):
        self.assertIn("goSlide(dx < 0 ? 1 : -1)", self.index)

    def test_the_server_is_asked_for_a_step(self):
        self.assertIn("JSON.stringify({step:step})", self.index)

    def test_a_swipe_that_starts_on_a_button_is_left_alone(self):
        """The gear in the corner still opens the settings."""
        self.assertIn('target.closest("a,button,input,select,textarea,label")',
                      self.index)

    def test_the_browser_is_told_to_leave_the_swipe_to_the_page(self):
        self.assertIn("touch-action:none", self.index)
        self.assertIn("if(swipeIsOne(SWIPE) && e.cancelable) e.preventDefault()",
                      self.index)

    def test_the_slide_arrives_from_the_side_it_was_swiped_from(self):
        self.assertIn('const from = (way < 0) ? "-100%" : "100%"', self.index)
        self.assertIn("slideTransition(ghost, SWIPE_WAY)", self.index)

    def test_the_slide_is_shown_straight_away(self):
        """A gesture that takes half a second to answer does not feel like
        it was heard."""
        self.assertIn("applySlide({name: names[", self.index)


if __name__ == "__main__":
    unittest.main()
