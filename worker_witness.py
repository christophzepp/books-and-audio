"""Completion barrier for the unmodified BookOrbit read-aloud worker.

The HTTP service has no BO credentials and never publishes or edits library files.
Only a live authenticated POST can dispatch work. Background tasks only capture
read-only evidence. A completed response is withheld (503) until its receipt and
archive are fsynced; BookOrbit may then safely remove its temporary workspace.
"""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import uuid
from urllib.parse import urlsplit

import requests

BODY_LIMIT = 4 * 1024**2
RESPONSE_LIMIT = 8 * 1024**2
REPORT_LIMIT = 16 * 1024**2
FILE_LIMIT = 20 * 1024**3
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
STAGES = {"preparing_audio", "downloading_model", "generating_emissions", "marking_up_ebook", "aligning", "validating"}
STOPPING = threading.Event()
KEYS = {"jobId", "epubPath", "audioPaths", "workDir", "language", "granularity", "model", "sourceBytes", "audioDurationSeconds", "maxOutputBytes", "decisions", "resumeFrom"}


class Blocked(Exception):
    """Fixed error code, never raw paths, credentials or upstream error text."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def parse_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise Blocked("duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(Blocked("nonfinite_json")))
    except (UnicodeError, ValueError, TypeError):
        raise Blocked("invalid_json") from None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def positive_int(value, zero=False):
    return type(value) is int and (0 if zero else 1) <= value <= 2**53 - 1


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != dt.timedelta(0):
            raise ValueError()
        return parsed
    except (AttributeError, ValueError, TypeError):
        raise Blocked("invalid_timestamp") from None


def route(method, target):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or "%" in target:
        raise Blocked("unsupported_route")
    if method == "GET" and parsed.path == "/health" and not parsed.query:
        return "health", None, 0
    if method == "POST" and parsed.path == "/v1/jobs" and not parsed.query:
        return "submit", None, 0
    match = re.fullmatch(r"/v1/jobs/([0-9a-f-]+)", parsed.path)
    if not match or not UUID_RE.fullmatch(match[1]) or method not in {"GET", "DELETE"}:
        raise Blocked("unsupported_route")
    after = 0
    if parsed.query:
        query = re.fullmatch(r"afterSeq=(0|[1-9][0-9]{0,15})", parsed.query)
        if method != "GET" or not query or int(query[1]) > 2**53 - 1:
            raise Blocked("unsupported_query")
        after = int(query[1])
    return "status" if method == "GET" else "cancel", match[1], after


def path_allowed(value, roots):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or str(Path(value)) != value:
        raise Blocked("invalid_path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise Blocked("invalid_path")
    if not any(value.startswith(root.rstrip("/") + "/") for root in roots):
        raise Blocked("path_outside_roots")
    return value


@contextlib.contextmanager
def safe_file(path):
    """Walk every component without following symlinks (including ancestors)."""
    parts = Path(path).parts
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    handle = None
    try:
        for part in parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise Blocked("not_regular_file")
        yield handle
    finally:
        if handle is not None:
            os.close(handle)
        os.close(directory)


def identity(path, info):
    return {"path": path, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "dev": info.st_dev, "ino": info.st_ino}


def file_stat(path):
    with safe_file(path) as handle:
        return identity(path, os.fstat(handle))


def read_file(path, limit, *, hashed=False, content=False):
    with safe_file(path) as handle:
        before = identity(path, os.fstat(handle))
        if not 0 < before["size"] <= limit:
            raise Blocked("file_size_limit")
        result = hashlib.sha256()
        chunks = []
        total = 0
        while True:
            if STOPPING.is_set():
                raise Blocked("witness_stopping")
            chunk = os.read(handle, min(1024**2, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > before["size"] or total > limit:
                raise Blocked("file_size_changed")
            if hashed:
                result.update(chunk)
            if content:
                chunks.append(chunk)
        if total != before["size"] or identity(path, os.fstat(handle)) != before or file_stat(path) != before:
            raise Blocked("file_changed_during_capture")
    if hashed:
        before["sha256"] = result.hexdigest()
    return before, b"".join(chunks) if content else None


def atomic(path, raw, *, immutable=False):
    """File and directory fsync; immutable archives never replace old evidence."""
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise Blocked("immutable_evidence_conflict")
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def validate_request(body, roots, work_root):
    if not isinstance(body, dict) or set(body) != KEYS:
        raise Blocked("request_protocol_mismatch")
    if not isinstance(body["jobId"], str) or not UUID_RE.fullmatch(body["jobId"]):
        raise Blocked("invalid_job_id")
    path_allowed(body["epubPath"], roots)
    audio = body["audioPaths"]
    if not isinstance(audio, list) or not 1 <= len(audio) <= 2000 or any(not isinstance(path, str) for path in audio) or len(set(audio)) != len(audio):
        raise Blocked("invalid_audio_paths")
    for path in audio:
        path_allowed(path, roots)
    if body["workDir"] != work_root.rstrip("/") + "/" + body["jobId"]:
        raise Blocked("invalid_work_directory")
    if not isinstance(body["language"], str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", body["language"]):
        raise Blocked("invalid_language")
    if body["granularity"] not in {"sentence", "word"} or body["model"] != "quartznet15x5":
        raise Blocked("unsupported_alignment")
    if not positive_int(body["sourceBytes"], zero=True) or not positive_int(body["maxOutputBytes"]) or body["maxOutputBytes"] > FILE_LIMIT:
        raise Blocked("invalid_size_limit")
    duration = body["audioDurationSeconds"]
    if duration is not None and (type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0):
        raise Blocked("invalid_duration")
    if body["resumeFrom"] is not None and body["resumeFrom"] not in STAGES:
        raise Blocked("invalid_resume_stage")
    decisions = body["decisions"]
    if not isinstance(decisions, dict) or set(decisions) != {"chapters", "audio"} or not isinstance(decisions["chapters"], dict) or not isinstance(decisions["audio"], list):
        raise Blocked("invalid_decisions")
    if len(decisions["chapters"]) > 10000 or len(decisions["audio"]) > 1000:
        raise Blocked("decision_limit")
    if any(value not in {"excluded", "included", "approved"} for value in decisions["chapters"].values()):
        raise Blocked("invalid_chapter_decision")
    for entry in decisions["audio"]:
        if not isinstance(entry, dict) or set(entry) != {"track", "start", "end"} or not isinstance(entry["track"], str):
            raise Blocked("invalid_audio_decision")
        for key in ("start", "end"):
            value = entry[key]
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
                raise Blocked("invalid_audio_decision")
    return body


def capture_input(request):
    source, _ = read_file(request["epubPath"], FILE_LIMIT, hashed=True)
    audio = [file_stat(path) for path in request["audioPaths"]]
    if any(not 0 < item["size"] <= FILE_LIMIT for item in audio):
        raise Blocked("invalid_audio_size")
    if source["size"] + sum(item["size"] for item in audio) != request["sourceBytes"]:
        raise Blocked("source_bytes_mismatch")
    value = {"source": source, "audio": audio, "input_captured_at": now()}
    check_input(value)
    return value


def check_input(value):
    for expected in [value["source"], *value["audio"]]:
        if file_stat(expected["path"]) != {k: v for k, v in expected.items() if k != "sha256"}:
            raise Blocked("input_identity_changed")


def worker_source(item):
    return {"path": item["path"], "size": item["size"], "mtimeMs": item["mtime_ns"] // 1000000}


def marker_inputs(request, inputs):
    audio = [worker_source(item) for item in inputs["audio"]]
    epub = worker_source(inputs["source"])
    # Official Intl.Locale canonicalizes language/script/region casing.
    pieces = request["language"].split("-")
    locale = "-".join([pieces[0].lower()] + [p.title() if len(p) == 4 else p.upper() if len(p) in (2, 3) else p.lower() for p in pieces[1:]])
    return {
        "preparing_audio": {"v": 1, "audio": audio},
        "marking_up_ebook": {"v": 1, "epub": epub, "granularity": request["granularity"], "locale": locale},
        "aligning": {"v": 1, "audio": audio, "epub": epub, "model": request["model"], "locale": locale, "granularity": request["granularity"], "decisions": request["decisions"]},
    }


def marker_canonical(value):
    """Node canonicalJson for finite JSON marker values (including integral floats)."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + marker_canonical(value[key]) for key in keys) + "}"
    if isinstance(value, list):
        return "[" + ",".join(marker_canonical(item) for item in value) + "]"
    if type(value) is float:
        if not math.isfinite(value):
            raise Blocked("nonfinite_marker")
        if value == 0:
            return "0"
        if 1e-6 <= abs(value) < 1e21:
            return format(decimal.Decimal(repr(value)), "f").rstrip("0").rstrip(".") if "." in format(decimal.Decimal(repr(value)), "f") else format(decimal.Decimal(repr(value)), "f")
        mantissa, exponent = repr(value).lower().split("e")
        mantissa = mantissa.removesuffix(".0")
        exponent = int(exponent)
        return mantissa + "e" + ("+" if exponent >= 0 else "-") + str(abs(exponent))
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def capture_completion(state, worker):
    request, inputs = state["request"], state["input"]
    check_input(inputs)
    source, _ = read_file(request["epubPath"], FILE_LIMIT, hashed=True)
    if source != inputs["source"]:
        raise Blocked("source_bytes_changed")
    output, _ = read_file(request["workDir"] + "/output.epub", min(FILE_LIMIT, request["maxOutputBytes"]), hashed=True)
    if output["size"] != worker["outputBytes"]:
        raise Blocked("output_bytes_mismatch")
    report, raw = read_file(request["workDir"] + "/report.json", REPORT_LIMIT, hashed=True, content=True)
    if not isinstance(parse_json(raw), dict):
        raise Blocked("invalid_alignment_report")
    markers = {}
    for stage, expected in marker_inputs(request, inputs).items():
        _, raw = read_file(request["workDir"] + "/.done-" + stage, BODY_LIMIT, content=True)
        marker = parse_json(raw)
        if not isinstance(marker, dict) or marker.get("stage") != stage or marker.get("inputs") != expected or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("fingerprint", ""))):
            raise Blocked("stage_marker_mismatch")
        if digest(marker_canonical(marker["inputs"]).encode("utf-8")) != marker["fingerprint"]:
            raise Blocked("stage_marker_fingerprint_mismatch")
        if timestamp(marker.get("finishedAt")) > timestamp(now()):
            raise Blocked("future_stage_marker")
        markers[stage] = marker
    check_input(inputs)
    for expected in (output, report):
        if file_stat(expected["path"]) != {k: v for k, v in expected.items() if k != "sha256"}:
            raise Blocked("output_identity_changed")
    return {"schema": 1, "kind": "official-worker-completion", "job_id": request["jobId"], "generation_id": state["generation_id"], "request_sha256": state["request_sha256"], "request": request, "input_captured_at": inputs["input_captured_at"], "completed_witness_at": now(), "source": source, "audio": inputs["audio"], "output": output, "report": {"sha256": report["sha256"], "size": report["size"]}, "markers": markers, "worker": {"job_id": request["jobId"], "status": "completed", "output_bytes": worker["outputBytes"], "accepted": state["accepted"]}}


