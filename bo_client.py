"""Small BookOrbit v3.5.0 API client with memory-only authentication.

The engine owns conflict decisions, operation journals, manifest checks, and
BookOrbit's automatic status side effects. Mutating requests are NEVER retried.
GET and the exact read-only book-query POST may retry once after a 401 response.
"""

from datetime import date, datetime
import hashlib
import re
from urllib.parse import urlsplit
from uuid import UUID

import requests


_UNSET = object()
_ASSET_ID = re.compile(r"aud_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I)
_REVISION = re.compile(r"[0-9a-f]{64}")


class RequestError(Exception):
    """Sanitized API failure; excludes URLs, credentials, and response bodies."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class BOClient:
    PAGE_SIZE = 200
    MAX_PAGES = 500
    MAX_FILE_SHA256_BYTES = 10 * 1024 ** 3
    STATUSES = frozenset(("unread", "want_to_read", "reading", "on_hold",
                          "rereading", "read", "skimmed", "abandoned"))

    def __init__(self, base_url, username, password):
        try:
            parsed = urlsplit(base_url)
            valid = (
                parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and parsed.path in ("", "/")
            )
            parsed.port
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Invalid BookOrbit base URL")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("BookOrbit username is missing")
        if not isinstance(password, str) or not password:
            raise ValueError("BookOrbit password is missing")
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._authenticated = False
        self.user = None
        self.session = requests.Session()
        self.session.trust_env = False

    def close(self):
        self.session.headers.pop("Authorization", None)
        self.session.close()
        self._password = ""
        self._authenticated = False
        self.user = None

    @staticmethod
    def _id(value):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            return value
        raise ValueError("Invalid BookOrbit identifier")

    @staticmethod
    def _integer(value, minimum=0):
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    @staticmethod
    def _timestamp(value):
        if not isinstance(value, str) or "T" not in value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.tzinfo is not None and parsed.utcoffset() is not None
        except ValueError:
            return False

    @classmethod
    def _date_value(cls, value):
        if value is None:
            return True
        if not isinstance(value, str):
            return False
        if cls._timestamp(value):
            return True
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return False
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False

    def _send(self, method, path, label, payload):
        try:
            return self.session.request(
                method, self.base_url + path, params=None, json=payload,
                timeout=(5, 20), allow_redirects=False, verify=True,
            )
        except requests.RequestException:
            raise RequestError(f"{method} {label}: transport failure") from None

    @staticmethod
    def _json(response, method, label, *, allow_null=False):
        try:
            result = response.json()
        except ValueError:
            raise RequestError(f"{method} {label}: invalid JSON", response.status_code) from None
        if result is None and allow_null:
            return None
        if not isinstance(result, dict):
            raise RequestError(f"{method} {label}: invalid object schema", response.status_code)
        return result

    def _login(self):
        self._authenticated = False
        self.session.headers.pop("Authorization", None)
        label = "/api/v1/auth/login"
        response = self._send("POST", label, label,
                              {"username": self._username, "password": self._password})
        if response.status_code != 200:
            raise RequestError(f"POST {label}: HTTP {response.status_code}", response.status_code)
        result = self._json(response, "POST", label)
        token = result.get("accessToken")
        if (not isinstance(token, str) or not token.strip()
                or any(char.isspace() for char in token)):
            raise RequestError(f"POST {label}: invalid token schema", 200)
        self.session.headers["Authorization"] = "Bearer " + token
        self._authenticated = True
        self.user = result.get("user")

    def _request(self, method, path, label, *, payload=None, allow_null=False,
                 success=(200,)):
        if not self._authenticated:
            self._login()
        response = self._send(method, path, label, payload)
        # The exact book-query POST is read-only, just like GET. Refresh an
        # expired login once and repeat that same page; never retry mutations.
        # The transaction engine alone decides what to do after write failures.
        read_only = method == "GET" or (method == "POST" and path == "/api/v1/books/query")
        if response.status_code == 401 and read_only:
            self._login()
            response = self._send(method, path, label, payload)
        if response.status_code not in success:
            if response.status_code == 401:
                self._authenticated = False
            raise RequestError(f"{method} {label}: HTTP {response.status_code}", response.status_code)
        return self._json(response, method, label, allow_null=allow_null)

    def file_sha256(self, file_id, expected_size):
        """Hash the complete original download in bounded memory, never a derivative.

        Only a pre-stream 401 may retry. Size/transport failures never return a
        partial digest; callers still own before/after identity validation.
        """
        file_id = self._id(file_id)
        if (not self._integer(expected_size, 1)
                or expected_size > self.MAX_FILE_SHA256_BYTES):
            raise ValueError('Invalid original-file size for SHA-256')
        label = '/api/v1/books/files/{file_id}/download'
        path = '/api/v1/books/files/' + file_id + '/download'
        if not self._authenticated:
            self._login()
        for attempt in range(2):
            response = None
            try:
                response = self.session.request(
                    'GET', self.base_url + path, params=None, json=None,
                    headers={'Accept-Encoding': 'identity', 'Range': None,
                             'If-Range': None, 'If-None-Match': None,
                             'If-Modified-Since': None},
                    timeout=(5, 30), allow_redirects=False, verify=True, stream=True,
                )
                status = response.status_code
                if status == 401:
                    self._authenticated = False
                    if attempt == 0:
                        response.close()
                        response = None
                        self._login()
                        continue
                if status != 200:
                    raise RequestError(f'GET {label}: HTTP {status}', status)
                size = response.headers.get('Content-Length')
                if (not isinstance(size, str) or re.fullmatch(r'[0-9]+', size) is None
                        or len(size) > 20 or int(size) != expected_size
                        or response.headers.get('Content-Range') is not None
                        or response.headers.get('Content-Encoding') is not None
                        or response.headers.get('Transfer-Encoding') is not None):
                    raise RequestError(f'GET {label}: invalid original-file response', status)
                digest = hashlib.sha256()
                received = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024, decode_unicode=False):
                    if not isinstance(chunk, bytes):
                        raise RequestError(f'GET {label}: invalid original-file body', status)
                    received += len(chunk)
                    if received > expected_size:
                        raise RequestError(f'GET {label}: original-file size changed', status)
                    digest.update(chunk)
                if received != expected_size:
                    raise RequestError(f'GET {label}: original-file body incomplete', status)
                return digest.hexdigest()
            except requests.RequestException:
                raise RequestError(f'GET {label}: original-file transport failure') from None
            finally:
                if response is not None:
                    response.close()
        raise RequestError(f'GET {label}: authentication failed', 401)

    def query(self):
        """Read all cards; refuse partial, duplicate, or changing pagination."""
        label = "/api/v1/books/query"
        results, seen = [], set()
        expected_total = None
        for page in range(self.MAX_PAGES):
            data = self._request("POST", label, label, success=(200, 201), payload={
                "collapseSeries": False,
                "sort": [{"field": "title", "dir": "asc"}],
                "pagination": {"page": page, "size": self.PAGE_SIZE},
            })
            items, total = data.get("items"), data.get("total")
            if (not isinstance(items, list) or any(not isinstance(x, dict) for x in items)
                    or not self._integer(total)
                    or not self._integer(data.get("page")) or data["page"] != page
                    or not self._integer(data.get("size"), 1) or data["size"] != self.PAGE_SIZE):
                raise RequestError(f"POST {label}: invalid pagination schema", 200)
            if expected_total is not None and total != expected_total:
                raise RequestError(f"POST {label}: inventory changed during pagination", 200)
            expected_total = total
            if len(items) > self.PAGE_SIZE or len(results) + len(items) > total:
                raise RequestError(f"POST {label}: inconsistent pagination", 200)
            for item in items:
                item_id = item.get("id")
                if not self._integer(item_id, 1) or item_id in seen:
                    raise RequestError(f"POST {label}: duplicate or missing book ID", 200)
                seen.add(item_id)
            results.extend(items)
            if len(results) == total:
                return results
            if len(items) != self.PAGE_SIZE:
                raise RequestError(f"POST {label}: incomplete pagination", 200)
        raise RequestError(f"POST {label}: pagination limit exceeded")

    def detail(self, book_id):
        return self._request("GET", f"/api/v1/books/{self._id(book_id)}", "/api/v1/books/{book_id}")

    def manifest(self, book_id):
        return self._request("GET", f"/api/v1/audiobooks/{self._id(book_id)}/manifest",
                             "/api/v1/audiobooks/{book_id}/manifest")

    def playback(self, book_id):
        # A missing/unavailable endpoint (404) must NOT masquerade as no progress.
        return self._request("GET", f"/api/v1/audiobooks/{self._id(book_id)}/playback-state",
                             "/api/v1/audiobooks/{book_id}/playback-state", allow_null=True)

    def library(self, library_id):
        return self._request("GET", f"/api/v1/libraries/{self._id(library_id)}",
                             "/api/v1/libraries/{library_id}")

    def write_position(self, book_id, asset_id, position_ms, base_revision,
                       manifest_revision, operation_id, captured_at):
        book_id = self._id(book_id)
        if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
            raise ValueError("Invalid BookOrbit asset identifier")
        if not self._integer(position_ms) or not self._integer(base_revision):
            raise ValueError("Invalid BookOrbit position or base revision")
        if not isinstance(manifest_revision, str) or not _REVISION.fullmatch(manifest_revision):
            raise ValueError("Invalid BookOrbit manifest revision")
        try:
            valid_operation = (isinstance(operation_id, str) and
                               str(UUID(operation_id)) == operation_id.lower())
        except (ValueError, AttributeError):
            valid_operation = False
        if not valid_operation or not self._timestamp(captured_at):
            raise ValueError("Invalid BookOrbit operation or capture timestamp")
        return self._request("PUT", f"/api/v1/audiobooks/{book_id}/playback-state",
                             "/api/v1/audiobooks/{book_id}/playback-state", payload={
            "assetId": asset_id, "positionMs": position_ms,
            "capturedAt": captured_at, "operationId": operation_id,
            "baseRevision": base_revision, "manifestRevision": manifest_revision,
        })

    def write_status(self, book_id, status, started_at=_UNSET, finished_at=_UNSET):
        book_id = self._id(book_id)
        if not isinstance(status, str) or status not in self.STATUSES:
            raise ValueError("Invalid BookOrbit reading status")
        payload = {"status": status}
        for key, value in (("startedAt", started_at), ("finishedAt", finished_at)):
            if value is not _UNSET:
                if not self._date_value(value):
                    raise ValueError("Invalid BookOrbit lifecycle date")
                payload[key] = value
        return self._request("PATCH", f"/api/v1/books/{book_id}/status",
                             "/api/v1/books/{book_id}/status", payload=payload)
