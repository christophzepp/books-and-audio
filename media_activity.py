"""Bounded, best-effort diagnostics on the state volume. Never opens a media path."""
from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from admin_store import atomic

WEEK = 7 * 86400
MAX_DETAILS = 20000
PAGE_SIZE = 100
KINDS = {
    'podcast': {'duration', 'variant'},
    'folder': {'mount'},
    'ebook': {'discovery', 'index'},
    'readalong': {'source', 'overlay'},
    'series': {'series_target', 'series_import'},
}
COUNTERS = {
    'api_observation_requests': 'api',
    'ebook_source_read_bytes': 'epub_bytes',
    'folder_durable_hits': 'proofs',
    'ebook_media_proof_hits': 'proofs',
    'native_observation_durable_hits': 'proofs',
    'podcast_variant_source_cache_hits': 'proofs',
}
_current = ContextVar('media_activity', default=None)


def title_text(title):
    if not isinstance(title, str):
        return ''
    return ' '.join(title.split())[:220]


def record(kind, title, reason):
    batch = _current.get()
    if batch is None or reason not in KINDS.get(kind, ()):
        return
    at = int(batch.clock())
    batch.details.append((at, kind, title_text(title), reason))
    batch.sums[at // 60, 'checks'] += 1


def count(name, amount=1):
    batch = _current.get()
    if batch is not None and amount:
        batch.sums[int(batch.clock()) // 60, name] += amount


@contextmanager
def recording(clock=time.time):
    batch = Batch(clock)
    token = _current.set(batch)
    try:
        yield batch
    finally:
        _current.reset(token)


class Batch:
    def __init__(self, clock):
        self.clock = clock
        self.details = deque(maxlen=MAX_DETAILS)
        self.sums = Counter()

    def packet(self):
        return {'id': uuid.uuid4().hex, 'details': list(self.details),
                'counts': [[minute, name, n] for (minute, name), n in self.sums.items()]}

    def clear(self):
        self.details.clear()
        self.sums.clear()


SCHEMA = """
CREATE TABLE IF NOT EXISTS details(id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS details_time ON details(at DESC,id DESC);
CREATE TABLE IF NOT EXISTS totals(minute INTEGER, name TEXT, n INTEGER NOT NULL,
    PRIMARY KEY(minute,name));
CREATE TABLE IF NOT EXISTS packets(id TEXT PRIMARY KEY, at INTEGER NOT NULL);
"""


class Journal:
    def __init__(self, control):
        self.path = Path(control) / 'media-activity.sqlite'
        self.ready = False

    def save(self, packet, now, *, deduplicate=False):
        cutoff = int(now) - WEEK
        first_minute = (cutoff + 59) // 60
        with sqlite3.connect(self.path, timeout=2) as db:
            if not self.ready:
                db.executescript(SCHEMA)
                self.path.chmod(0o600)
                self.ready = True
            if deduplicate and not db.execute(
                    'INSERT OR IGNORE INTO packets VALUES (?,?)', (packet['id'], now)).rowcount:
                return
            db.executemany('INSERT INTO details(at,kind,title,reason) VALUES (?,?,?,?)',
                           ((at, kind, title_text(title), reason) for at, kind, title, reason
                            in packet['details'] if reason in KINDS.get(kind, ()) and cutoff <= at <= now))
            db.executemany('INSERT INTO totals VALUES (?,?,?) ON CONFLICT(minute,name) '
                           'DO UPDATE SET n=n+excluded.n', ((minute, name, n) for minute, name, n
                           in packet['counts'] if name in {'checks','cycles','series_runs','api','proofs','epub_bytes'}
                           and type(n) is int and n > 0 and first_minute <= minute <= now//60))
            db.execute('DELETE FROM details WHERE at < ?', (cutoff,))
            db.execute('DELETE FROM details WHERE (at,id) <= (SELECT at,id FROM details ORDER BY at DESC,id DESC LIMIT 1 OFFSET ?)',
                       (MAX_DETAILS,))
            db.execute('DELETE FROM totals WHERE minute < ?', (first_minute,))
            db.execute('DELETE FROM packets WHERE at < ?', (cutoff,))

    def flush(self, batch):
        if batch.sums or batch.details:
            try:
                self.save(batch.packet(), int(batch.clock()))
            except (OSError, sqlite3.Error):
                logging.warning('Medienzugriffe konnten nicht protokolliert werden.')
                # Do not let unavailable diagnostic storage prevent playback sync.
                oldest = int(batch.clock() - WEEK)//60
                for key in list(batch.sums):
                    if key[0] < oldest:
                        del batch.sums[key]
            else:
                batch.clear()

    def import_series(self, directory, now):
        for path in sorted(Path(directory).glob('activity-*.json'))[:100]:
            try:
                if path.is_symlink() or path.stat().st_size > 8*1024*1024:
                    continue
                packet = json.loads(path.read_bytes())
                if path.name != 'activity-' + str(uuid.UUID(hex=packet['id']).hex) + '.json':
                    continue
                self.save(packet, now, deduplicate=True)
                path.unlink()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                logging.warning('Hörspiel-Zugriffsbericht konnte nicht übernommen werden.')

    def report(self, days, before=None, now=None):
        if type(days) is not int or days not in (1, 7):
            raise ValueError('Ungültiger Messzeitraum.')
        end = int(time.time() if now is None else now)
        # Aggregates and host samples have minute resolution. Use the same
        # first in-range minute for counters, item rows and watcher samples.
        start = ((end - days * 86400 + 59) // 60) * 60
        result = {'from': start, 'to': end, 'totals': {}, 'details': [], 'next': None,
                  'first_cycle': None, 'last_cycle': None, 'watcher': []}
        if self.path.is_file():
            db = sqlite3.connect(self.path.absolute().as_uri()+'?mode=ro', uri=True)
            try:
                db.execute('BEGIN')
                result['totals'] = dict(db.execute('SELECT name,SUM(n) FROM totals WHERE minute BETWEEN ? AND ? '
                        'GROUP BY name', (start//60, end//60)))
                first, last = db.execute("SELECT MIN(minute),MAX(minute) FROM totals WHERE name='cycles' "
                        'AND minute BETWEEN ? AND ?', (start//60,end//60)).fetchone()
                result.update(first_cycle=None if first is None else first*60,
                              last_cycle=None if last is None else last*60)
                args = [start, end]
                extra = ''
                if before:
                    extra = ' AND (at,id) < (?,?)'
                    args.extend(before)
                rows = db.execute('SELECT at,id,kind,title,reason FROM details WHERE at BETWEEN ? AND ?'
                                  +extra+' ORDER BY at DESC,id DESC LIMIT ?', (*args, PAGE_SIZE+1)).fetchall()
                result['details'] = [dict(at=r[0],kind=r[2],title=r[3],reason=r[4]) for r in rows[:PAGE_SIZE]]
                if len(rows) > PAGE_SIZE:
                    result['next'] = list(rows[PAGE_SIZE-1][:2])
            finally:
                db.close()
        from disk_activity import samples
        result['watcher'] = watcher_report(self.path.parent, start, end)
        result['kernel_watcher'] = samples(self.path.parent, start, end)
        return result


def spool_series(directory, batch):
    packet = batch.packet()
    path = Path(directory)
    try:
        cutoff = batch.clock() - WEEK
        for old in path.glob('activity-*.json'):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        atomic(path / ('activity-' + packet['id'] + '.json'), packet)
    except OSError:
        logging.warning('Hörspiel-Zugriffsbericht konnte nicht geschrieben werden.')


def watcher_report(directory, start, end):
    # Optional projection of the existing host collector, containing no paths or titles.
    try:
        path = Path(directory) / 'disk-observations.json'
        if path.is_symlink() or path.stat().st_size > 4*1024*1024:
            return []
        data = json.loads(path.read_bytes())
        rows = data['minutes']
        if data['schema'] != 1 or not isinstance(rows, list) or len(rows) > 10100:
            return []
        return [dict(at=at, reads=reads, writes=writes) for at, reads, writes in rows
                if all(type(n) is int and n >= 0 for n in (at, reads, writes)) and start <= at <= end]
    except (OSError, KeyError, TypeError, ValueError):
        return []