class Upstream:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise Blocked("invalid_upstream_url")
        self.url, self.token = url.rstrip("/"), token

    def call(self, method, path, body=None):
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, self.url + path, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"}, data=None if body is None else canonical(body), timeout=(5, 15), allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400 or response.headers.get("Content-Encoding", "identity") != "identity":
                        raise Blocked("invalid_upstream_response")
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > RESPONSE_LIMIT:
                            raise Blocked("upstream_response_limit")
                    return response.status_code, parse_json(bytes(raw))
        except requests.RequestException:
            raise Blocked("upstream_unreachable") from None


class Witness:
    def __init__(self, state_dir, roots, work_root, upstream, executor=None):
        self.root = Path(state_dir)
        self.roots = tuple(str(Path(root)) for root in roots)
        self.work_root = str(Path(work_root))
        self.upstream = upstream
        self.executor = executor or concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.locks, self.futures, self.lock = {}, {}, threading.RLock()
        for path in (self.root, self.root / "jobs", self.root / "receipts", self.root / "receipts" / "archive"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
                raise Blocked("unsafe_state_directory")
            for directory_path in (path, path.parent):
                directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def job_lock(self, job):
        with self.lock:
            return self.locks.setdefault(job, threading.RLock())

    def load(self, job):
        path = self.root / "jobs" / (job + ".json")
        if not path.exists():
            return None
        _, raw = read_file(str(path), 32 * 1024**2, content=True)
        return parse_json(raw)

    def save(self, state):
        atomic(self.root / "jobs" / (state["request"]["jobId"] + ".json"), canonical(state) + b"\n")

    def schedule(self, state, stage, worker=None):
        job, generation = state["request"]["jobId"], state["generation_id"]
        key = job, generation, stage
        with self.lock:
            future = self.futures.get(key)
            if future and not future.done():
                return
            def run():
                try:
                    result = capture_input(state["request"]) if stage == "input" else capture_completion(state, worker)
                    if STOPPING.is_set():
                        return
                    with self.job_lock(job):
                        current = self.load(job)
                        if not current or current["generation_id"] != generation or current["phase"] != stage + "_pending":
                            return
                        if stage == "input":
                            current["input"] = result
                            current["phase"] = "input_ready"
                        else:
                            raw = canonical(result) + b"\n"
                            sha = digest(raw)
                            atomic(self.root / "receipts" / "archive" / (job + "-" + sha + ".json"), raw, immutable=True)
                            atomic(self.root / "receipts" / (job + ".json"), raw)
                            current["receipt_sha256"] = sha
                            current["phase"] = "complete"
                        self.save(current)
                except Exception as exc:
                    if STOPPING.is_set():
                        return  # retain pending state for a clean restart
                    with self.job_lock(job):
                        current = self.load(job)
                        if current and current["generation_id"] == generation and current["phase"] == stage + "_pending":
                            current["phase"] = "blocked"
                            current["error"] = str(exc) if isinstance(exc, Blocked) and re.fullmatch("[a-z_]+", str(exc)) else "capture_failed"
                            self.save(current)
                            print(canonical({"event": "read_aloud_witness_blocked", "job_id": job, "error_code": current["error"], "at": now()}).decode("utf-8"), flush=True)
            self.futures[key] = self.executor.submit(run)

    def begin(self, request, previous_worker):
        events = previous_worker.get("events", []) if isinstance(previous_worker, dict) else []
        sequence = max([e.get("seq", 0) for e in events if isinstance(e, dict) and positive_int(e.get("seq"))] or [0])
        state = {"generation_id": str(uuid.uuid4()), "request": request, "request_sha256": digest(canonical(request)), "phase": "input_pending", "prior_seq": sequence, "released": False}
        self.save(state)
        self.schedule(state, "input")
        return self.pending()

    @staticmethod
    def pending(code="witness_pending"):
        return 503, {"errorCode": code, "error": "Read-aloud evidence is not yet ready; retry the request."}

    def observe(self, state, worker):
        if not isinstance(worker, dict) or worker.get("jobId") != state["request"]["jobId"] or worker.get("status") not in {"queued", "running", "completed", "failed", "cancelled"} or not isinstance(worker.get("events"), list):
            raise Blocked("invalid_worker_job")
        accepted_events = [event for event in worker["events"] if isinstance(event, dict) and event.get("code") == "job_accepted" and positive_int(event.get("seq"))]
        accepted = max(accepted_events, key=lambda item: item["seq"]) if accepted_events else None
        if not state.get("accepted"):
            if not accepted or accepted["seq"] <= state["prior_seq"] or not positive_int((accepted.get("data") or {}).get("attempt")):
                raise Blocked("missing_attempt_evidence")
            timestamp(accepted.get("at"))
            state["accepted"] = {"seq": accepted["seq"], "at": accepted["at"], "attempt": accepted["data"]["attempt"]}
            self.save(state)
        elif accepted and accepted["seq"] != state["accepted"]["seq"]:
            raise Blocked("worker_attempt_changed")
        if worker["status"] == "completed":
            if not positive_int(worker.get("outputBytes")):
                raise Blocked("invalid_output_size")
            if state["phase"] == "complete":
                path = self.root / "receipts" / (state["request"]["jobId"] + ".json")
                _, raw = read_file(str(path), 32 * 1024**2, content=True)
                receipt = parse_json(raw)
                if digest(raw) != state["receipt_sha256"] or receipt["generation_id"] != state["generation_id"] or receipt["worker"]["accepted"] != state["accepted"] or receipt["worker"]["output_bytes"] != worker["outputBytes"]:
                    raise Blocked("receipt_binding_mismatch")
                state["released"] = True
                self.save(state)
                return 200, worker
            if state["phase"] not in {"dispatching", "active", "completion_pending"}:
                return self.pending("witness_blocked")
            state["phase"] = "completion_pending"
            self.save(state)
            self.schedule(state, "completion", worker)
            return self.pending()
        if state["phase"] in {"completion_pending", "complete"}:
            raise Blocked("worker_completion_regressed")
        state["phase"] = "terminal" if worker["status"] in {"failed", "cancelled"} else "active"
        state["released"] = state["phase"] == "terminal"
        self.save(state)
        return 200, worker

    def post(self, request):
        validate_request(request, self.roots, self.work_root)
        job = request["jobId"]
        with self.job_lock(job):
            state = self.load(job)
            request_sha = digest(canonical(request))
            if state and state["phase"] == "blocked":
                return self.pending("witness_blocked")
            # New POST after a released terminal state deliberately means a new
            # official attempt, even with exactly the same UUID and request.
            fresh = not state or state.get("released") or state["phase"] == "cancelled"
            if state and state["request_sha256"] != request_sha and not fresh:
                return self.pending("request_changed_while_active")
            if fresh:
                code, previous = self.upstream.call("GET", "/v1/jobs/" + job)
                if code == 200 and previous.get("status") not in {"completed", "failed", "cancelled"}:
                    return self.pending("unknown_running_attempt")
                if code not in {200, 404}:
                    return code, previous
                return self.begin(request, previous if code == 200 else None)
            phase = state["phase"]
            if phase == "input_pending":
                self.schedule(state, "input")
                return self.pending()
            if phase == "input_ready":
                check_input(state["input"])
                code, previous = self.upstream.call("GET", "/v1/jobs/" + job)
                if code == 200 and previous.get("status") not in {"completed", "failed", "cancelled"}:
                    return self.pending("unknown_running_attempt")
                if code not in {200, 404}:
                    return code, previous
                state["phase"] = "dispatching"
                self.save(state)  # fsynced BEFORE the only mutating upstream call
                code, worker = self.upstream.call("POST", "/v1/jobs", request)
                if code not in {200, 202}:
                    return code, worker
                outcode, body = self.observe(state, worker)
                return code if outcode == 200 else outcode, body
            code, worker = self.upstream.call("GET", "/v1/jobs/" + job)
            if code == 404:
                # Official worker may restart and forget a job. A fresh source
                # capture must precede a new POST; this call does not dispatch.
                return self.begin(request, None)
            if code != 200:
                return code, worker
            if state["phase"] == "dispatching" and not state.get("accepted") and worker.get("status") in {"completed", "failed", "cancelled"}:
                sequences = [event.get("seq", 0) for event in worker.get("events", []) if isinstance(event, dict) and positive_int(event.get("seq"))]
                if sequences and max(sequences) <= state["prior_seq"]:
                    # POST transport failed before acceptance: the authoritative
                    # worker still shows the preceding terminal attempt. Only
                    # this fresh caller request may prepare another dispatch.
                    return self.begin(request, worker)
            return self.observe(state, worker)

    def get(self, job, after=0):
        with self.job_lock(job):
            code, worker = self.upstream.call("GET", "/v1/jobs/" + job)
            if code != 200:
                return code, worker
            state = self.load(job)
            if not state or state["phase"] in {"input_pending", "input_ready", "blocked", "cancelled"}:
                if worker.get("status") == "completed":
                    return self.pending("missing_pre_dispatch_evidence")
                return code, worker
            code, worker = self.observe(state, worker)
            if code == 200:
                worker = dict(worker, events=[event for event in worker["events"] if event.get("seq", 0) > after])
            return code, worker

    def health(self):
        if STOPPING.is_set():
            return self.pending("witness_stopping")
        for path in (self.root, self.root / "jobs", self.root / "receipts", self.root / "receipts" / "archive"):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return self.pending("unsafe_state_directory")
        space = os.statvfs(self.root)
        if space.f_bavail * space.f_frsize < 1024**2:
            return self.pending("witness_storage_low")
        return self.upstream.call("GET", "/health")

    def cancel(self, job):
        # Never waits for a file hash. Serializing with dispatch prevents DELETE
        # racing just before POST and inadvertently allowing work after cancel.
        with self.job_lock(job):
            code, worker = self.upstream.call("DELETE", "/v1/jobs/" + job)
            state = self.load(job)
            if state:
                state["phase"] = "cancelled"
                self.save(state)
            if code == 200 and isinstance(worker, dict) and worker.get("status") == "completed":
                return self.pending("cancelled_completion_withheld")
            return code, worker


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, code, body):
        raw = canonical(body)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        if code == 503:
            self.send_header("Retry-After", "5")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def handle_request(self):
        try:
            if STOPPING.is_set():
                return self.reply(*Witness.pending("witness_stopping"))
            authorization = self.headers.get_all("Authorization", [])
            expected = "Bearer " + self.server.token
            if len(authorization) != 1 or not hmac.compare_digest(authorization[0].encode(), expected.encode()):
                return self.reply(401, {"errorCode": "unauthorized", "error": "Authentication required."})
            action, job, after = route(self.command, self.path)
            if self.headers.get("Transfer-Encoding"):
                raise Blocked("unsupported_transfer_encoding")
            lengths = self.headers.get_all("Content-Length", [])
            if action == "submit":
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]) or not 0 < int(lengths[0]) <= BODY_LIMIT:
                    raise Blocked("request_body_limit")
                self.connection.settimeout(15)
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise Blocked("incomplete_request")
                result = self.server.witness.post(parse_json(raw))
            elif action == "health":
                result = self.server.witness.health()
            elif action == "cancel":
                result = self.server.witness.cancel(job)
            else:
                result = self.server.witness.get(job, after)
            return self.reply(*result)
        except Blocked as exc:
            return self.reply(503, {"errorCode": str(exc), "error": "Read-aloud evidence check requires retry or review."})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            return self.reply(503, {"errorCode": "witness_unavailable", "error": "Read-aloud evidence is temporarily unavailable."})

    do_GET = do_POST = do_DELETE = handle_request


