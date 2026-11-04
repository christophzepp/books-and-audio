"""Private, authenticated web shell for queued sync administration.

Only AdminStore submits commands. This service never opens the synchronization
SQLite database, edits connection credentials itself, or calls media services.
"""
import argparse
import base64
import collections
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import threading
import time
from urllib.parse import urlsplit

BODY_LIMIT = 64 * 1024
SESSION_SECONDS = 8 * 3600
CSRF_SECONDS = 15 * 60
COOKIE = "sync_admin"
CSRF_COOKIE = "sync_admin_csrf"
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
from admin_store import KINDS
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"


class WebError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def parse_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise WebError(400, "Die Anfrage ist ungültig.") from None


def atomic_private(path, raw):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or not 0 < info.st_size <= limit:
            raise ValueError("private file permissions")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) != info.st_size:
            raise ValueError("private file changed")
        return raw
    finally:
        os.close(fd)


class Auth:
    def __init__(self, root, clock=time.time):
        self.directory = Path(root) / "web-auth"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("private auth directory required")
        fsync_dir(self.directory.parent)
        self.credentials = self.directory / "credentials.json"
        self.setup_file = self.directory / "setup-token"
        self.lock = threading.RLock()
        self.clock = clock
        self.sessions, self.preauth, self.attempts = {}, {}, {}
        self.record = None
        if self.credentials.exists():
            self.record = parse_json(private_read(self.credentials, 4096))
            if set(self.record) != {"version", "username", "salt", "hash"} or self.record["version"] != 1 or not isinstance(self.record["username"], str) or not re.fullmatch(r"[0-9a-f]{128}", self.record["hash"]) or len(base64.b64decode(self.record["salt"], validate=True)) != 16:
                raise ValueError("invalid auth record")
            self.setup_file.unlink(missing_ok=True)
            fsync_dir(self.directory)
        elif not self.setup_file.exists():
            atomic_private(self.setup_file, (secrets.token_urlsafe(32) + "\n").encode())
        if not self.record:
            token = private_read(self.setup_file, 256).decode().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError("invalid bootstrap token")
        self.dummy_salt = secrets.token_bytes(16)

    @property
    def initialized(self):
        return self.record is not None

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, maxmem=128 * 1024**2, dklen=64).hex()

    def prune(self):
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        self.preauth = {k: expiry for k, expiry in self.preauth.items() if expiry > now}
        self.attempts = {key: [t for t in times if t > now - 600] for key, times in self.attempts.items() if any(t > now - 600 for t in times)}

    def nonce(self):
        with self.lock:
            self.prune()
            if len(self.preauth) >= 2048:
                del self.preauth[min(self.preauth, key=self.preauth.get)]
            token = secrets.token_urlsafe(32)
            self.preauth[token] = self.clock() + CSRF_SECONDS
            return token

    def session(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with self.lock:
            self.prune()
            return self.sessions.get(hashlib.sha256(token.encode()).hexdigest())

    def check_csrf(self, session, cookie, header):
        if not isinstance(cookie, str) or not isinstance(header, str) or not hmac.compare_digest(cookie, header):
            raise WebError(403, "Die Sicherheitsprüfung ist fehlgeschlagen. Bitte lade die Seite neu.")
        with self.lock:
            self.prune()
            expected = session["csrf"] if session else cookie if cookie in self.preauth else None
            if not expected or not hmac.compare_digest(expected, header):
                raise WebError(403, "Die Sicherheitsprüfung ist abgelaufen. Bitte lade die Seite neu.")

    def limit(self, client):
        self.prune()
        attempts = self.attempts.setdefault(client, [])
        if len(attempts) >= 5:
            raise WebError(429, "Zu viele Anmeldeversuche. Bitte warte zehn Minuten.")
        attempts.append(self.clock())

    def new_session(self, nonce):
        self.preauth.pop(nonce, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"csrf": csrf, "expires": self.clock() + SESSION_SECONDS}
        return token, csrf

    def setup(self, payload, client, nonce):
        with self.lock:
            self.limit(client)
            if self.initialized:
                raise WebError(409, "Die Einrichtung ist bereits abgeschlossen.")
            if set(payload) != {"token", "username", "password", "confirmation"}:
                raise WebError(400, "Bitte fülle alle Felder aus.")
            token = private_read(self.setup_file, 256).decode().strip()
            if not isinstance(payload["token"], str) or not hmac.compare_digest(payload["token"], token):
                raise WebError(401, "Der Einrichtungscode ist ungültig.")
            username, password = payload["username"], payload["password"]
            if not isinstance(username, str) or not 3 <= len(username.strip()) <= 80 or any(ord(c) < 32 for c in username):
                raise WebError(400, "Der Benutzername muss 3 bis 80 Zeichen lang sein.")
            if not isinstance(password, str) or not 12 <= len(password) <= 1024 or password != payload["confirmation"]:
                raise WebError(400, "Das Passwort muss mindestens 12 Zeichen lang sein; beide Eingaben müssen übereinstimmen.")
            salt = secrets.token_bytes(16)
            record = {"version": 1, "username": username.strip().casefold(), "salt": base64.b64encode(salt).decode(), "hash": self.password_hash(password, salt)}
            atomic_private(self.credentials, json_bytes(record) + b"\n")
            self.record = record
            self.setup_file.unlink(missing_ok=True)
            fsync_dir(self.directory)
            self.attempts.pop(client, None)
            return self.new_session(nonce)

    def login(self, payload, client, nonce):
        with self.lock:
            self.limit(client)
            username, password = payload.get("username"), payload.get("password")
            if set(payload) != {"username", "password"} or not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or not 1 <= len(password) <= 1024:
                raise WebError(401, "Benutzername oder Passwort ist falsch.")
            salt = base64.b64decode(self.record["salt"]) if self.record else self.dummy_salt
            actual = self.password_hash(password, salt)
            correct = bool(self.record and hmac.compare_digest(actual, self.record["hash"]) and hmac.compare_digest(username.strip().casefold().encode(), self.record["username"].encode()))
            if not correct:
                raise WebError(401, "Benutzername oder Passwort ist falsch.")
            self.attempts.pop(client, None)
            return self.new_session(nonce)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def allowed_hosts(value):
    hosts = set()
    for item in value.split(","):
        item = item.strip().lower()
        parsed = urlsplit("http://" + item)
        if not item or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("invalid allowed host")
        hosts.add((parsed.hostname, parsed.port))
    return hosts


class AdminHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SyncAdmin"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass  # URLs, headers, bodies and credentials must not enter HTTP logs.

    def host(self):
        headers = self.headers.get_all("Host", [])
        if len(headers) != 1 or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", headers[0]):
            raise WebError(403, "Dieser Servername ist nicht freigegeben.")
        parsed = urlsplit("http://" + headers[0])
        try:
            host, port = parsed.hostname, parsed.port
        except ValueError:
            raise WebError(403, "Dieser Servername ist nicht freigegeben.") from None
        if not any(host == item[0] and (item[1] is None or port == item[1]) for item in self.server.allowed):
            raise WebError(403, "Dieser Servername ist nicht freigegeben.")
        return headers[0]

    def origin(self, host):
        origins = self.headers.get_all("Origin", [])
        expected = ("https://" if self.server.cookie_secure else "http://") + host
        if len(origins) != 1 or not hmac.compare_digest(origins[0].encode(), expected.encode()):
            raise WebError(403, "Die Anfrage muss von dieser Verwaltungsseite kommen.")
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin"}:
            raise WebError(403, "Eine fremde Seite darf keine Änderungen auslösen.")

    def cookies(self):
        headers = self.headers.get_all("Cookie", [])
        if len(headers) > 1:
            raise WebError(400, "Ungültige Sitzung.")
        result = http.cookies.SimpleCookie()
        try:
            result.load(headers[0] if headers else "")
        except http.cookies.CookieError:
            raise WebError(400, "Ungültige Sitzung.") from None
        return {key: item.value for key, item in result.items()}

    def set_cookie(self, name, value, *, http_only=False, clear=False, max_age=None):
        cookie = name + "=" + value + "; Path=/; SameSite=Strict; Max-Age=" + ("0" if clear else str(max_age if max_age is not None else SESSION_SECONDS if name == COOKIE else CSRF_SECONDS))
        if http_only:
            cookie += "; HttpOnly"
        if self.server.cookie_secure:
            cookie += "; Secure"
        return cookie

    def respond(self, code, value, *, content_type="application/json; charset=utf-8", cookies=()):
        raw = value if isinstance(value, bytes) else json_bytes(value)
        self.send_response(code)
        for key, header in (("Content-Type", content_type), ("Content-Length", str(len(raw))), ("Cache-Control", "no-store"), ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"), ("Connection", "close")):
            self.send_header(key, header)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if code == 429:
            self.send_header("Retry-After", "600")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or not 0 < int(lengths[0]) <= BODY_LIMIT:
            raise WebError(413, "Die Anfrage ist zu groß oder unvollständig.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise WebError(415, "Es werden nur JSON-Anfragen akzeptiert.")
        raw = self.rfile.read(int(lengths[0]))
        if len(raw) != int(lengths[0]):
            raise WebError(400, "Die Anfrage ist unvollständig.")
        return parse_json(raw)

    def require_session(self, session):
        if not session:
            raise WebError(401, "Bitte melde dich an.")

    def handle_api(self):
        host = self.host()
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "%" in self.path:
            raise WebError(404, "Die Seite wurde nicht gefunden.")
        path = parsed.path
        if self.command == "GET" and path == "/health":
            return self.respond(200, {"ok": True})
        cookies = self.cookies()
        token = cookies.get(COOKIE, "")
        session = self.server.auth.session(token)
        if self.command == "GET" and path == "/api/session":
            nonce = session["csrf"] if session else self.server.auth.nonce()
            result = {"initialized": self.server.auth.initialized, "authenticated": bool(session)}
            if session:
                result["csrf"] = nonce
            return self.respond(200, result, cookies=[self.set_cookie(CSRF_COOKIE, nonce, max_age=SESSION_SECONDS if session else CSRF_SECONDS)])
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.command == "GET" and path in static:
            filename, content_type = static[path]
            return self.respond(200, (self.server.static / filename).read_bytes(), content_type=content_type)
        if self.command == "GET":
            self.require_session(session)
            getters = {"/api/status": self.server.store.status, "/api/connections": self.server.store.list_connections, "/api/folders": self.server.store.list_folders, "/api/events": self.server.store.events}
            if path in getters:
                return self.respond(200, getters[path]())
            match = re.fullmatch(r"/api/commands/([0-9a-f-]+)", path)
            if match and UUID_RE.fullmatch(match[1]):
                value = self.server.store.command(match[1])
                if value is None:
                    raise WebError(404, "Der Auftrag wurde nicht gefunden.")
                return self.respond(200, value)
            raise WebError(404, "Die Seite wurde nicht gefunden.")
        if self.command != "POST":
            raise WebError(405, "Diese Anfrageart ist nicht erlaubt.")
        self.origin(host)
        csrf_headers = self.headers.get_all("X-CSRF-Token", [])
        self.server.auth.check_csrf(session, cookies.get(CSRF_COOKIE), csrf_headers[0] if len(csrf_headers) == 1 else None)
        payload = self.body()
        if path in {"/api/setup", "/api/login"}:
            method = self.server.auth.setup if path == "/api/setup" else self.server.auth.login
            token, csrf = method(payload, self.client_address[0], cookies.get(CSRF_COOKIE))
            return self.respond(200, {"ok": True}, cookies=[self.set_cookie(COOKIE, token, http_only=True), self.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_SECONDS)])
        self.require_session(session)
        if path == "/api/logout":
            self.server.auth.logout(token)
            return self.respond(200, {"ok": True}, cookies=[self.set_cookie(COOKIE, "", http_only=True, clear=True), self.set_cookie(CSRF_COOKIE, "", clear=True)])
        if path == "/api/commands":
            if set(payload) != {"kind", "payload"} or payload["kind"] not in KINDS or not isinstance(payload["payload"], dict):
                raise WebError(400, "Dieser Auftrag ist nicht gültig.")
            data = payload["payload"]
            if payload["kind"] == "events.clear" and (set(data) != {"confirm"} or data["confirm"] is not True):
                raise WebError(400, "Bitte bestätige das Leeren des gesamten Protokolls.")
            if payload["kind"].startswith(("connection.", "folder.", "provider.")) and (type(data.get("expected_revision")) is not int or data["expected_revision"] < 0):
                raise WebError(400, "Der Verbindungsstand fehlt. Bitte aktualisiere die Seite.")
            if payload["kind"] in {"episode.approve", "episode.status_apply", "review.approve"} and (data.get("confirm") is not True or not isinstance(data.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["digest"]) or not isinstance(data.get("review_id"), str)):
                raise WebError(400, "Bitte bestätige genau die angezeigte Prüfung.")
            result = self.server.store.submit(payload["kind"], data)
            # No input or arbitrary store fields may be echoed by this endpoint.
            command_id = result.get("id") if isinstance(result, dict) else result
            if not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id):
                raise RuntimeError("invalid queued id")
            return self.respond(202, {"id": command_id, "status": "queued"})
        raise WebError(404, "Die Seite wurde nicht gefunden.")

    def handle_request(self):
        try:
            return self.handle_api()
        except WebError as exc:
            return self.respond(exc.code, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            # Store errors may include secrets; only its sanitized command()
            # result is allowed to expose a business error to the frontend.
            return self.respond(503, {"error": "Die Verwaltung ist gerade nicht verfügbar. Bitte versuche es erneut."})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = handle_request


def main():
    parser = argparse.ArgumentParser(description="Private synchronization administration")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    port = int(os.environ.get("ADMIN_PORT", "8790"))
    if args.health:
        from http.client import HTTPConnection
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/health")
            response = connection.getresponse()
            ok = response.status == 200 and response.read(128) == b'{"ok":true}'
            connection.close()
            return 0 if ok else 1
        except Exception:
            return 1
    from admin_store import AdminStore
    root = os.environ.get("ADMIN_STATE_DIR", "/admin-state")
    auth = Auth(root)
    server = http.server.ThreadingHTTPServer((os.environ.get("ADMIN_HOST", "0.0.0.0"), port), AdminHandler)
    server.auth = auth
    server.store = AdminStore(root, os.environ.get("ADMIN_KEY_FILE", "/config/secret.key"))
    server.static = Path(__file__).parent / "admin_static"
    server.cookie_secure = os.environ.get("ADMIN_COOKIE_SECURE", "0") == "1"
    server.allowed = allowed_hosts(os.environ.get("ADMIN_ALLOWED_HOSTS", "127.0.0.1,localhost"))
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Private sync administration listening", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Sync administration could not start; check configured files and permissions", flush=True)
        raise SystemExit(1) from None
