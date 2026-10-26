"""FoxESS (Foxconn) Cloud API client - standard library only.

Two families of endpoint live in the FoxESS cloud:

* Open API  - ``https://www.foxesscloud.com/op/v...`` - authenticated with a
  long-lived API key plus an MD5 signature.  Data freshness is limited by the
  datalogger's upload cadence (measured: 300 s exactly).
* Web API   - ``https://www.foxesscloud.com/basic/...`` and ``/dew/...`` -
  authenticated with a session token from a username/password login.  This is
  the family that serves the live WebSocket feed.

Signing rules differ subtly between the two families and both are handled
here:

* Open API signs the request **path** with a real CRLF:
  ``md5(path + "\\r\\n" + token + "\\r\\n" + timestamp)``
* The web portal's own JavaScript signs with the **literal four characters**
  ``\\r\\n`` (backslash-r-backslash-n) instead of a real CRLF.

Mixing those up is the single most common cause of ``errno 40256``
("illegal signature").
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EU_DOMAIN = "https://www.foxesscloud.com"
US_DOMAIN = "https://portal.foxesscloud.us"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ERRNO_SIGNATURE = 40256
ERRNO_RATE_LIMIT = 40400


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def build_headers(path: str, token: str, lang: str = "en") -> dict[str, str]:
    """Open API headers: token + timestamp + MD5 signature over the path."""
    timestamp = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "token": token,
        "lang": lang,
        "timestamp": timestamp,
        "signature": md5(f"{path}\r\n{token}\r\n{timestamp}"),
    }


class FoxESSError(RuntimeError):
    """Raised when the cloud returns a non-zero errno."""

    def __init__(self, errno: Any, en: str = "", zh: str = ""):
        self.errno = errno
        super().__init__(f"errno={errno} en={en} zh={zh}")


class FoxESSApi:
    """Thin wrapper over the FoxESS Open API."""

    def __init__(self, domain: str = EU_DOMAIN, timeout: int = 25):
        self.domain = domain.rstrip("/")
        self.timeout = timeout

    # -- plumbing --------------------------------------------------------

    def request(self, path: str, token: str, body: dict | None = None,
                params: dict | None = None) -> dict:
        url = self.domain + path
        if params:
            url = f"{url}?{urlencode(params)}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(
            url,
            data=payload,
            headers=build_headers(path, token),
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise FoxESSError(f"HTTP {exc.code}", detail) from None
        except URLError as exc:
            raise FoxESSError("network", str(exc.reason)) from None

        data = json.loads(raw)
        errno = data.get("errno")
        if errno not in (0, None):
            raise FoxESSError(errno, str(data.get("en") or ""), str(data.get("zh_CN") or ""))
        return data

    # -- endpoints --------------------------------------------------------

    def access_count(self, token: str) -> dict:
        return (self.request("/op/v0/user/getAccessCount", token).get("result") or {})

    def device_list(self, token: str) -> list[dict]:
        body = {"currentPage": 1, "pageSize": 100}
        result = self.request("/op/v0/device/list", token, body).get("result") or {}
        return result.get("data") or []

    def device_detail(self, token: str, sn: str) -> dict:
        return (self.request("/op/v1/device/detail", token, params={"sn": sn})
                .get("result") or {})

    def module_list(self, token: str) -> list[dict]:
        body = {"currentPage": 1, "pageSize": 100}
        result = self.request("/op/v0/module/list", token, body).get("result") or {}
        return result.get("data") or []

    def plant_list(self, token: str) -> list[dict]:
        body = {"currentPage": 1, "pageSize": 100}
        result = self.request("/op/v0/plant/list", token, body).get("result") or {}
        return result.get("data") or []

    def battery_soc(self, token: str, sn: str) -> dict:
        return (self.request("/op/v0/device/battery/soc/get", token, params={"sn": sn})
                .get("result") or {})

    def report_query(self, token: str, sn: str, dimension: str,
                     year: int, month: int, day: int,
                     variables: list[str]) -> list[dict]:
        """The production report.

        "month" gives one value per day of that month, "day" gives one
        value per hour of that day.  The values come back positionally,
        so the day of the month is the index.
        """
        body = {"sn": sn, "dimension": dimension, "variables": variables,
                "year": year, "month": month, "day": day}
        return (self.request("/op/v0/device/report/query", token, body)
                .get("result") or [])

    def generation_report(self, token: str, sn: str,
                          dimension: str = "day") -> dict:
        """Today's, this month's and the lifetime generation."""
        body = {"sn": sn, "dimension": dimension}
        return (self.request("/op/v0/device/generation", token, body)
                .get("result") or {})

    def real_time(self, token: str, sns: list[str],
                  variables: list[str] | None = None) -> list[dict]:
        body: dict[str, Any] = {"sns": sns}
        if variables:
            body["variables"] = variables
        return (self.request("/op/v1/device/real/query", token, body).get("result") or [])


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def report_values(result: list[dict], variable: str) -> list:
    """The values of one variable in a production report.

    The variables come back in the order they were asked for, and each
    one has a "variable" name and a positional list of "values".
    """
    for entry in result or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("variable") or entry.get("name")
        if name == variable:
            values = entry.get("values")
            if isinstance(values, list):
                return values
    return []


def variables_from_real_time(result: list[dict]) -> dict[str, float]:
    """Flatten a v1 real/query response into ``{variable: value}``.

    The v1 endpoint returns the timestamp at DEVICE level
    (``result[0]['time']``), not on each variable - v0 put it on every
    variable.  Older per-variable timestamps are still honoured.
    """
    flat: dict[str, float] = {}
    for device in result:
        dev_ts = device.get("time")
        for entry in device.get("datas") or []:
            name = entry.get("variable")
            if name is None:
                continue
            try:
                flat[name] = float(entry.get("value"))
            except (TypeError, ValueError):
                continue
            flat.setdefault("_time", dev_ts)  # type: ignore[arg-type]
    return flat


def device_timestamp(result: list[dict]) -> str | None:
    if not result:
        return None
    return result[0].get("time")