def main():
    parser = argparse.ArgumentParser(description="Official read-aloud worker completion witness")
    parser.add_argument("--health", action="store_true", help="Check authenticated local proxy and upstream health without displaying credentials")
    args = parser.parse_args()
    state = os.environ.get("WITNESS_STATE", "/witness")
    token_path = os.environ.get("WITNESS_TOKEN_FILE", "/run/secrets/read-aloud-worker-token")
    _, raw = read_file(token_path, 16384, content=True)
    token = raw.decode("utf-8").strip()
    if not token or any(c.isspace() for c in token):
        raise Blocked("invalid_token_file")
    if args.health:
        try:
            code, body = Upstream("http://127.0.0.1:" + os.environ.get("WITNESS_PORT", "8788"), token).call("GET", "/health")
            return 0 if code == 200 and body.get("status") == "ok" else 1
        except Exception:
            return 1
    upstream = Upstream(os.environ.get("WITNESS_UPSTREAM_URL", "http://read-aloud-worker:8787"), token)
    witness = Witness(state, os.environ.get("WITNESS_LIBRARY_ROOTS", "/books,/audiobooks").split(","), os.environ.get("WITNESS_WORK_ROOT", "/data/read-aloud/jobs"), upstream)
    lock = os.open(Path(state) / "proxy.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = http.server.ThreadingHTTPServer((os.environ.get("WITNESS_HOST", "0.0.0.0"), int(os.environ.get("WITNESS_PORT", "8788"))), Handler)
    server.witness, server.token = witness, token
    atomic(Path(state) / "health-started.json", canonical({"schema": 1, "started_at": now()}) + b"\n")
    def stop(*_):
        STOPPING.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Read-aloud completion witness listening", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        witness.executor.shutdown(wait=True)
        os.close(lock)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Read-aloud witness could not start; check configured mounts and permissions", flush=True)
        raise SystemExit(1) from None
