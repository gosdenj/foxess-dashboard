"""HTTP server for the standalone FoxESS dashboard.

Serves a single-page UI and pushes live updates over Server-Sent Events.
Standard library only.

Routes
------
``/``              the dashboard
``/settings``      the settings page
``/api/state``     JSON snapshot of everything on screen
``/api/settings``  GET = current settings (secrets masked), POST = save
``/events``        Server-Sent Events stream of live updates
``/healthz``       liveness probe
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.parse
import inspect
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .config import BUILD, ConfigStore
from .octopus import (OctopusClient, OctopusError, OctopusMini, REST, _all_meters,
                      meter_point_detail, today_start)
from .state import DashboardState
from . import weather

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: Called after settings change so the caller can re-wire live connections.
ApplyCallback = Callable[[dict], None]

#: Errors that mean "the client went away", not "the server is broken".
#: A browser that reloads the page, reconnects the event stream or loses
#: Wi-Fi closes its socket with a TCP reset; that is routine.
CLIENT_GONE = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)


def _debug() -> bool:
    return os.environ.get("FOXESS_DEBUG", "").lower() in ("1", "true", "yes")


#: The Octopus settings that the test button can save.
OCTOPUS_FIELDS = ("api_key", "account_number", "mpan", "meter_serial",
                  "export_mpan", "tariff", "email", "password")


def octopus_changes(given: dict, stored) -> dict[str, str]:
    """The Octopus settings a test has proved are worth keeping.

    A blank or masked field means "keep what is stored", so only what
    really changed is written - the masked key is never saved back.
    """
    changes: dict[str, str] = {}
    for name in OCTOPUS_FIELDS:
        value = str(given.get(name) or "")
        if value in ("", "__KEEP__", "********"):
            value = stored(name)
        if value and value != stored(name):
            changes[f"octopus.{name}"] = value
    return changes


#: The weather settings the test button can save.
WEATHER_FIELDS = ("location", "latitude", "longitude", "temperature_unit",
                  "wind_speed_unit", "tilt", "azimuth", "performance_ratio",
                  "array_kwp")

#: The ones that are numbers, so that a blank one does not wipe a setting.
WEATHER_NUMERIC = ("latitude", "longitude", "tilt", "azimuth",
                   "performance_ratio", "array_kwp")


def weather_changes(given: dict, store) -> dict:
    """The weather settings a test has proved are worth keeping.

    A blank field means "keep what is stored", so a half-filled form
    cannot wipe a working setting.
    """
    changes: dict = {}
    for name in WEATHER_FIELDS:
        value = given.get(name)
        if value is None or value == "":
            continue
        if name in WEATHER_NUMERIC:
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
        else:
            value = str(value)
        current = store.get(f"weather.{name}", "") if store is not None else ""
        try:
            same = float(current) == float(value)
        except (TypeError, ValueError):
            same = current == value
        if not same:
            changes[f"weather.{name}"] = value
    return changes


class DashboardHandler(BaseHTTPRequestHandler):
    state: DashboardState = None            # type: ignore[assignment]
    config = None                           # type: ignore[assignment]
    on_settings_saved: ApplyCallback | None = None
    server_version = "FoxESSDashboard/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        print(f"[http] {self.address_string()} {fmt % args}")

    def handle_one_request(self) -> None:
        """Serve one request, tolerating a client that hangs up.

        The protocol version is HTTP/1.1, so the connection is kept alive and
        the handler loops back to read the next request. If the browser has
        gone away in the meantime - a page reload, an event-stream
        reconnect, a Wi-Fi drop - that read fails with ConnectionResetError
        and socketserver prints a full traceback for what is really just a
        closed door.
        """
        try:
            super().handle_one_request()
        except CLIENT_GONE:
            self.close_connection = True

    # -- helpers ----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")
    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path in ("/settings", "/settings.html"):
            return self._serve_static("settings.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/state":
            return self._json(self.state.snapshot())
        if path == "/api/settings":
            if self.config is None:
                return self._json({"error": "no config store"}, 500)
            return self._json({"settings": self.config.masked(),
                               "path": self.config.path_str(),
                               "build": BUILD})
        if path == "/api/settings/export":
            # The full settings, secrets and all, so they can be copied
            # to another machine without being typed out again.
            if self.config is None:
                return self._json({"error": "no config store"}, 500)
            return self._json({"settings": self.config.all(),
                               "path": self.config.path_str(),
                               "build": BUILD,
                               "note": "this file contains your API key "
                                       "and password"})
        if path == "/events":
            return self._serve_events()
        if path == "/healthz":
            return self._json({"ok": True})
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/slide":
            return self._do_slide()
        if path == "/api/octopus/test":
            return self._do_octopus_test()
        if path == "/api/weather/locate":
            return self._do_weather_locate()
        if path == "/api/weather/test":
            return self._do_weather_test()
        if path != "/api/settings":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if self.config is None:
            self._json({"error": "no config store"}, 500)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "invalid JSON"}, 400)
            return

        updates = payload.get("settings")
        if not isinstance(updates, dict):
            self._json({"error": "expected {'settings': {...}}"}, 400)
            return

        try:
            saved = self.config.save(updates)
        except OSError as exc:
            self._json({"error": f"could not write config: {exc}"}, 500)
            return

        if self.on_settings_saved:
            try:
                self.on_settings_saved(saved)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"saved but apply failed: {exc}",
                            "settings": self.config.masked()}, 500)
                return

        self._json({"ok": True, "settings": self.config.masked()})

    def _do_slide(self) -> None:
        """Jump the rotation to a named slide."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json({"error": "invalid JSON"}, 400)
            return
        name = payload.get("name") or ""
        if not isinstance(name, str):
            self._json({"error": "expected {'name': '...'}"}, 400)
            return
        self.state.set_slide(name)
        self._json({"ok": True, "slide": self.state.slide()})

    def _test_mini(self, email: str, password: str,
                    account_number: str, api_key: str = "") -> str:
        """Try the Octopus Mini and say what came back.

        The Mini is optional - it only needs the account's email and
        password, and it is what gives the live figures.  If it is not
        set up, this says so rather than failing the whole test.
        """
        if not (api_key or (email and password and account_number)):
            return ""
        mini = OctopusMini(email=email, password=password,
                           account_number=account_number, api_key=api_key)
        try:
            device_id = mini.meter_device_id()
        except OctopusError as exc:
            return f"mini: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"mini: {type(exc).__name__}: {exc}"
        try:
            live = mini.live()
        except OctopusError as exc:
            return f"mini {device_id}: {exc}"
        demand = live.get("demand_w")
        message = (f"mini {device_id}: "
                   f"{demand:.0f} W right now"
                   if isinstance(demand, (int, float))
                   else f"mini {device_id}: found")
        # today's figures from the meter's own counters
        try:
            today = mini.today()
        except OctopusError as exc:
            return message + f"\ntoday: {exc}"
        consumption = today.get("consumption_kwh")
        export = today.get("export_kwh")
        cost = today.get("cost")
        readings = today.get("readings", 0)
        if consumption is not None:
            message += (f"\ntoday: {consumption:.2f} kWh imported"
                        + (f", {export:.2f} kWh exported" if export else "")
                        + (f", \u00a3{cost:.2f}" if cost is not None else "")
                        + f" ({readings} readings"
                        + (", part of the day only" if today.get("partial")
                           else "") + ")")
            # The biggest half hour, so the numbers can be checked
            # against the Octopus app.
            peak = today.get("peak_import_kwh")
            when = today.get("peak_import_at")
            if peak:
                message += (f"\nbiggest import: {peak:.2f} kWh"
                            + (f" at {when[11:16]}" if when else ""))
            peak_out = today.get("peak_export_kwh")
            when_out = today.get("peak_export_at")
            if peak_out:
                message += (f"\nbiggest export: {peak_out:.2f} kWh"
                            + (f" at {when_out[11:16]}" if when_out else ""))
        else:
            message += f"\ntoday: no readings came back ({readings})"
        return message

    def _do_weather_locate(self) -> None:
        """Look a place up by name.

        Only towns and villages are in the gazetteer, so the list of
        matches is returned and the settings page lets the user choose.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json({"ok": False, "message": "invalid JSON"}, 400)
            return
        name = str(payload.get("name") or "").strip()
        if not name:
            self._json({"ok": False, "message": "type a place name first"}, 400)
            return
        try:
            places = weather.geocode(name)
        except weather.WeatherError as exc:
            self._json({"ok": False, "message": str(exc)}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, 502)
            return
        if not places:
            self._json({"ok": False,
                        "message": f"nothing called '{name}' was found - try the "
                                   "nearest town"}, 404)
            return
        self._json({"ok": True, "places": places})

    def _do_weather_test(self) -> None:
        """Try the weather settings and say what came back.

        The settings are taken from the request, so the forecast can be
        tried before anything is saved.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json({"ok": False, "message": "invalid JSON"}, 400)
            return
        given = payload.get("settings") or {}

        def field(name: str, default: str = "") -> str:
            value = given.get(name)
            if value is None or value == "":
                stored = self.config.get(f"weather.{name}", default) if self.config else default
                return str(stored if stored is not None else default)
            return str(value)

        try:
            latitude = float(field("latitude"))
            longitude = float(field("longitude"))
        except (TypeError, ValueError):
            self._json({"ok": False,
                        "message": "a latitude and a longitude are needed - press "
                                   "Find to look the place up"}, 400)
            return
        try:
            tilt = int(float(field("tilt", str(weather.DEFAULT_TILT))))
            azimuth = int(float(field("azimuth", str(weather.DEFAULT_AZIMUTH))))
        except (TypeError, ValueError):
            tilt, azimuth = weather.DEFAULT_TILT, weather.DEFAULT_AZIMUTH
        try:
            ratio = float(field("performance_ratio",
                                str(weather.DEFAULT_PERFORMANCE_RATIO)))
        except (TypeError, ValueError):
            ratio = weather.DEFAULT_PERFORMANCE_RATIO

        try:
            raw = weather.fetch(latitude, longitude, tilt=tilt, azimuth=azimuth,
                                temperature=field("temperature_unit", "celsius"),
                                wind=field("wind_speed_unit", "kmh"),
                                past_days=21, forecast_days=7)
        except weather.WeatherError as exc:
            self._json({"ok": False, "message": str(exc)}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, 502)
            return

        parsed = weather.parse(raw, field("location"))
        current = parsed.get("current") or {}
        days = parsed.get("days") or []
        solar = (self.state.weather() or {}).get("solar") or {}

        # What was tested is what is used: the settings are saved, so the
        # slide does not sit there showing yesterday's place.  Only what
        # actually changed is written.
        changes = weather_changes(given, self.config)
        if changes and self.config is not None:
            try:
                saved = self.config.save(changes)
                if self.on_settings_saved:
                    self.on_settings_saved(saved)
            except OSError as exc:
                message = f"the forecast works, but it was not saved: {exc}"
                self._json({"ok": True, "message": message, "weather": parsed,
                            "solar": solar})
                return

        message = (f"{parsed.get('location', {}).get('name') or 'that place'}: "
                   f"{current.get('text', '?')}, "
                   f"{current.get('temperature') if current.get('temperature') is not None else '?'}"
                   f"{parsed.get('units', {}).get('temperature', '°C')}, "
                   f"{len(days)} days ahead")
        if days:
            tomorrow = days[1] if len(days) > 1 else days[0]
            message += (f", tomorrow {tomorrow.get('poa', 0):.2f} kWh/m² of sun")
        if solar.get("calibrated"):
            message += (f". The array is about {solar.get('kwp')} kWp, fitted from "
                        f"{solar.get('days')} days of {solar.get('source')}")
        else:
            message += (". The array has not been fitted yet - "
                        + (solar.get("reason") or
                           "that needs a few days of your own yield"))
        self._json({"ok": True, "message": message, "weather": parsed,
                    "solar": solar})

    def _do_octopus_test(self) -> None:
        """Try the Octopus credentials and say exactly what happened.

        The settings are taken from the request, so it can be tested
        before anything is saved.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json({"ok": False, "message": "invalid JSON"}, 400)
            return
        given = payload.get("settings") or {}

        def field(name: str, stored: str = "") -> str:
            value = str(given.get(name) or "")
            if value in ("", "__KEEP__", "********"):
                return stored
            return value

        store = self.config

        def stored(name: str) -> str:
            return (store.get(f"octopus.{name}", "") or "") if store else ""

        client = OctopusClient(
            api_key=field("api_key", stored("api_key")),
            account_number=field("account_number", stored("account_number")),
            mpan=field("mpan", stored("mpan")),
            meter_serial=field("meter_serial", stored("meter_serial")),
            export_mpan=field("export_mpan", stored("export_mpan")),
            tariff=field("tariff", stored("tariff") or "flux"),
        )

        # What is tested is what is used: save the credentials, so the
        # dashboard does not sit there with nothing to look the meter up
        # with.  Only what actually changed is written.
        changes = octopus_changes(given, stored)

        # The Mini is tested first, because it does not need the API key -
        # it is the one that gives the live figures.
        mini_message = self._test_mini(
            field("email", stored("email")),
            field("password", stored("password")),
            field("account_number", stored("account_number")),
            field("api_key", stored("api_key")))
        if mini_message and mini_message.startswith("mini "):
            if changes and store is not None:
                try:
                    saved = store.save(changes)
                    if self.on_settings_saved:
                        self.on_settings_saved(saved)
                except OSError as exc:
                    mini_message += f"\nnot saved: {exc}"

        try:
            if not client.api_key:
                if mini_message.startswith("mini "):
                    self._json({"ok": True,
                                "message": "the Mini works.\n" + mini_message
                                           + "\nno API key - the rates work "
                                             "without one, but the half-hourly "
                                             "meter readings do not"})
                else:
                    self._json({"ok": False,
                                "message": "no API key - the rates work without one, "
                                           "but the meter readings do not"
                                           + (f"\n{mini_message}" if mini_message else "")})
                return
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False,
                        "message": f"{type(exc).__name__}: {exc}"
                                   + (f"\n{mini_message}" if mini_message else "")})
            return
        try:
            if (not client.mpan or not client.meter_serial
                    or not client.export_mpan):
                if not client.account_number:
                    self._json({"ok": False,
                                "message": "no account number and no meter "
                                           "point - one of them is needed"})
                    return
                try:
                    found = client.lookup()
                except OctopusError as exc:
                    self._json({"ok": False,
                                "message": f"the meter could not be looked up: {exc}"
                                           + (f"\n{mini_message}" if mini_message else "")})
                    return
                client.mpan = found["mpan"]
                client.meter_serial = found["meter_serial"]
                client.export_mpan = client.export_mpan or found["export_mpan"]
                client.export_meter_serial = found["export_meter_serial"]
                # remember it, so the dashboard does not have to look it
                # up again
                for name, value in (("mpan", client.mpan),
                                    ("meter_serial", client.meter_serial),
                                    ("export_mpan", client.export_mpan),
                                    ("export_meter_serial",
                                     client.export_meter_serial)):
                    if value and not stored(name):
                        changes[f"octopus.{name}"] = value
            now = datetime.now(timezone.utc)
            start = today_start(client.timezone_name)
            readings = client.consumption(client.mpan, client.meter_serial,
                                          start, now)
            total = sum(r["kwh"] for r in readings)

            # If that meter has no readings, try every other meter on the
            # meter point over a wider window, so the answer says what is
            # really there rather than just "nothing".
            tried: list[str] = []
            if not readings:
                tried = self._octopus_probe(client, start, now)

            if changes and store is not None:
                try:
                    saved = store.save(changes)
                    if self.on_settings_saved:
                        self.on_settings_saved(saved)
                except OSError as exc:
                    message_extra = f"\nnot saved: {exc}"
                else:
                    message_extra = ""
            else:
                message_extra = ""

            message = (f"found {client.mpan} / {client.meter_serial} - "
                       f"{len(readings)} readings, {total:.2f} kWh")
            message += f"\n(build {BUILD})"
            if mini_message:
                message += f"\n{mini_message}"
            if client.export_mpan:
                message += (f"\nexport {client.export_mpan} / "
                            f"{client.export_meter_serial or client.meter_serial}")
            else:
                message += "\nno export meter found"
            message += message_extra
            message += (f"\nserver time {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                        f" - today is {start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            if tried:
                message += "\n" + "\n".join(tried)
            self._json({
                "ok": bool(readings),
                "message": message,
                # the settings as they now stand, so the page can fill the
                # fields in with what was actually found
                "settings": (store.masked() if store is not None else {}),
                "mpan": client.mpan,
                "meter_serial": client.meter_serial,
                "readings": len(readings),
                "kwh": round(total, 3),
                "probes": tried,
            })
        except OctopusError as exc:
            self._json({"ok": False, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"})

    def _octopus_probe(self, client: OctopusClient, start: datetime,
                       now: datetime) -> list[str]:
        """Try every meter on the meter point, over several windows.

        A dual-register meter has an import meter and an export meter with
        different serial numbers, and the readings may not start at
        midnight - so the answer needs more than one attempt.
        """
        notes: list[str] = []
        try:
            account = client._get(f"{REST}/accounts/{client.account_number}/")
            meters = _all_meters(account)
        except Exception as exc:  # noqa: BLE001
            return [f"could not list the meters: {exc}"]

        # what each meter point says about itself
        for meter in meters:
            detail = meter_point_detail(client, meter["mpan"])
            partner = str(detail.get("import_mpan") or "").strip()
            kind = "export of " + partner if partner else "import"
            notes.append(f"{meter['mpan']}: {kind}"
                         + (f", {detail.get('mpan')}" if detail.get("mpan") else ""))

        windows = [
            ("today", start, now),
            ("7 days", now - timedelta(days=7), now),
            ("30 days", now - timedelta(days=30), now),
        ]
        for meter in meters:
            serial = meter["serial"]
            label = meter["direction"] or "meter"
            for name, first, last in windows:
                # the same call the dashboard makes, so the answer cannot
                # be better than the real thing
                try:
                    rows = client.consumption(meter["mpan"], serial,
                                              first, last)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{meter['mpan']} {serial} ({label}) "
                                 f"{name}: {exc}")
                    continue
                kwh = sum(r["kwh"] for r in rows)
                when = ""
                if rows:
                    first = rows[0].get("start")
                    last = rows[-1].get("start")
                    when = (f", {first} to {last}")
                notes.append(f"{meter['mpan']} {serial} ({label}) {name}: "
                             f"{len(rows)} readings, {kwh:.2f} kWh{when}")
        if not notes:
            notes.append("no other meters on this meter point")
        return notes

    def _serve_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")

    def _serve_events(self) -> None:
        """Server-Sent Events: push a snapshot whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last: str | None = None
        try:
            # A peer that vanishes without a TCP reset (a Wi-Fi drop, a
            # sleeping laptop) would otherwise keep this thread - and the
            # socket - alive for ever. Writes then fail instead of hanging.
            self.connection.settimeout(30)
            while True:
                snapshot = self.state.snapshot()
                payload = json.dumps(snapshot, default=str)
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = payload
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except CLIENT_GONE:
            pass
        except (TimeoutError, OSError):
            pass
        finally:
            self.close_connection = True


