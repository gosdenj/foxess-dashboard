"""FoxESS Cloud WebSocket client - live inverter data at ~5 second resolution.

Connects to the undocumented ``/dew/v0/wsmaitian`` endpoint, the same one the
FoxESS web dashboard uses.  This bypasses the 300 second upload cadence of the
Open API entirely.

Standard library only - no pip installs, no wasm runtime.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://www.foxesscloud.com"
WS_HOST = "www.foxesscloud.com"
WS_PATH = "/dew/v0/wsmaitian"
LOGIN_PATH = "/basic/v0/user/login"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ERRNO_SIGNATURE = 40256

#: The web portal's JS signs with literal "\r\n" characters; the Open API
#: signs with a real CRLF.  Neither is documented, so try both.
SIG_STYLES = ("literal", "crlf", "none")


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _post_login(body: bytes, style: str) -> dict:
    ts = str(int(time.time() * 1000))
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "lang": "en",
        "timestamp": ts,
        "token": "",
        "timezone": "Europe/London",
        "platform": "web",
        "dt": f"Europe/London@{ts}@{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
    }
    if style == "literal":
        headers["signature"] = md5(f"{LOGIN_PATH}\\r\\n\\r\\n{ts}")
    elif style == "crlf":
        headers["signature"] = md5(f"{LOGIN_PATH}\r\n\r\n{ts}")

    req = Request(BASE_URL + LOGIN_PATH, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8", "replace") or "{}")
    except URLError as exc:
        return {"errno": -1, "msg": f"network error: {exc.reason}"}


def login(user: str, password: str) -> tuple[str, str]:
    """Log in to the FoxESS web portal.  Returns ``(session_token, style)``.

    The endpoint validates the account before it validates the signature, so
    the MD5 signature can be matched to whichever convention the server wants.
    """
    body = json.dumps({
        "user": user,
        "password": md5(password),
        "type": 1,
        "verification": 1,
    }).encode("utf-8")

    attempts: list[tuple[str, dict]] = []
    for style in SIG_STYLES:
        data = _post_login(body, style)
        attempts.append((style, data))
        if data.get("errno") == 0:
            token = (data.get("result") or {}).get("token")
            if token:
                return token, style

    cred = [d for _, d in attempts if d.get("errno") not in (ERRNO_SIGNATURE, -1, None)]
    sig = [d for _, d in attempts if d.get("errno") == ERRNO_SIGNATURE]
    best = cred[0] if cred else (sig[0] if sig else attempts[-1][1])
    raise FoxESSAuthError(
        f"login failed (tried {', '.join(SIG_STYLES)}): "
        f"errno={best.get('errno')} msg={best.get('msg') or best.get('en')}"
    )


class FoxESSAuthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Minimal RFC 6455 client
# ---------------------------------------------------------------------------

class WebSocket:
    """A blocking, stdlib-only WebSocket client (text frames only)."""

    def __init__(self, host: str, path: str, timeout: int = 30,
                 port: int = 443, use_tls: bool = True):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
        self.rfile = self.sock.makefile("rb")
        self._handshake(host, path)

    def _handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://www.foxesscloud.com\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        status = self.rfile.readline().decode("latin-1").strip()
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key_, _, value = line.decode("latin-1").partition(":")
                headers[key_.strip().lower()] = value.strip()
        if "101" not in status:
            raise ConnectionError(f"websocket handshake failed: {status}")
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("websocket accept key mismatch")

    def recv(self) -> tuple[Any, bytes]:
        """Return ``(opcode, payload)``.  opcode may be ``None`` (EOF)."""
        try:
            header = self.rfile.read(2)
        except (socket.timeout, TimeoutError):
            return "timeout", b""
        if len(header) < 2:
            return None, b""
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if masked else None
        data = self.rfile.read(length) if length else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def send(self, opcode: int, payload: bytes = b"") -> None:
        """Send a client frame (client frames must be masked)."""
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self.send(0x8, b"\x03\xe8")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------

def power_kw(obj: Any) -> float | None:
    """``{'value': '3141', 'unit': 'W'}`` -> ``3.141`` (kW passthrough)."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("value")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if str(obj.get("unit", "W")) == "kW" else value / 1000.0


