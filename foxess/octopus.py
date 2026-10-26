"""Octopus Energy client.

Reads half-hourly meter data and tariff rates from Octopus. Two surfaces are
used:

* the REST API for consumption, tariffs and rates - simple and stable
* the GraphQL (Kraken) API to look up the meter point and meter serial from
  an account number, so the user only has to paste one thing

Authentication is HTTP Basic with the API key as the username and an empty
password. The tariff rates and the product catalogue are public and need no
key at all.

Standard library only, like the rest of the project.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

REST = "https://api.octopus.energy/v1"
GRAPHQL = "https://api.octopus.energy/v1/graphql/"

#: Flux is a combined import + export tariff with three fixed bands, in UK
#: local time. Octopus is explicit that the cheap band stays 02:00-05:00 local
#: even when the in-home display disagrees, so the band is worked out here
#: rather than read off the meter.
FLUX_BANDS = {
    "cheap": (2, 5),    # 02:00 - 05:00, super cheap
    "peak": (16, 19),   # 16:00 - 19:00, peak
}

#: The Flux product codes. The tariff code varies by region, so it is
#: looked up in the product catalogue at runtime and these are only a
#: fallback.
FLUX_PRODUCTS = {
    "import": ("FLUX-IMPORT-23-02-14", "E-1R-FLUX-IMPORT-23-02-14-A"),
    "export": ("FLUX-EXPORT-23-02-14", "E-1R-FLUX-EXPORT-23-02-14-A"),
}

#: Agile and Tracker, for anyone who moves tariff.
AGILE_PRODUCTS = {
    "import": ("AGILE-24-10-01", "E-1R-AGILE-24-10-01-A"),
    "export": ("AGILE-OUTGOING-19-05-13", "E-2R-AGILE-OUTGOING-19-05-13-A"),
}

TARIFFS = {
    "flux": FLUX_PRODUCTS,
    "agile": AGILE_PRODUCTS,
}


class OctopusError(Exception):
    """Any problem talking to Octopus."""


def _now(tz: str = "Europe/London") -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 - no tz database on the box
        return datetime.now(timezone.utc)


def today_start(timezone_name: str = "Europe/London") -> datetime:
    """Midnight this morning, local time, as UTC.

    "Today" means the local day - the meter settles on the UK clock, not
    on UTC, so the two are an hour apart for half the year.
    """
    if ZoneInfo is not None:
        try:
            local = datetime.now(ZoneInfo(timezone_name))
        except Exception:  # noqa: BLE001 - no timezone database
            local = datetime.now(timezone.utc)
    else:
        local = datetime.now(timezone.utc)
    return local.replace(hour=0, minute=0, second=0,
                         microsecond=0).astimezone(timezone.utc)


def _items(value: Any) -> list:
    """A field may come back as one thing or as a list of them.

    Octopus's GraphQL is not consistent about this - a meter point is
    one thing in the schema, but a list in the reply - so everything is
    walked as though it might be either.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _shape(value: Any, depth: int = 0) -> str:
    """What came back, described by shape rather than by value.

    This goes in the error message when the reply cannot be understood,
    so the next attempt shows what the API actually sent without
    spilling the account's details.
    """
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [f"{k}: {_shape(v, depth + 1)}"
                 for k, v in list(value.items())[:6]]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        more = f", ...{len(value)} items" if len(value) > 1 else ""
        return "[" + _shape(value[0], depth + 1) + more + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


