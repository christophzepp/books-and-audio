"""Small, non-retrying Audiobookshelf 2.37.1 API client.

The transaction engine owns concurrency checks, journaling, and the separate
position/completion steps required by ABS. This module never invents sessions.
"""

import math
from urllib.parse import quote, urlsplit

import requests


class RequestError(Exception):
    """A sanitized API failure; never contains credentials or response bodies."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ABSClient:
    PAGE_SIZE = 100

    def __init__(self, base_url, token):
        try:
            parsed = urlsplit(base_url)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
            parsed.port  # Reject malformed ports without echoing the input.
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Invalid ABS base URL")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("ABS token is missing")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": "Bearer " + token})

    def close(self):
        self.session.close()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not value or value in (".", ".."):
            raise ValueError("Invalid ABS identifier")
        return quote(value, safe="")

    def _request(self, method, path, label, *, params=None, payload=None,
                 missing_ok=False, expect_json=True):
        try:
            response = self.session.request(
                method, self.base_url + path, params=params, json=payload,
                timeout=(5, 20), allow_redirects=False, verify=True,
            )
        except requests.RequestException:
            raise RequestError(f"{method} {label}: transport failure") from None
        status = response.status_code
        if missing_ok and status == 404:
            return None
        if status != 200:
            raise RequestError(f"{method} {label}: HTTP {status}", status)
        if not expect_json:
            return None
        try:
            result = response.json()
        except ValueError:
            raise RequestError(f"{method} {label}: invalid JSON", status) from None
        if not isinstance(result, dict):
            raise RequestError(f"{method} {label}: invalid object schema", status)
        return result

    @staticmethod
    def _list(data, key, label):
        items = data.get(key)
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            raise RequestError(f"GET {label}: invalid list schema", 200)
        return items

    def version(self):
        """GET /status has serverVersion in the verified v2.37.1 server."""
        result = self._request("GET", "/status", "/status")
        version = result.get("serverVersion")
        if result.get("app") != "audiobookshelf" or not isinstance(version, str) or not version:
            raise RequestError("GET /status: invalid version schema", 200)
        return version

    def libraries(self):
        result = self._request("GET", "/api/libraries", "/api/libraries")
        return self._list(result, "libraries", "/api/libraries")

    def inventory(self, libid):
        path = f"/api/libraries/{self._id(libid)}/items"
        label = "/api/libraries/{library_id}/items"
        results, seen = [], set()
        expected_total = None
        page = 0
        while True:
            result = self._request(
                "GET", path, label,
                params={"limit": self.PAGE_SIZE, "page": page},
            )
            items = self._list(result, "results", label)
            total = result.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise RequestError(f"GET {label}: invalid total", 200)
            if expected_total is not None and total != expected_total:
                raise RequestError(f"GET {label}: inventory changed during pagination", 200)
            expected_total = total
            if len(items) > self.PAGE_SIZE or len(results) + len(items) > total:
                raise RequestError(f"GET {label}: inconsistent pagination", 200)
            for item in items:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id or item_id in seen:
                    raise RequestError(f"GET {label}: duplicate or missing item ID", 200)
                seen.add(item_id)
            results.extend(items)
            if len(results) == total:
                return results
            if len(items) != self.PAGE_SIZE:
                raise RequestError(f"GET {label}: incomplete pagination", 200)
            page += 1

    def item(self, item_id):
        return self._request(
            "GET", f"/api/items/{self._id(item_id)}", "/api/items/{item_id}",
            params={"expanded": 1, "include": "progress"},
        )

    def progress(self, item_id):
        return self._request(
            "GET", f"/api/me/progress/{self._id(item_id)}",
            "/api/me/progress/{item_id}", missing_ok=True,
        )

    def allprogress(self):
        result = self._request("GET", "/api/me/progress", "/api/me/progress")
        return self._list(result, "mediaProgress", "/api/me/progress")

    @staticmethod
    def _finite_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def write_position(self, item_id, position, duration):
        if (not self._finite_number(position) or not self._finite_number(duration)
                or duration <= 0 or not 0 <= position <= duration):
            raise ValueError("Invalid ABS position or duration")
        return self._request(
            "PATCH", f"/api/me/progress/{self._id(item_id)}",
            "/api/me/progress/{item_id}", expect_json=False,
            payload={
                "currentTime": position, "duration": duration,
                "progress": position / duration, "markAsFinishedTimeRemaining": 0,
            },
        )

    def write_finished(self, item_id, finished, finished_at=None):
        if not isinstance(finished, bool):
            raise ValueError("ABS finished status must be boolean")
        payload = {"isFinished": finished, "markAsFinishedTimeRemaining": 0}
        if finished_at is not None:
            if (not finished or not self._finite_number(finished_at)
                    or finished_at <= 0 or int(finished_at) != finished_at):
                raise ValueError("Invalid ABS completion timestamp")
            payload["finishedAt"] = int(finished_at)
        return self._request(
            "PATCH", f"/api/me/progress/{self._id(item_id)}",
            "/api/me/progress/{item_id}", payload=payload, expect_json=False,
        )
