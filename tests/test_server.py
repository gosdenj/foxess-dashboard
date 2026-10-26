"""Tests for the HTTP server's handling of clients that hang up.

A browser that reloads the page or reconnects the event stream closes its
socket with a TCP reset. The server must treat that as routine and keep
serving - not print a traceback and not die.
"""

from __future__ import annotations

import contextlib
import io
import os
import socket
import struct
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.server import QuietServer, serve  # noqa: E402
from foxess.state import DashboardState  # noqa: E402


def _reset(sock: socket.socket) -> None:
    """Close a socket the hard way, with a TCP RST rather than a FIN."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))
    sock.close()


class ServerFixture:
    """Runs the real server on a random port for the duration of a test."""

    def __init__(self) -> None:
        self.state = DashboardState()
        self.state.set_meta(plant_name="test", inverter_sn="SN1")
        self.state.set_live({"pvPower": 1.0, "loadsPower": 2.0, "SoC": 50.0})
        self.httpd = QuietServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True, name="test-server")
        self.thread.start()

    def _handler(self):
        from foxess.server import DashboardHandler
        attrs = {"state": self.state, "config": None, "on_settings_saved": None}
        return type("BoundHandler", (DashboardHandler,), attrs)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TestSettingsCallback(unittest.TestCase):
    """The callback that re-wires the connections when settings change.

    It is stored on the handler class, and a plain function stored on a
    class becomes a bound method - so it has to be kept as it was given,
    or it is handed the handler as well and complains about the number
    of arguments.
    """

    def test_a_plain_function_is_called_with_one_argument(self):
        from foxess.server import handler_class
        seen = []
        handler = handler_class(DashboardState(), None,
                                on_settings_saved=lambda saved: seen.append(saved))
        # Through an INSTANCE - that is where a plain function would be
        # turned into a bound method and handed the handler as well.
        instance = object.__new__(handler)
        instance.on_settings_saved({"a": 1})
        self.assertEqual(seen, [{"a": 1}])

    def test_a_bound_method_still_works(self):
        from foxess.server import handler_class

        class Wiring:
            def __init__(self):
                self.saved = None

            def apply_settings(self, saved):
                self.saved = saved

        wiring = Wiring()
        handler = handler_class(DashboardState(), None,
                                on_settings_saved=wiring.apply_settings)
        instance = object.__new__(handler)
        instance.on_settings_saved({"b": 2})
        self.assertEqual(wiring.saved, {"b": 2})

    def test_no_callback_is_fine(self):
        from foxess.server import handler_class
        handler = handler_class(DashboardState(), None)
        self.assertIsNone(handler.on_settings_saved)


class TestClientDisconnect(unittest.TestCase):
    """A client that vanishes must not disturb the server."""

    def setUp(self) -> None:
        self.server = ServerFixture()
        self.addCleanup(self.server.close)

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.server.url + path, timeout=5) as resp:
            return resp.read()

    def test_reset_on_keepalive_does_not_raise(self):
        """The exact case that produced the traceback.

        A request is made, the response is read, and the socket is then
        reset. The server loops back to read the next request on that
        keep-alive connection and used to die with ConnectionResetError.
        """
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        try:
            sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
            data = sock.recv(4096)
            self.assertIn(b"200 OK", data)
            _reset(sock)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

        # Give the handler thread time to notice and hit the reset.
        time.sleep(0.4)

        # The server is still alive and serving.
        self.assertIn(b"live", self._get("/api/state"))

    def test_reset_mid_response_does_not_raise(self):
        """Reset before the response is fully written."""
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        try:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(0.05)
            _reset(sock)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

        time.sleep(0.4)
        self.assertIn(b"live", self._get("/api/state"))

    def test_event_stream_client_disconnects(self):
        """The SSE stream is torn down mid-flight."""
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        try:
            sock.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.recv(1024)
            _reset(sock)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

        time.sleep(1.5)
        self.assertIn(b"live", self._get("/api/state"))

    def test_server_survives_many_disconnects(self):
        """Repeated reconnects - a flapping network - must be survivable."""
        for _ in range(12):
            sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
            try:
                sock.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
                sock.recv(256)
                _reset(sock)
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
        time.sleep(1.0)
        self.assertIn(b"live", self._get("/api/state"))

    def test_healthz_still_answers(self):
        self.assertIn(b"ok", self._get("/healthz"))

    def test_reset_prints_no_traceback(self):
        """The reported symptom: a traceback on the console.

        socketserver prints one for every exception that escapes a request
        handler. A client that resets its socket must not produce one.
        """
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
            try:
                sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
                sock.recv(4096)
                _reset(sock)
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
            time.sleep(0.5)

        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("ConnectionResetError", err.getvalue())


class TestQuietServer(unittest.TestCase):
    def test_handle_error_is_silent_for_client_gone(self):
        """A connection reset must not produce a traceback."""
        server = QuietServer(("127.0.0.1", 0), _make_handler())
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                try:
                    raise ConnectionResetError(54, "Connection reset by peer")
                except ConnectionResetError:
                    import sys as _sys
                    _sys.exc_info()
                    server.handle_error(None, ("127.0.0.1", 12345))
            self.assertEqual(err.getvalue(), "")
        finally:
            server.server_close()

    def test_handle_error_reports_real_bugs(self):
        """A genuine server bug must still be reported."""
        server = QuietServer(("127.0.0.1", 0), _make_handler())
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                try:
                    raise ValueError("something genuinely broken")
                except ValueError:
                    import sys as _sys
                    _sys.exc_info()
                    server.handle_error(None, ("127.0.0.1", 12345))
            self.assertIn("ValueError", err.getvalue())
        finally:
            server.server_close()


def _make_handler():
    from foxess.server import DashboardHandler
    attrs = {"state": DashboardState(), "config": None, "on_settings_saved": None}
    return type("BoundHandler", (DashboardHandler,), attrs)


if __name__ == "__main__":
    unittest.main()