class OctopusMini:
    """The live data from an Octopus Mini.

    The Mini reads the meter itself, so its numbers are live rather than
    a day or two behind like the half-hourly readings.  They only come
    from the GraphQL API, and the GraphQL API wants the account's email
    and password rather than the API key.

    Octopus allows about a hundred calls an hour for the whole account,
    so this is meant to be polled no more than once a minute or two.
    """

    def __init__(self, email: str = "", password: str = "",
                 account_number: str = "", api_key: str = "",
                 timezone_name: str = "Europe/London") -> None:
        self.email = (email or "").strip()
        self.password = password or ""
        self.account_number = (account_number or "").strip()
        self.api_key = (api_key or "").strip()
        self.timezone_name = timezone_name or "Europe/London"
        self._kraken = ""
        self.last_error = ""
        self._token = ""
        self._device_id = ""
        self._device_ids: list[str] = []

    @property
    def configured(self) -> bool:
        """The Mini can be reached with the API key, or with a login."""
        return bool(self.account_number
                    and (self.api_key or (self.email and self.password)))

    # -- GraphQL ---------------------------------------------------------

    def _call(self, query: str, variables: dict | None = None,
              token: str | None = None) -> dict:
        body = json.dumps({"query": query,
                           "variables": variables or {}}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        request = urllib.request.Request(GRAPHQL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OctopusError(f"GraphQL HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OctopusError(f"could not reach Octopus: {exc.reason}") from exc
        if payload.get("errors"):
            message = "; ".join(e.get("message", "") for e in payload["errors"])
            # Octopus puts the useful part in the extensions, so say that
            # too - "Invalid data." on its own tells you nothing
            details = []
            for error in payload["errors"]:
                for extra in (error.get("extensions") or {}).get("validationErrors") or []:
                    text = extra.get("message")
                    if text and text not in details:
                        details.append(text)
            raise OctopusError("; ".join(details) or message or "GraphQL error")
        return payload.get("data") or {}

    def token(self, force: bool = False) -> str:
        """The Kraken token, which the GraphQL API wants.

        The API key can get one on its own, which is by far the easiest
        way in.  The email and password are a fallback - that route wants
        a CAPTCHA as well, so it only works for some accounts.
        """
        if self._token and not force:
            return self._token
        problems = []
        if self.api_key:
            token = self._token_with("api key", {"APIKey": self.api_key})
            if token:
                self._token = token
                return token
            problems.append(self.last_error)
        if self.email and self.password:
            token = self._token_with("email and password",
                                     {"email": self.email,
                                      "password": self.password})
            if token:
                self._token = token
                return token
            problems.append(self.last_error)
        raise OctopusError("; ".join(p for p in problems if p) or
                           "the Octopus API key is needed for the live "
                           "meter data")

    def _token_with(self, how: str, fields: dict) -> str:
        """Ask for a token one way, and say what went wrong if it fails.

        A failure here is not fatal - there is another way in - so the
        error is kept rather than raised.
        """
        try:
            data = self._call(
                "mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) "
                "{obtainKrakenToken(input: $input) {token}}",
                {"input": fields}, token="")
        except OctopusError as exc:
            self.last_error = str(exc)
            return ""
        token = ((data.get("obtainKrakenToken") or {}).get("token") or "")
        if not token:
            self.last_error = (f"the Octopus {how} was not accepted - "
                               "check it and try again")
        return token

    def meter_device_id(self, force: bool = False) -> str:
        """The id of the smart meter the Mini reads.

        The telemetry comes from the device that does the reading, and
        that device is found through the meter itself.  The old
        "octocare" query, which used to give this out, has been retired
        by Octopus - it now answers "Account is not currently
        supported." - so the meter is asked instead.
        """
        if self._device_id and not force:
            return self._device_id
        data = self._call(
            "query($account: String!) { account(accountNumber: $account) { "
            "electricityAgreements { meterPoint { mpan direction "
            "meters { serialNumber "
            "smartImportElectricityMeter { deviceId id status } "
            "smartExportElectricityMeter { deviceId id status } "
            "smartDevices { deviceId id status } } } } } }",
            {"account": self.account_number}, token=self.token())
        found: list[str] = []
        account = data.get("account")
        if not isinstance(account, dict):
            account = account[0] if _items(account) and isinstance(
                _items(account)[0], dict) else {}
        for agreement in _items(account.get("electricityAgreements")):
            if not isinstance(agreement, dict):
                continue
            for point in _items(agreement.get("meterPoint")):
                if not isinstance(point, dict):
                    continue
                for meter in _items(point.get("meters")):
                    if not isinstance(meter, dict):
                        continue
                    for key in ("smartImportElectricityMeter",
                                "smartExportElectricityMeter",
                                "smartDevices"):
                        for smart in _items(meter.get(key)):
                            if not isinstance(smart, dict):
                                continue
                            for field in ("deviceId", "id"):
                                value = smart.get(field)
                                if value and value not in found:
                                    found.append(value)
        if not found:
            shape = _shape(data.get("account"))
            if len(shape) > 400:
                shape = shape[:400] + "..."
            raise OctopusError(
                "no smart meter found on this account - the live data "
                "needs a SMETS2 smart meter, which the Mini reads "
                "(the reply was: " + shape + ")")
        self._device_ids = found
        self._device_id = found[0]
        return self._device_id

    def telemetry(self, start: datetime, end: datetime,
                  grouping: str = "HALF_HOURLY") -> list[dict]:
        """The meter's readings between two times.

        Each one is the meter's own counter: "consumption" and "export"
        are the running totals in Wh, and "demand" is the power in W at
        that moment.
        """
        self.meter_device_id()
        query = (
            "query getSmartMeterTelemetry($meterDeviceId: String!, "
            "$start: DateTime, $end: DateTime, $grouping: TelemetryGrouping) "
            "{smartMeterTelemetry(deviceId: $meterDeviceId, start: $start, "
            "end: $end, grouping: $grouping) {readAt demand consumption "
            "export consumptionDelta costDeltaWithTax}}")
        variables = {"start": _iso(start), "end": _iso(end),
                     "grouping": grouping}
        problems = []
        for device_id in self._device_ids:
            variables["meterDeviceId"] = device_id
            try:
                data = self._call(query, variables, token=self.token())
            except OctopusError as exc:
                problems.append(str(exc))
                # the session token may have expired - get a new one and
                # try again before giving up
                try:
                    self.token(force=True)
                    data = self._call(query, variables, token=self.token())
                except OctopusError as second:
                    problems.append(str(second))
                    continue
            break
        else:
            raise OctopusError("; ".join(problems) or
                               "the smart meter would not give any readings")
        rows = data.get("smartMeterTelemetry") or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append({
                "read_at": row.get("readAt"),
                "demand": _number(row.get("demand")),
                "consumption": _number(row.get("consumption")),
                "export": _number(row.get("export")),
                "delta": _number(row.get("consumptionDelta")),
                "cost": _number(row.get("costDeltaWithTax")),
            })
        return out

    def live(self) -> dict:
        """The meter's live reading - the power being used right now."""
        now = datetime.now(timezone.utc)
        rows = self.telemetry(now - timedelta(minutes=30), now, "ONE_MINUTE")
        if not rows:
            return {}
        latest = rows[-1]
        return {
            "demand_w": latest.get("demand"),
            "consumption_wh": latest.get("consumption"),
            "export_wh": latest.get("export"),
            "read_at": latest.get("read_at"),
        }

    def today(self) -> dict:
        """Today's figures, taken from the meter's own running counters.

        The Mini's counters are cumulative, so the energy used today is
        the difference between the counter now and the counter at
        midnight.  The readings are taken every half hour, which is what
        the chart wants as well.
        """
        now = datetime.now(timezone.utc)
        # the local day, not the UTC one - "today" should mean midnight
        # where the house is
        start = today_start(self.timezone_name).astimezone(timezone.utc)
        rows = self.telemetry(start - timedelta(hours=1), now, "HALF_HOURLY")
        partial = False
        if not rows:
            # The Mini may not keep a whole day of half-hourly readings,
            # so try a shorter window rather than show nothing at all.
            rows = self.telemetry(now - timedelta(hours=6), now, "HALF_HOURLY")
            partial = True
            if not rows:
                return {"series": [], "readings": 0, "partial": True}
        _add_deltas(rows)
        today_rows = [r for r in rows
                      if r.get("read_at") and _as_utc(r["read_at"]) >= start]
        consumption = sum(r.get("consumption_delta") or 0.0 for r in today_rows)
        export = sum(r.get("export_delta") or 0.0 for r in today_rows)
        cost = sum(r.get("cost") or 0.0 for r in today_rows)
        series = [{"t": r.get("read_at"),
                   "kwh": round((r.get("consumption_delta") or 0.0) / 1000.0, 4),
                   "export_kwh": round((r.get("export_delta") or 0.0) / 1000.0, 4),
                   "cost": round((r.get("cost") or 0.0) / 100.0, 4),
                   "demand_w": r.get("demand")}
                  for r in today_rows]
        # The biggest half hour, so the figures can be checked against
        # the Octopus app.  If these do not match, the chart is wrong.
        peak_in = max(today_rows,
                      key=lambda r: r.get("consumption_delta") or 0.0,
                      default=None)
        peak_out = max(today_rows,
                       key=lambda r: r.get("export_delta") or 0.0,
                       default=None)
        return {
            "consumption_kwh": round(consumption / 1000.0, 3),
            "export_kwh": round(export / 1000.0, 3),
            "cost": round(cost / 100.0, 2),
            "series": series,
            "readings": len(rows),
            "partial": partial,
            "peak_import_kwh": round(
                (peak_in.get("consumption_delta") or 0.0) / 1000.0, 3)
            if peak_in else 0.0,
            "peak_import_at": peak_in.get("read_at") if peak_in else None,
            "peak_export_kwh": round(
                (peak_out.get("export_delta") or 0.0) / 1000.0, 3)
            if peak_out else 0.0,
            "peak_export_at": peak_out.get("read_at") if peak_out else None,
        }


def _add_deltas(rows: list[dict]) -> None:
    """Turn the meter's readings into the energy of each period.

    The meter gives the running total in "consumption" and the energy of
    the period in "consumptionDelta", so the delta is used where it is
    there.  It is the period *after* the reading, which is what the
    chart wants - the bar at 12:00 is the half hour from 12:00.

    There is no export delta, so the export comes from the difference
    between one running total and the next - which is also the period
    after the reading, so it is taken from the row that follows.
    """
    for index, row in enumerate(rows):
        following = rows[index + 1] if index + 1 < len(rows) else None
        if following is None:
            # the last reading has no period after it that has finished
            row["consumption_delta"] = max(0.0, row.get("delta") or 0.0)
            row["export_delta"] = 0.0
            continue
        # the period after a reading is the difference to the next one
        given = row.get("delta")
        row["consumption_delta"] = (
            max(0.0, given) if given is not None else
            max(0.0, (following.get("consumption") or 0.0)
                - (row.get("consumption") or 0.0)))
        row["export_delta"] = max(
            0.0, (following.get("export") or 0.0)
            - (row.get("export") or 0.0))


def _as_utc(value: Any) -> datetime:
    try:
        text = str(value).replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class OctopusClient:
    """A small Octopus client. Nothing is cached; the caller decides the
    polling cadence."""

    def __init__(self, api_key: str = "", account_number: str = "",
                 mpan: str = "", meter_serial: str = "",
                 export_mpan: str = "", export_meter_serial: str = "",
                 tariff: str = "flux",
                 timezone_name: str = "Europe/London") -> None:
        self.api_key = (api_key or "").strip()
        self.account_number = (account_number or "").strip()
        self.mpan = (mpan or "").strip()
        self.meter_serial = (meter_serial or "").strip()
        self.export_mpan = (export_mpan or "").strip()
        self.export_meter_serial = (export_meter_serial or "").strip()
        self.tariff = (tariff or "flux").lower()
        self.timezone_name = timezone_name
        self._kraken = ""

    # -- plumbing --------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.api_key and (self.mpan and self.meter_serial))

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _get(self, url: str, auth: bool = True) -> Any:
        headers = {"Accept": "application/json"}
        if auth and self.api_key:
            headers.update(self._auth_header())
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise OctopusError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OctopusError(f"could not reach Octopus: {exc.reason}") from exc
        except (ValueError, OSError) as exc:
            raise OctopusError(str(exc)) from exc

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # the GraphQL API wants a session token, not the API key
            headers["Authorization"] = self.kraken_token()
        request = urllib.request.Request(GRAPHQL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OctopusError(f"GraphQL HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OctopusError(f"could not reach Octopus: {exc.reason}") from exc
        if payload.get("errors"):
            message = "; ".join(e.get("message", "") for e in payload["errors"])
            # Octopus puts the useful part in the extensions, so say that
            # too - "Invalid data." on its own tells you nothing
            details = []
            for error in payload["errors"]:
                for extra in (error.get("extensions") or {}).get("validationErrors") or []:
                    text = extra.get("message")
                    if text and text not in details:
                        details.append(text)
            raise OctopusError("; ".join(details) or message or "GraphQL error")
        return payload.get("data") or {}

    # -- meter point lookup ----------------------------------------------

    def lookup_rest(self) -> dict[str, str]:
        """Find the meter point from the account number, over plain REST.

        The account endpoint lists the meter points and the meters
        registered on them, so this needs no GraphQL and works with an
        ordinary API key.
        """
        if not self.account_number:
            raise OctopusError("no account number")
        account = self._get(f"{REST}/accounts/{self.account_number}/")
        return _meter_points(account, self)

    def kraken_token(self) -> str:
        """A session token from the API key.

        The GraphQL API wants one of these in the Authorization header,
        not the API key itself.
        """
        if not self.api_key:
            raise OctopusError("no API key")
        if self._kraken:
            return self._kraken
        mini = OctopusMini(api_key=self.api_key)
        self._kraken = mini.token()
        return self._kraken

    def lookup_graphql(self) -> dict[str, str]:
        """Find the meter point with the Kraken GraphQL API.

        Kept as a fallback: the plain REST route is tried first because it
        works with an ordinary API key.
        """
        if not self.api_key:
            raise OctopusError("no API key")
        if not self.account_number:
            raise OctopusError("no account number")
        # The agreement carries the meter point directly. The direction says
        # whether it is the import or the export meter point - Flux can have
        # both.
        query = """
        query($account: String!) {
          account(accountNumber: $account) {
            number
            electricityAgreements {
              id
              meterPoint {
                mpan
                direction
                meters { serialNumber }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"account": self.account_number})
        account = data.get("account")
        if not isinstance(account, dict):
            account = _items(account)[0] if _items(account) and isinstance(
                _items(account)[0], dict) else {}
        agreements = _items(account.get("electricityAgreements"))
        if not agreements:
            raise OctopusError(
                "no electricity agreement found for that account number")

        result = {"mpan": "", "meter_serial": "", "export_mpan": "",
                  "export_meter_serial": "", "tariff_code": ""}
        for agreement in agreements:
            if not isinstance(agreement, dict):
                continue
            for point in _items(agreement.get("meterPoint")):
                if not isinstance(point, dict):
                    continue
                mpan = point.get("mpan") or ""
                if not mpan:
                    continue
                serial = ""
                for meters in _items(point.get("meters")):
                    if isinstance(meters, dict):
                        serial = meters.get("serialNumber") or ""
                        if serial:
                            break
                direction = str(point.get("direction") or "").upper()
                if direction == "EXPORT":
                    if not result["export_mpan"]:
                        result["export_mpan"] = mpan
                        result["export_meter_serial"] = serial
                elif not result["mpan"]:
                    result["mpan"] = mpan
                    result["meter_serial"] = serial
        if not result["mpan"] and result["export_mpan"]:
            # only an export meter point: use it for the import readings too
            result["mpan"] = result["export_mpan"]
            result["meter_serial"] = result["export_meter_serial"]
        if not result["mpan"]:
            raise OctopusError(
                "no meter point found for that account number")
        return result

    def lookup(self) -> dict[str, str]:
        """Find the meter point from the account number.

        REST first, GraphQL second - either way the MPAN and the meter
        serial come back, so the user only has to paste an account number.
        """
        errors = []
        for finder in (self.lookup_rest, self.lookup_graphql):
            try:
                found = finder()
            except OctopusError as exc:
                errors.append(f"{finder.__name__.replace('lookup_', '')}: {exc}")
                continue
            if found.get("mpan") and found.get("meter_serial"):
                return found
            errors.append(f"{finder.__name__.replace('lookup_', '')}: no meter found")
        raise OctopusError("; ".join(errors) or "could not look up the meter")

    # -- consumption ------------------------------------------------------

    def consumption(self, mpan: str, meter_serial: str,
                     start: datetime, end: datetime) -> list[dict]:
        """Half-hourly readings between two times.

        Each reading is {"start": iso, "end": iso, "kwh": float}.
        """
        if not mpan or not meter_serial:
            raise OctopusError("no meter point")
        # Half-hourly is what the endpoint returns by default, so no
        # group_by is sent - the only accepted values are hour, day, week,
        # month and quarter, and "half-hour" is rejected.  The ordering is
        # "period" for oldest first, or "-period" for newest first.
        base = (f"{REST}/electricity-meter-points/{urllib.parse.quote(mpan)}"
                f"/meters/{urllib.parse.quote(meter_serial)}/consumption/")

        # The latest page first, and the window cut out of it here.  A
        # query bounded by period_from and period_to comes back with the
        # right number of periods and next to no energy in them - and
        # with the same answer for two different meters - so it is not
        # trusted.
        payload = self._get(base + "?page_size=1000")
        rows = payload.get("results") or []
        readings = self._readings(rows)
        inside = self._within(readings, start, end)
        if inside:
            return inside

        # Nothing in the window - ask for the period itself, in case the
        # readings are older than the latest page reaches.
        params = {
            "period_from": _iso(start),
            "period_to": _iso(end),
            "order_by": "period",
            "page_size": "250",
        }
        payload = self._get(base + "?" + urllib.parse.urlencode(params))
        rows = payload.get("results") or []
        readings = self._readings(rows)
        if rows and not readings:
            # The meter did answer, but nothing in it could be read - say
            # so, rather than looking like an empty meter.
            raise OctopusError(
                f"the meter returned {len(rows)} readings but none could be "
                f"read - the first one was {rows[0]}")
        return self._within(readings, start, end)

    @staticmethod
    def _readings(rows: list) -> list[dict]:
        """Turn the API's rows into readings."""
        readings = []
        for row in rows:
            try:
                kwh = float(row.get("consumption"))
            except (TypeError, ValueError):
                continue
            if kwh < 0:
                continue
            readings.append({
                "start": row.get("interval_start"),
                "end": row.get("interval_end"),
                "kwh": kwh,
            })
        readings.sort(key=lambda r: str(r.get("start") or ""))
        return readings

    @staticmethod
    def _within(readings: list[dict], start: datetime,
                end: datetime) -> list[dict]:
        """The readings that fall inside the window."""
        inside = []
        for row in readings:
            when = _parse_iso(row.get("start"))
            if when is None:
                inside.append(row)
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if start <= when < end:
                inside.append(row)
        return inside

    # -- rates ------------------------------------------------------------

    def _tariff_code(self, kind: str) -> str:
        product, fallback = TARIFFS.get(self.tariff, FLUX_PRODUCTS)[kind]
        try:
            data = self._get(f"{REST}/products/{product}/", auth=False)
        except OctopusError:
            return fallback
        tariffs = data.get("single_register_electricity_tariffs") or {}
        for variant in ("_A", "_B", "_C", "_D"):
            entry = (tariffs.get(variant) or {}).get("direct_debit_monthly")
            if entry and entry.get("code"):
                return entry["code"]
        return fallback

    def unit_rate(self, kind: str, when: datetime) -> dict | None:
        """The unit rate in p/kWh for the half-hour containing *when*."""
        product, tariff = TARIFFS.get(self.tariff, FLUX_PRODUCTS)[kind]
        tariff = self._tariff_code(kind) if kind == "import" else tariff
        start = (when - timedelta(hours=2)).astimezone(timezone.utc)
        end = (when + timedelta(hours=2)).astimezone(timezone.utc)
        params = {"period_from": _iso(start), "period_to": _iso(end)}
        url = (f"{REST}/products/{product}/electricity-tariffs/{tariff}"
               f"/standard-unit-rates/?{urllib.parse.urlencode(params)}")
        try:
            payload = self._get(url, auth=False)
        except OctopusError:
            return None
        for row in payload.get("results") or []:
            valid_from = _parse_iso(row.get("valid_from"))
            valid_to = _parse_iso(row.get("valid_to"))
            if valid_from and valid_to and valid_from <= when < valid_to:
                return {
                    "p_kwh": row.get("value_inc_vat"),
                    "from": row.get("valid_from"),
                    "to": row.get("valid_to"),
                }
        return None

    def rate_series(self, kind: str, start: datetime, end: datetime) -> list[dict]:
        """Every published rate between two times."""
        product, tariff = TARIFFS.get(self.tariff, FLUX_PRODUCTS)[kind]
        if kind == "import":
            tariff = self._tariff_code(kind)
        params = {"period_from": _iso(start), "period_to": _iso(end)}
        url = (f"{REST}/products/{product}/electricity-tariffs/{tariff}"
               f"/standard-unit-rates/?{urllib.parse.urlencode(params)}")
        try:
            payload = self._get(url, auth=False)
        except OctopusError:
            return []
        out = []
        for row in payload.get("results") or []:
            out.append({
                "from": row.get("valid_from"),
                "to": row.get("valid_to"),
                "p_kwh": row.get("value_inc_vat"),
            })
        return out

    def rate_at(self, series: list[dict], when: datetime) -> float | None:
        """The rate covering a moment, from a series of published rates."""
        for row in series:
            start = _parse_iso(row.get("from"))
            end = _parse_iso(row.get("to"))
            if start and end and start <= when < end:
                try:
                    return float(row["p_kwh"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    def rates(self, when: datetime | None = None) -> dict:
        """The import and export rates right now, plus the Flux band."""
        when = when or _now(self.timezone_name)
        band, until = self.band(when)
        import_rate = self.unit_rate("import", when)
        export_rate = self.unit_rate("export", when)
        standard_import = self._standard_rate("import")
        standard_export = self._standard_rate("export")
        return {
            "band": band,
            "band_until": until,
            "import_p_kwh": (import_rate or {}).get("p_kwh"),
            "export_p_kwh": (export_rate or {}).get("p_kwh"),
            "import_standard_p_kwh": standard_import,
            "export_standard_p_kwh": standard_export,
            "import_premium_p_kwh": _premium(import_rate, standard_import),
            "export_premium_p_kwh": _premium(export_rate, standard_export),
            "tariff": self.tariff,
        }

    def _standard_rate(self, kind: str) -> float | None:
        """The standard (non-peak, non-cheap) rate, for the premium figure."""
        product, tariff = TARIFFS.get(self.tariff, FLUX_PRODUCTS)[kind]
        try:
            data = self._get(f"{REST}/products/{product}/", auth=False)
        except OctopusError:
            return None
        tariffs = data.get("single_register_electricity_tariffs") or {}
        for variant in ("_A", "_B"):
            entry = (tariffs.get(variant) or {}).get("direct_debit_monthly")
            if entry and entry.get("standard_unit_rate_inc_vat") is not None:
                return entry["standard_unit_rate_inc_vat"]
        return None

    def band(self, when: datetime | None = None) -> tuple[str, str]:
        """Which Flux band we are in, and when it ends (UK local time)."""
        when = when or _now(self.timezone_name)
        local = when.astimezone(ZoneInfo(self.timezone_name)) \
            if _has_zone(self.timezone_name) else when
        hour = local.hour
        for name, (start, end) in FLUX_BANDS.items():
            if start <= hour < end:
                until = local.replace(hour=end, minute=0, second=0, microsecond=0)
                return name, until.isoformat()
        return "standard", ""

    # -- standing charge --------------------------------------------------

    def standing_charge(self) -> float | None:
        """The daily standing charge in pence."""
        product, tariff = TARIFFS.get(self.tariff, FLUX_PRODUCTS)["import"]
        tariff = self._tariff_code("import")
        url = (f"{REST}/products/{product}/electricity-tariffs/{tariff}"
               f"/standing-charges/")
        try:
            payload = self._get(url, auth=False)
        except OctopusError:
            return None
        for row in payload.get("results") or []:
            value = row.get("value_inc_vat")
            if value is not None:
                return value
        return None


# -- helpers -------------------------------------------------------------

def _energy(rows: list) -> float:
    """The total consumption in a set of rows."""
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("consumption"))
        except (TypeError, ValueError):
            continue
    return total


def _is_export(product_code: str) -> bool:
    return "EXPORT" in (product_code or "").upper()


def _meter_points(payload: Any,
                  client: "OctopusClient | None" = None) -> dict[str, str]:
    """Pull the meter points and their meters out of an account response.

    The shape of the account payload has changed over the years, so rather
    than guessing at one layout this walks the response and collects
    anything that looks like a meter point: a 13-digit MPAN together with
    a list of meters.

    A dual-register meter - which is what Flux has - is registered as two
    meter points: the import one, and an export one that names its
    partner in "import_mpan".  The account response does not always say
    which is which, so the meter point is asked directly.
    """
    result = {"mpan": "", "meter_serial": "", "export_mpan": "",
              "export_meter_serial": "", "tariff_code": ""}
    for point in _find_meter_points(payload):
        mpan = str(point.get("mpan") or "")
        if not mpan.isdigit() or len(mpan) != 13:
            continue
        meters = point.get("meters")
        if not isinstance(meters, list):
            meters = []
        # is this an export meter point, and which import does it belong to?
        export_point = False
        if client is not None:
            export_point = _is_export_point(client, mpan)
        seen = 0
        for meter in meters:
            if not isinstance(meter, dict):
                continue
            serial = str(meter.get("serial_number")
                         or meter.get("serialNumber") or "")
            if not serial:
                continue
            direction = str(meter.get("direction") or "").strip().lower()
            is_export = direction.startswith("exp") or export_point
            if not direction and not export_point and seen >= 1:
                # nothing says which is which: on a meter point with two
                # meters, the first is the import and the second the export
                is_export = True
            if is_export:
                if not result["export_mpan"]:
                    result["export_mpan"] = mpan
                    result["export_meter_serial"] = serial
            elif not result["mpan"]:
                result["mpan"] = mpan
                result["meter_serial"] = serial
            seen += 1
    return result


def meter_point_detail(client: "OctopusClient", mpan: str) -> dict:
    """What the meter point says about itself.

    An export meter point names the import it belongs to in
    "import_mpan", which is the only reliable way to tell the two
    apart - the account response does not always label them.
    """
    try:
        detail = client._get(f"{REST}/electricity-meter-points/"
                             f"{urllib.parse.quote(mpan)}/")
    except Exception:  # noqa: BLE001
        return {}
    return detail if isinstance(detail, dict) else {}


def _is_export_point(client: "OctopusClient", mpan: str) -> bool:
    """Is this meter point the export half of a dual-register meter?"""
    return bool(str(meter_point_detail(client, mpan)
                    .get("import_mpan") or "").strip())


def _all_meters(payload: Any) -> list[dict[str, str]]:
    """Every meter on every meter point, with its direction.

    A dual-register meter has two of them - one for import, one for
    export - and they have different serial numbers.
    """
    out: list[dict[str, str]] = []
    for point in _find_meter_points(payload):
        mpan = str(point.get("mpan") or "")
        if not mpan.isdigit() or len(mpan) != 13:
            continue
        meters = point.get("meters")
        if not isinstance(meters, list):
            continue
        for meter in meters:
            if not isinstance(meter, dict):
                continue
            serial = str(meter.get("serial_number")
                         or meter.get("serialNumber") or "")
            if not serial:
                continue
            out.append({
                "mpan": mpan,
                "serial": serial,
                "direction": str(meter.get("direction") or "").strip().lower(),
            })
    return out


def _find_meter_points(payload: Any) -> list:
    """Every dict in the payload that carries an MPAN and a list of meters."""
    found = []
    if isinstance(payload, dict):
        mpan = payload.get("mpan")
        meters = payload.get("meters")
        if isinstance(mpan, str) and isinstance(meters, list):
            found.append(payload)
        for value in payload.values():
            found.extend(_find_meter_points(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find_meter_points(item))
    return found


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _premium(rate: dict | None, standard: float | None) -> float | None:
    """How much better (or worse) the current rate is than standard."""
    if not rate or standard is None:
        return None
    try:
        return round(float(rate["p_kwh"]) - float(standard), 2)
    except (KeyError, TypeError, ValueError):
        return None


def _has_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False