def parse_message(msg: dict) -> dict[str, float]:
    """Map a FoxESS WebSocket frame to flat variables."""
    node = (msg.get("result") or {}).get("node") or {}
    if not node:
        return {}
    out: dict[str, float] = {}

    bat = node.get("bat") or {}
    if bat.get("soc") is not None:
        try:
            out["SoC"] = float(bat["soc"])
        except (TypeError, ValueError):
            pass

    bat_kw = power_kw(bat.get("power"))
    if bat_kw is not None:
        charging = str(bat.get("charge")) == "1"
        out["batChargePower"] = bat_kw if charging else 0.0
        out["batDischargePower"] = 0.0 if charging else bat_kw

    solar_kw = power_kw((node.get("solar") or {}).get("power"))
    if solar_kw is not None:
        out["pvPower"] = solar_kw

    load_kw = power_kw((node.get("load") or {}).get("power"))
    if load_kw is not None:
        out["loadsPower"] = load_kw

    grid = node.get("grid") or {}
    grid_kw = power_kw(grid.get("power"))
    if grid_kw is not None:
        solar = out.get("pvPower")
        load = out.get("loadsPower")
        if solar is not None and load is not None:
            net = (load + out.get("batChargePower", 0.0)
                   - out.get("batDischargePower", 0.0) - solar)
            predicted = abs(net)
            reliable = (grid_kw < 0.05 or (predicted > 0.05
                         and max(grid_kw / predicted, predicted / grid_kw) < 3.0))
            importing = net > 0 if reliable else str(grid.get("gridStatus", "")) == "3"
        else:
            importing = str(grid.get("gridStatus", "")) == "3"
        if importing:
            out["gridConsumptionPower"] = grid_kw
            out["feedinPower"] = 0.0
        else:
            out["gridConsumptionPower"] = 0.0
            out["feedinPower"] = grid_kw

    for extra in ("auxPower", "backupPower", "epsPower"):
        value = power_kw(node.get(extra))
        if value is not None:
            out[extra] = value

    return out


# ---------------------------------------------------------------------------
# Background live feed
# ---------------------------------------------------------------------------

class FoxESSLive:
    """Runs the WebSocket in a background thread and exposes the latest frame.

    Reconnects automatically: Fox drops the connection roughly every 480
    seconds by design.
    """

    def __init__(self, user: str, password: str, plant_id: str,
                 on_update: Callable[[dict], None] | None = None):
        self.user = user
        self.password = password
        self.plant_id = plant_id
        self.on_update = on_update
        self.data: dict[str, float] = {}
        self.connected = False
        self.last_update: float | None = None
        self.last_error: str | None = None
        self.messages = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="foxess-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        return {
            "data": dict(self.data),
            "connected": self.connected,
            "last_update": self.last_update,
            "last_error": self.last_error,
            "messages": self.messages,
        }

    def _ws_url(self, token: str) -> str:
        return (f"{WS_PATH}?plantId={quote(self.plant_id, safe='')}"
                f"&token={quote(token, safe='')}&platform=web&lang=en")

    def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                token, _style = login(self.user, self.password)
                attempts = 0
                self._consume(token)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                attempts += 1
                if attempts >= 5:
                    self.last_error = f"giving up after {attempts} attempts: {exc}"
                    return
                self._stop.wait(min(5 * attempts, 30))

    def _consume(self, token: str) -> None:
        ws = WebSocket(WS_HOST, self._ws_url(token))
        self.connected = True
        self.last_error = None
        try:
            while not self._stop.is_set():
                opcode, data = ws.recv()
                if opcode is None or opcode == 0x8:
                    break
                if opcode == "timeout":
                    continue
                if opcode == 0x9:
                    ws.send(0xA, data)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                try:
                    parsed = parse_message(json.loads(data.decode("utf-8", "replace")))
                except (json.JSONDecodeError, AttributeError):
                    continue
                if not parsed:
                    continue
                self.data = parsed
                self.last_update = time.time()
                self.messages += 1
                if self.on_update:
                    try:
                        self.on_update(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            self.connected = False
            ws.close()