class QuietServer(ThreadingHTTPServer):
    """A threading server that stays quiet when a client hangs up.

    ``socketserver`` prints a full traceback for any exception that escapes
    a request handler. Connection resets are routine here - the event
    stream is reconnected whenever the page reloads - so they are reported
    as a one-line note instead, or not at all.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            if _debug():
                print(f"[http] client {client_address[0]} disconnected")
            return
        super().handle_error(request, client_address)


def handler_class(state: DashboardState, config=None,
                  on_settings_saved: ApplyCallback | None = None):
    """The request handler, bound to a state and a config.

    A plain function stored on a class becomes a BOUND method when it is
    reached through an instance, so it would be handed the handler as
    well and complain about the number of arguments.  staticmethod keeps
    it as it was given.  A bound method is left alone - it is already
    bound to what it needs.
    """
    attrs = {"state": state, "config": config}
    if on_settings_saved is not None:
        attrs["on_settings_saved"] = (
            staticmethod(on_settings_saved)
            if inspect.isfunction(on_settings_saved) else on_settings_saved)
    return type("BoundHandler", (DashboardHandler,), attrs)


def serve(state: DashboardState, host: str = "127.0.0.1", port: int = 8765,
          config=None, on_settings_saved: ApplyCallback | None = None) -> None:
    handler = handler_class(state, config, on_settings_saved)
    httpd = QuietServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"\n  FoxESS dashboard -> http://{host}:{port}")
    if config is not None:
        print(f"  settings file     -> {config.path_str()}")
        print(f"  settings page     -> http://{host}:{port}/settings\n")
    else:
        print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.shutdown()
        httpd.server_close()
