"""Dedicated radio-drama writer. Progress workers keep read-only media mounts."""
import argparse
import base64
import fcntl
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from admin_store import atomic
from series_imports import ARR, ImportQueue, exclude_from_libraries, from_event, publish, publish_arr
import series_listenarr as native

STATE = Path(os.environ.get('SERIES_STATE', '/series-state'))
SETTINGS = Path(os.environ.get('SERIES_SETTINGS', '/series-control/settings.json'))


def options():
    try:
        config = json.loads(SETTINGS.read_text())
    except FileNotFoundError:
        return False, 3600
    enabled = config.get('enabled')
    seconds = config.get('interval_seconds')
    if type(enabled) is not bool or type(seconds) is not int or not 300 <= seconds <= 604800:
        raise ValueError('Ungültige Hörspiel-Einstellungen')
    return enabled, seconds


def retry_signal(conflicts):
    if not conflicts:
        return None
    from listenarr_conflicts import read
    from maintenance_settings import podcast_window
    config = read(SETTINGS.parent / 'maintenance-window.json')
    active, day = podcast_window(config, time.time())
    return (read(SETTINGS.parent / 'listenarr-retries.json'), day if active else None)


def run(archive, apply):
    result = dict(status='completed', moved=0, skipped=0, conflicts=0, empty_dirs_removed=0)
    errors = []
    if os.environ.get('LISTENARR_ORIGIN'):
        try:
            api = native.Client(os.environ['LISTENARR_ORIGIN'])
            api.login()
            try:
                result.update(native.run(api, apply))
            finally:
                if api.csrf:
                    api.call('account/logout', {})
        except (OSError, KeyError, TypeError, ValueError, RuntimeError, native.http.client.HTTPException) as error:
            errors.append('Listenarr: ' + error.__class__.__name__)
    if os.environ.get('SERIES_RMAB_ORIGIN'):
        try:
            from series_readmeabook import completed_imports
            for job in completed_imports(archive.known):
                if apply:
                    archive.enqueue(job)
        except Exception as error:
            errors.append('ReadMeABook: ' + error.__class__.__name__)
    pending = archive.queued()
    if pending and apply:
        try:
            with native.maintenance_guard():
                root = None
                try:
                    for key, job in pending:
                        try:
                            is_arr_event = job.get('kind') == 'arr-event'
                            publication = archive.publication(key)
                            pending_cleanup = publication and publication[1] == 2
                            if is_arr_event and not pending_cleanup:
                                from series_arr import lookup
                                try:
                                    resolved = lookup(job)
                                except (OSError, KeyError, RuntimeError) as error:
                                    errors.append(job['source'] + ': ' + type(error).__name__)
                                    continue
                            from media_activity import record
                            if pending_cleanup:
                                parts = publication[0].get('target')
                                target = parts[-1] if isinstance(parts, list) and parts else ''
                            else:
                                target = str(resolved[1] if is_arr_event else job.get('target', '')).rsplit('/', 1)[-1]
                            record('series', target or job.get('source'), 'series_import')
                            if root is None:
                                try:
                                    root = os.open(os.environ.get('SERIES_DRAMA_MOUNT', '/audio-dramas'),
                                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                                except OSError as error:
                                    errors.append('Hörspielordner: ' + type(error).__name__)
                                    break
                            if pending_cleanup:
                                from series_publication import finish_committed
                                finish_committed(root, archive, key, publication[0])
                                moved = False
                            elif is_arr_event:
                                moved = publish_arr(root, key, job, resolved, archive)
                            else:
                                moved = publish(root, key, job, archive)
                        except (OSError, ValueError, native.Defer) as error:
                            archive.mark(key, 'conflict', str(error)[:240])
                            continue
                        archive.mark(key, 'done')
                        result['moved' if moved else 'skipped'] += 1
                finally:
                    if root is not None:
                        os.close(root)
        except native.Defer:
            result['status'] = 'waiting'
    result['conflicts'] += archive.conflicts()
    if errors:
        result.update(status='error', message=', '.join(errors))
    return result


def receive(queue, key):
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def do_POST(self):
            source = self.path.removeprefix('/imports/')
            if self.path != '/imports/' + source or source not in ARR | {'shelfmark'}:
                self.send_error(404)
                return
            auth = self.headers.get('Authorization', '')
            expected_basic = base64.b64encode((source + ':' + key).encode()).decode()
            if not any(hmac.compare_digest(auth.encode(), expected.encode()) for expected in
                       ('Bearer ' + key, 'Basic ' + expected_basic)):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Books and Audio imports", charset="UTF-8"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    self.send_error(413)
                    return
                event = json.loads(self.rfile.read(size))
                job = from_event(source, event)
                if job:
                    queue.enqueue(job)
            except (ValueError, KeyError, TypeError, AttributeError):
                self.send_error(400)
                return
            self.send_response(202 if job else 204)
            self.end_headers()
    return Receiver


def serve_imports(queue):
    path = os.environ.get('SERIES_IMPORT_KEY_FILE')
    if not path:
        return None
    key = Path(path).read_text().strip()
    if len(key) < 24 or not key.isascii() or any(not 33 <= ord(ch) <= 126 for ch in key):
        raise ValueError('Unbrauchbarer Import-Schlüssel')
    http = ThreadingHTTPServer((os.getenv('SERIES_IMPORT_BIND', '0.0.0.0'), 8793), receive(queue, key))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return http


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--loop', action='store_true')
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--retry', metavar='IMPORT_ID')
    mode.add_argument('--list-conflicts', action='store_true')
    mode.add_argument('--prepare-imports', action='store_true', help='Set up private source roots and scanner exclusions')
    args = parser.parse_args()
    os.umask(0o022)  # Published episode directories must remain readable to media servers.
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    queue = ImportQueue(STATE)
    if args.prepare_imports:
        from series_files import open_dir
        from series_imports import PRODUCERS
        with native.maintenance_guard():
            fd = os.open(os.environ.get('SERIES_DRAMA_MOUNT', '/audio-dramas'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                exclude_from_libraries(fd)
                for source in sorted(PRODUCERS):
                    with open_dir(fd, ['.series-import', source], create=True):
                        pass
            finally:
                os.close(fd)
        return
    if args.list_conflicts:
        with queue.connect() as db:
            for key, source, reason in db.execute("SELECT id,json_extract(receipt,'$.source'),reason FROM imports WHERE state='conflict' ORDER BY rowid"):
                print(json.dumps(dict(id=key, source=source, reason=reason), ensure_ascii=False))
        return
    if args.retry:
        with queue.connect() as db:
            n = db.execute("UPDATE imports SET state='queued',reason=NULL WHERE id=? AND state='conflict'", (args.retry,)).rowcount
        if n != 1:
            parser.error('Kein zurückgestellter Import mit dieser Kennung')
        return
    if not args.loop:
        summary = run(queue, False)
        summary['would_move'] = summary.pop('moved')
        print(json.dumps(summary, ensure_ascii=False))
        if summary['status'] == 'error':
            raise SystemExit(1)
        return
    with (STATE / 'organizer.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        http = serve_imports(queue)
        last = None
        state = None
        held = False
        signal = None
        try:
            while True:
                try:
                    enabled, interval = options()
                    current_signal = retry_signal(held) if enabled else None
                    force = bool(current_signal and (current_signal[0] != signal[0]
                                 or current_signal[1] and current_signal[1] != signal[1]))
                    if not enabled:
                        if state != 'disabled':
                            atomic(STATE / 'status.json', dict(status='disabled', enabled=False, checked_at=time.time()))
                        state, last = 'disabled', None
                    elif last is None or force or time.monotonic() - last >= interval:
                        before = current_signal or retry_signal(True)
                        # Also consume this wakeup on failure. Provider outages
                        # must use the normal retry interval.
                        if held:
                            signal = before
                        from media_activity import recording, count, spool_series
                        with recording() as activity:
                            count('series_runs')
                            try:
                                result = run(queue, True)
                            finally:
                                spool_series(STATE, activity)
                        held = bool(result.get('listenarr_conflicts'))
                        signal = before if held else None
                        state = result['status']
                        atomic(STATE / 'status.json', dict(result, enabled=True, checked_at=time.time()))
                        last = time.monotonic()
                except Exception as error:
                    # Public status contains no provider response, credential or path.
                    if state != 'error':
                        atomic(STATE / 'status.json', dict(status='error', message=type(error).__name__, checked_at=time.time()))
                    state = 'error'
                    last = time.monotonic()
                time.sleep(10)  # Poll only the NVMe GUI settings, never the media root.
        finally:
            if http:
                http.shutdown()
                http.server_close()


if __name__ == '__main__':
    main()
