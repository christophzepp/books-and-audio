"""Durable scheduling of automatic podcast file checks on the worker's state volume."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

from maintenance_settings import podcast_window

PREFIX = '_podcast_file_check:'
CURRENT = ContextVar('podcast_file_check', default=None)
MAX_EPISODES_PER_CYCLE = 3


class FileCheckScheduled(ValueError):
    """This episode's physical verification is reserved for media maintenance."""


def require_file_check(bridge, mapping, *, enqueue_only=False):
    job = CURRENT.get()
    if job is not None and job.queue.bridge is bridge and job.mapping_id == mapping['id']:
        job.require(enqueue_only=enqueue_only)


class EpisodeCheck:
    def __init__(self, queue, mapping):
        from podcast_sync import fingerprint
        self.queue = queue
        self.mapping_id = mapping['id']
        self.identity = fingerprint([mapping['fingerprint'], mapping['jf']['fingerprint'],
                                     mapping['worker_path'], mapping.get('abs_guid'),
                                     mapping.get('feed_url'), mapping.get('enclosure_url')])
        self.claimed_window = None
        self.deferred = False

    def require(self, *, enqueue_only):
        queue = self.queue
        active, window = podcast_window(queue.bridge.config, queue.bridge.clock())
        if self.claimed_window == window and active and not enqueue_only:
            return
        previous = queue.entries.get(self.mapping_id, {})
        row = dict(previous) if previous.get('identity') == self.identity else {'identity': self.identity}
        row['pending'] = True
        if (not queue.apply or not active or enqueue_only or queue.remaining <= 0
                or row.get('last_window') == window):
            if queue.apply and row != previous:
                queue.replace(self.mapping_id, row)
            self.deferred = True
            raise FileCheckScheduled('Die Dateiprüfung ist für die Medienwartung vorgemerkt.')
        # Persist before touching a file. A restart after a failed check must not
        # turn one episode into a stat storm for the rest of the same window.
        if not queue.replace(self.mapping_id, dict(row, last_window=window)):
            self.deferred = True
            raise FileCheckScheduled('Die Dateiprüfung wurde inzwischen neu eingeplant.')
        queue.remaining -= 1
        self.claimed_window = window
        self.deferred = False


class PodcastFileChecks:
    def __init__(self, bridge, apply):
        self.bridge, self.apply = bridge, apply
        self.remaining = MAX_EPISODES_PER_CYCLE
        self.original = {key[len(PREFIX):]: value for key, value in bridge.db.execute(
            'SELECT id,value FROM state WHERE id GLOB ?', (PREFIX + '*',))}
        self.entries = {key: json.loads(value) for key, value in self.original.items()}

    def replace(self, mapping_id, row):
        """Acquire, finish or remove a job only if its observed revision still matches."""
        key = PREFIX + mapping_id
        encoded = json.dumps(row, separators=(',',':'), sort_keys=True) if row is not None else None
        old = self.original.get(mapping_id)
        with self.bridge.db:
            if encoded is None:
                changed = self.bridge.db.execute('DELETE FROM state WHERE id=? AND value=?', (key, old))
            elif old is None:
                changed = self.bridge.db.execute('INSERT OR IGNORE INTO state(id,value) VALUES(?,?)',
                                                 (key, encoded))
            else:
                changed = self.bridge.db.execute('UPDATE state SET value=? WHERE id=? AND value=?',
                                                 (encoded, key, old))
        if changed.rowcount != 1:
            fresh = self.bridge.db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
            encoded = fresh[0] if fresh else None
        if encoded is None:
            self.original.pop(mapping_id,None)
            self.entries.pop(mapping_id,None)
        else:
            self.original[mapping_id] = encoded
            self.entries[mapping_id] = json.loads(encoded)
        return changed.rowcount == 1

    def maintenance_order(self, mappings):
        # A full window may end before a long queue is drained. At the next
        # opening, jobs still awaiting their first turn precede repeat checks.
        # Stable ties retain the metadata/transfer journal priority.
        if not self.apply or not self.entries or not podcast_window(self.bridge.config, self.bridge.clock())[0]:
            return mappings
        return sorted(mappings, key=lambda item: self.entries.get(item['id'], {}).get('last_window', ''))

    def complete(self, mapping_id):
        row = self.entries.get(mapping_id)
        if self.apply and row and row.get('pending'):
            if 'last_window' in row:
                # Preserve this night's claim across a restart and a later
                # requeue of the same identity, even after a successful read.
                self.replace(mapping_id, dict(row, pending=False))
            else:
                self.discard(mapping_id)

    @property
    def pending_count(self):
        return sum(row.get('pending') is True for row in self.entries.values())

    def discard(self, mapping_id):
        if self.apply and mapping_id in self.entries:
            self.replace(mapping_id, None)

    @contextmanager
    def episode(self, mapping):
        job = EpisodeCheck(self, mapping)
        token = CURRENT.set(job)
        try:
            yield job
        finally:
            CURRENT.reset(token)
