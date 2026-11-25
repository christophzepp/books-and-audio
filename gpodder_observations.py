"""Incremental, read-only gPodder observations on the worker's state volume.

The regular worker holds sync.lock. Feed baselines and action batches are
committed with their cursor in SQLite; a crash can safely replay a batch.
No record here is an outgoing action, a playback baseline or a Floppy cursor.
"""
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json

from maintenance_settings import podcast_window
from pinepods_client import ActionBatchTooLarge, RequestError, action_epoch


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class ActionHistory:
    """One observation batch; store complete histories only for used feeds."""

    def __init__(self, bridge, client, fetch):
        self.db = bridge.db
        self.now = bridge.clock()
        active, window = podcast_window(bridge.config, self.now)
        self.window = window if active else None
        self.account = fingerprint([client.base_url, client.username, client._ensure_native(),
                                    client.PROTOCOL, client.server_version])
        self.fetch = fetch
        self.limit = client.MAX_ACTIONS
        self.ready = False
        self.audit_feed = None
        self.error = None
        self.histories = {}
        self.force_full = set()
        self.feed_errors = {}
        self.db.execute('''CREATE TABLE IF NOT EXISTS gpodder_observation_cursor (
            account TEXT PRIMARY KEY, cursor INTEGER NOT NULL, last_batch TEXT NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS gpodder_observation_feed (
            account TEXT NOT NULL, feed TEXT NOT NULL, actions TEXT NOT NULL,
            checked REAL NOT NULL, attempted REAL NOT NULL, audit_window TEXT, PRIMARY KEY(account,feed))''')
        self.state = self._state()

    def _state(self):
        row = self.db.execute('SELECT cursor,last_batch FROM gpodder_observation_cursor WHERE account=?',
                              (self.account,)).fetchone()
        return tuple(row) if row else None

    @contextmanager
    def _transaction(self):
        # Never keep a transaction or disk lock open while requesting a service.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self._state() != self.state:
                raise RequestError('PinePods: concurrent change to gPodder observation cursor')
            yield

    def _set_state(self, cursor, signature):
        self.db.execute('INSERT OR REPLACE INTO gpodder_observation_cursor VALUES (?,?,?)',
                        (self.account, cursor, signature))

    def _cursor(self, payload):
        cursor = payload['timestamp']
        if type(cursor) is not int or cursor < 0:
            raise RequestError('PinePods: invalid gPodder observation cursor')
        # A client with a fast clock can publish a future event. Never let its
        # timestamp hide subsequently uploaded events from correctly set clocks.
        return min(cursor, max(0, int(self.now)))

    def _reset(self, cursor):
        # A restored server or a truncated delta can no longer vouch for the
        # cached feeds. The next use of each feed needs a complete baseline.
        with self._transaction():
            self.db.execute('DELETE FROM gpodder_observation_feed WHERE account=?', (self.account,))
            self._set_state(cursor, '')
        self.state = (cursor, '')

    def _refresh(self):
        if self.state is None:
            return  # the first complete feed response establishes the cursor
        previous, last_batch = self.state
        since = max(0, previous - 1)  # upstream predicate is strictly greater
        try:
            payload = self.fetch(since=since)
        except ActionBatchTooLarge:
            # A one-row head query returns the account high-water mark in
            # PinePods 0.9.0. Never treat this limited response as a baseline.
            self._reset(self._cursor(self.fetch(limit=1)))
            return
        cursor = self._cursor(payload)
        rows = payload['actions']
        if cursor < previous or since and any(action_epoch(row) <= since for row in rows):
            self._reset(cursor)
            return
        signature = fingerprint(rows)
        if cursor == previous and signature == last_batch:
            return
        groups = defaultdict(list)
        for row in rows:
            groups[row['podcast']].append(row)
        with self._transaction():
            for feed, updates in groups.items():
                old = self.db.execute(
                    'SELECT actions FROM gpodder_observation_feed WHERE account=? AND feed=?',
                    (self.account, feed)).fetchone()
                if old is None:
                    continue  # never infer a complete new feed from a delta
                # Replace the overlapping time slice. Genuine duplicate actions
                # in the same response still count for alias proofs and receipts.
                history = [row for row in json.loads(old[0]) if since and action_epoch(row) <= since]
                history.extend(updates)
                if len(history) > self.limit:
                    self.db.execute('DELETE FROM gpodder_observation_feed WHERE account=? AND feed=?',
                                    (self.account, feed))
                else:
                    encoded = encode(history)
                    if old[0] != encoded:
                        self.db.execute('UPDATE gpodder_observation_feed SET actions=? WHERE account=? AND feed=?',
                                        (encoded, self.account, feed))
            self._set_state(cursor, signature)
        self.state = (cursor, signature)

    def _prepare(self):
        if self.error is not None:
            raise self.error
        if self.ready:
            return
        try:
            self._refresh()
        except RequestError as exc:
            # One failed network request per cycle, even for 1,000 episodes.
            self.error = exc
            raise
        self.ready = True
        if self.window:
            # PinePods filters by the client's event time. A backdated offline
            # action can therefore arrive below the cursor. Audit one complete
            # feed per cycle during the configured maintenance window.
            row = self.db.execute('''SELECT feed FROM gpodder_observation_feed
                WHERE account=? AND (audit_window IS NULL OR audit_window<>?)
                ORDER BY attempted,checked,feed LIMIT 1''', (self.account, self.window)).fetchone()
            if row:
                self.audit_feed = row[0]

    def _complete(self, feed):
        payload = self.fetch(feed_url=feed)
        cursor = self._cursor(payload)
        rows = payload['actions']
        if any(row['podcast'] != feed for row in rows):
            raise RequestError('PinePods: gPodder feed scope differs from response')
        first = self.state is None
        with self._transaction():
            self.db.execute('INSERT OR REPLACE INTO gpodder_observation_feed VALUES (?,?,?,?,?,?)',
                            (self.account, feed, encode(rows), self.now, self.now, self.window))
            if first:
                self._set_state(cursor, '')
        if first:
            self.state = (cursor, '')
        self.histories[feed] = rows
        self.force_full.discard(feed)

    def read(self, feed):
        self._prepare()
        if self.audit_feed is not None:
            audit, self.audit_feed = self.audit_feed, None
            try:
                self._complete(audit)
            except RequestError as exc:
                # An unavailable archival feed cannot block another feed.
                self.feed_errors[audit] = exc
                with self._transaction():
                    self.db.execute('UPDATE gpodder_observation_feed SET attempted=? WHERE account=? AND feed=?',
                                    (self.now, self.account, audit))
        if feed in self.feed_errors:
            raise self.feed_errors[feed]
        if feed not in self.histories:
            row = self.db.execute('SELECT actions FROM gpodder_observation_feed WHERE account=? AND feed=?',
                                  (self.account, feed)).fetchone()
            if row is None or feed in self.force_full:
                try:
                    self._complete(feed)
                except RequestError as exc:
                    self.feed_errors[feed] = exc
                    raise
            else:
                self.histories[feed] = json.loads(row[0])
        return {'actions': self.histories[feed], 'timestamp': self.state[0]}

    def invalidate(self, feed, *, durable=False):
        if durable:
            # A live comparison found a receipt below our incremental cursor.
            # Even if the worker exits now, rebaseline this feed on next use.
            with self._transaction():
                self.db.execute('DELETE FROM gpodder_observation_feed WHERE account=? AND feed=?',
                                (self.account, feed))
        self.feed_errors.pop(feed, None)
        self.histories.pop(feed, None)
        self.force_full.add(feed)
