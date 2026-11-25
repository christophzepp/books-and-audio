"""Optional private, incremental PinePods -> stock Floppy GPodder relay.

No playback writes, new passwords, public listener or Floppy source modifications.
The existing Basic credentials are verified by the pinned upstream on every poll.
A response remains replayable until Floppy acknowledges its cursor on a later poll.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests
from tracking_delivery import ensure_delivery_schema, record_batch
import tracking_outbox

LIMIT = 32 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def event_time(action):
    try:
        dt = datetime.fromisoformat(action['timestamp'].replace('Z', '+00:00'))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('invalid_event_time') from None


class Relay:
    def __init__(self, config, root, request=requests.request, *, source=None, resolver=None, clock=time.time):
        self.config, self.request = config, request
        self.clock = clock
        self.native_source = source
        self.resolver = resolver
        upstream = urlsplit(config['upstream'])
        if (upstream.scheme not in ('http', 'https') or not upstream.hostname
                or upstream.username or upstream.password or upstream.query or upstream.fragment):
            raise ValueError('invalid_upstream')
        from pinepods_client import _private_host
        if upstream.scheme == 'http' and not _private_host(upstream.hostname):
            raise ValueError('public_upstream_requires_https')
        self.upstream = config['upstream'].rstrip('/')
        self.username = config['username']
        if not isinstance(self.username, str) or not self.username or len(self.username) > 255:
            raise ValueError('invalid_username')
        baseline = config['baseline']
        if type(baseline) is not int or not 0 < baseline <= int(time.time()):
            raise ValueError('explicit_baseline_required')
        self.root = Path(root); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS state(id TEXT PRIMARY KEY,value TEXT NOT NULL);'
                             'CREATE TABLE IF NOT EXISTS seen(hash TEXT PRIMARY KEY);')
            scope = encoded([self.upstream, self.username, baseline, config.get('exclude_devices', [])])
            old = self.get(db, 'scope')
            if old is not None and old != scope:
                raise ValueError('relay_scope_changed')
            if old is None:
                self.set(db, 'scope', scope); self.set(db, 'ack', baseline)
                self.set(db, 'pending', None)
            self.set(db, 'delivery_mode', 'native-state' if config.get('native_source') is not None else 'legacy-actions')
            if config.get('native_source') is not None:
                from tracking_projection import NativeProjection
                NativeProjection.ensure_schema(db)
                ensure_delivery_schema(db)
                pending = self.get(db, 'pending')
                if pending:
                    record_batch(db, pending, offered_at=self.get(db, 'last_poll'))
                db.execute('CREATE TABLE IF NOT EXISTS native_tracking_superseded '
                           '(event_id TEXT PRIMARY KEY, successor TEXT NOT NULL)')
                tracking_outbox.ensure_schema(db, pending, self.get(db, 'native_blocked'), self.get(db, 'last_poll'))
                native = config['native_source']
                if not isinstance(native, dict): raise ValueError('invalid_native_tracking_source')
                native_scope = encoded([self.upstream, self.username, native.get('user_id'),
                                        native.get('server_version', '0.9.0')])
                previous_scope = self.get(db, 'native_scope')
                if previous_scope is not None and previous_scope != native_scope:
                    raise ValueError('native_tracking_scope_changed')
                self.set(db, 'native_scope', native_scope)
                if self.native_source is None:
                    from tracking_source import NativeTrackingSource
                    self.native_source = NativeTrackingSource(dict(native, upstream=self.upstream,
                        username=self.username, baseline=baseline))
                if self.resolver is None:
                    from tracking_rss import TrackingResolver
                    self.resolver = TrackingResolver(config.get('aliases', []))

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.root / 'relay.sqlite', timeout=30)) as connection:
            with connection:
                yield connection

    @staticmethod
    def get(db, key):
        row = db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def set(db, key, value):
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, encoded(value)))

    def upstream_request(self, method, path, authorization, **kwargs):
        response = self.request(method, self.upstream + path,
                                headers={'Authorization': authorization, 'User-Agent': 'Books-Audio-Tracking/1'},
                                timeout=(10, 45), allow_redirects=False, **kwargs)
        if response.status_code not in (200, 201, 204):
            raise PermissionError('upstream_auth') if response.status_code in (401, 403) else ValueError('upstream_unavailable')
        if len(response.content) > LIMIT:
            raise ValueError('upstream_too_large')
        return response

    def authenticate(self, authorization):
        try:
            scheme, data = authorization.split(' ', 1)
            username, password = base64.b64decode(data, validate=True).decode().split(':', 1)
            if scheme != 'Basic' or username != self.username or not password:
                raise ValueError()
        except (ValueError, UnicodeError, AttributeError):
            raise PermissionError('authentication_required') from None
        self.upstream_request('POST', '/api/2/auth/' + quote(self.username, safe='') + '/login.json', authorization)

    def actions(self, since, device, authorization):
        with self.lock, self.db() as db:
            ack, pending = self.get(db, 'ack'), self.get(db, 'pending')
            if since is not None and (type(since) is not int or since < 0):
                raise ValueError('invalid_cursor')
            if self.native_source is not None and device and device != 'books-audio-native':
                raise ValueError('native_tracking_device_filter_unsupported')
            # Only a cursor we actually issued can acknowledge a previous batch.
            if pending and since == pending['timestamp']:
                if self.native_source is not None:
                    record_batch(db, pending, acknowledged_at=int(self.clock()))
                for fingerprint in pending['fingerprints']:
                    db.execute('INSERT OR IGNORE INTO seen VALUES(?)', (fingerprint,))
                ack = pending['timestamp']; self.set(db, 'ack', ack)
                pending = None; self.set(db, 'pending', None)
                db.commit()  # Keep acknowledgement if the next upstream poll fails.
            elif since is not None and since > ack:
                raise ValueError('unknown_cursor')
            if pending:
                return {'actions': pending['actions'], 'timestamp': pending['timestamp']}
            if self.native_source is not None:
                return self.native_actions(db, ack, authorization)
            params = {'since': max(self.config['baseline'], ack - 1), 'aggregated': 'false'}
            if device: params['device'] = device
            response = self.upstream_request('GET', '/api/2/episodes/' + quote(self.username, safe='') + '.json', authorization, params=params)
            payload = response.json()
            cursor = payload.get('timestamp')
            rows = payload.get('actions')
            if (type(cursor) is not int or cursor < ack or cursor > time.time() + 300
                    or not isinstance(rows, list) or len(rows) > 100000):
                raise ValueError('invalid_upstream_batch')
            output, fingerprints, batch_seen = [], [], set()
            aliases = {(a['feed'], a['old']): a['new'] for a in self.config.get('aliases', [])}
            excluded = set(self.config.get('exclude_devices', []))
            for action in rows:
                if not isinstance(action, dict): raise ValueError('invalid_upstream_action')
                stamp = event_time(action)
                if stamp < self.config['baseline'] or stamp > cursor + 1: continue
                if action.get('device') in excluded or action.get('action') != 'play': continue
                if any(type(action.get(k)) not in (int, float) or not math.isfinite(action[k]) or action[k] < 0 for k in ('position', 'total')):
                    raise ValueError('invalid_upstream_position')
                if action['position'] <= 0: continue
                if not all(isinstance(action.get(k), str) and action[k] for k in ('episode', 'podcast')):
                    raise ValueError('invalid_upstream_identity')
                canonical = aliases.get((action['podcast'], action['episode']), action['episode'])
                identity = [action['podcast'], canonical, stamp, action.get('device') or '', action['action'], action['position']]
                fingerprint = hashlib.sha256(encoded(identity).encode()).hexdigest()
                if fingerprint in batch_seen or db.execute('SELECT 1 FROM seen WHERE hash=?', (fingerprint,)).fetchone(): continue
                batch_seen.add(fingerprint)
                item = dict(action)
                item['episode'] = aliases.get((item['podcast'], item['episode']), item['episode'])
                output.append(item); fingerprints.append(fingerprint)
            # A same-second event cannot be acknowledged safely with an unchanged cursor.
            if output and cursor == ack:
                raise ValueError('cursor_not_advanced')
            pending = dict(actions=output, timestamp=cursor, fingerprints=fingerprints)
            self.set(db, 'pending', pending)
            self.set(db, 'last_poll', int(time.time()))
            return {'actions': output, 'timestamp': cursor}

    def native_actions(self, db, ack, authorization):
        """Project final source states once; transport ACKs never create plays.

        Original GPodder actions are not a second source of history in this
        mode. They may be receipts written by our playback synchronizer, and
        PinePods' native UI does not reliably create them at all.
        """
        from tracking_projection import NativeProjection
        snapshots = self.native_source.capture(authorization)
        now = int(self.clock())
        # Publish a coherent local source snapshot before any outbound RSS
        # calls. A failed response can leave a ready event, never a receipt.
        db.execute('BEGIN')
        scan_number = (self.get(db, 'native_inventory_scan') or 0) + 1
        NativeProjection.update(db, snapshots, now,
            recovery_ids=self.config.get('native_source', {}).get('recovery_ids', ()),
            scan_number=scan_number)
        self.set(db, 'native_inventory_scan', scan_number)
        db.commit()
        if hasattr(self.resolver, 'begin_poll'): self.resolver.begin_poll()
        output, fingerprints, blocked = [], [], []
        cursor = max(now, ack + 1)
        current = {row['id']: row for row in snapshots}
        for event, successor in tracking_outbox.candidates(db):
            ident = event['_tracking_event_id']
            if not event['_tracking_completed'] and successor != ident:
                # Never-delivered intermediate progress is superseded by a
                # newer state in this same listen. This is not a consumer ACK.
                db.execute('INSERT INTO native_tracking_superseded VALUES(?,?)',
                           (ident, successor))
                continue
            wire = dict(event)
            fresh = current.get(event['_tracking_episode_id'])
            # The pinned native account/podcast/episode ID proves an enclosure
            # refresh independently of titles and duration. Resolve its fresh
            # URL against current RSS while preserving the original event time.
            reason = None
            if fresh is None:
                reason = 'native_episode_missing'
            elif fresh['podcast'] != event['podcast']:
                reason = 'native_feed_changed'
            else:
                wire['episode'] = fresh['episode']
            if not reason and not event['_tracking_completed']:
                # Floppy infers completion five seconds before total. Preserve
                # PinePods' explicit unfinished state in that representation;
                # the journal retains the exact native seconds.
                wire['position'] = min(event['position'], event['total'] - 6)
                if int(wire['position']) <= 0:
                    reason = 'native_position_too_short'
            if not reason:
                try:
                    item = self.resolver.resolve(wire)
                except (ValueError, requests.RequestException) as error:
                    # Resolver exceptions can include a private URL. Only persist
                    # the known reason codes from our RSS resolver.
                    reason = str(error) if str(error) in tracking_outbox.RSS_REASONS else 'resolver_unavailable'
            tracking_outbox.record_attempt(db, ident, cursor, now, reason)
            if reason:
                blocked.append(ident)
                continue
            item = {key: value for key, value in item.items() if not key.startswith('_tracking_')}
            output.append(item); fingerprints.append('native:' + ident)
        pending = dict(actions=output, timestamp=cursor, fingerprints=fingerprints)
        record_batch(db, pending, offered_at=now)
        self.set(db, 'pending', pending)
        self.set(db, 'last_poll', now)
        self.set(db, 'native_scan', dict(at=now, scan_number=scan_number, episodes=len(snapshots),
            source=getattr(self.native_source, 'last_status', {}),
            blocked=db.execute('SELECT count(*) FROM native_outbox WHERE reason IS NOT NULL').fetchone()[0]))
        self.set(db, 'native_blocked', blocked)
        return {'actions': output, 'timestamp': cursor}

    def status(self):
        with self.db() as db:
            pending = self.get(db, 'pending')
            result = dict(ok=True, mode='native-state' if self.native_source is not None else 'legacy-actions',
                        baseline=self.config['baseline'], acknowledged=self.get(db, 'ack'),
                        pending_actions=len(pending['actions']) if pending else 0,
                        last_poll=self.get(db, 'last_poll'), seen=db.execute('SELECT count(*) FROM seen').fetchone()[0])
            if self.native_source is not None:
                result.update(native_scan=self.get(db, 'native_scan'),
                    blocked=db.execute('SELECT count(*) FROM native_outbox WHERE reason IS NOT NULL').fetchone()[0],
                    baseline_episodes=db.execute('SELECT count(*) FROM native_tracking_episodes').fetchone()[0],
                    projected_events=db.execute('SELECT plays FROM native_outbox_stats WHERE id=1').fetchone()[0],
                    superseded_events=db.execute('SELECT count(*) FROM native_tracking_superseded').fetchone()[0],
                    outstanding_events=db.execute('SELECT count(*) FROM native_outbox').fetchone()[0])
            return result

    def record_floppy_poll(self, cursor):
        if self.native_source is not None:
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO native_floppy_polls(cursor,polled_at) VALUES(?,?)',
                           (cursor, int(self.clock())))


def handler(relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'BooksAudioTracking'
        def log_message(self, *args): pass  # Never log credentials, usernames or private feed URLs.
        def respond(self, status, value):
            body = encoded(value).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)
        def dispatch(self):
            floppy_actions = False
            try:
                parsed = urlsplit(self.path)
                if self.command == 'GET' and parsed.path == '/health':
                    self.respond(200, {'ok': True}); return
                authorization = self.headers.get('Authorization', '')
                relay.authenticate(authorization)
                username = quote(relay.username, safe='')
                if self.command == 'POST' and parsed.path == '/api/2/auth/' + username + '/login.json':
                    result = {}
                elif self.command == 'GET' and parsed.path == '/status':
                    result = relay.status()
                elif self.command == 'POST' and re.fullmatch('/api/2/devices/' + re.escape(username) + r'/[A-Za-z0-9_-]{1,100}\.json', parsed.path):
                    # Floppy's existing device is already configured upstream; no new writer.
                    result = {}
                elif self.command == 'GET' and re.fullmatch('/subscriptions/' + re.escape(username) + r'/[A-Za-z0-9_-]{1,100}\.json', parsed.path):
                    result = relay.upstream_request('GET', parsed.path, authorization).json()
                    if not isinstance(result, list) or not all(isinstance(x, str) for x in result): raise ValueError('invalid_subscriptions')
                elif self.command == 'GET' and parsed.path == '/api/2/episodes/' + username + '.json':
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {'since', 'device'} or any(len(v) != 1 for v in query.values()): raise ValueError('invalid_query')
                    since = int(query['since'][0]) if 'since' in query else None
                    result = relay.actions(since, query.get('device', [''])[0], authorization)
                    floppy_actions = bool(re.fullmatch(r'Floppy/[0-9]+(?:\.[0-9]+)*(?: \(https://github\.com/dannyvfilms/Floppy\))?',
                                                       self.headers.get('User-Agent', '')))
                else:
                    self.respond(404, {'error': 'unsupported_endpoint'}); return
            except PermissionError: self.respond(401, {'error': 'authentication_required'})
            except Exception: self.respond(503, {'error': 'tracking_temporarily_unavailable'})
            else:
                try:
                    self.respond(200, result)
                except (BrokenPipeError, ConnectionResetError):
                    return
                if floppy_actions:
                    # The authenticated Floppy request was served; retransmitting
                    # a cursor is the same fetch round. Never count an aborted
                    # response, or emit a second HTTP status after a sent body.
                    try:
                        relay.record_floppy_poll(result['timestamp'])
                    except sqlite3.Error:
                        logging.getLogger(__name__).warning('Could not record Floppy fetch evidence')
        do_GET = dispatch
        do_POST = dispatch
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/tracking/config.json')
    parser.add_argument('--state', default='/tracking/state')
    parser.add_argument('--listen', default='0.0.0.0'); parser.add_argument('--port', default=8791, type=int)
    args = parser.parse_args()
    relay = Relay(json.loads(Path(args.config).read_text()), args.state)
    ThreadingHTTPServer((args.listen, args.port), handler(relay)).serve_forever()


if __name__ == '__main__': main()
