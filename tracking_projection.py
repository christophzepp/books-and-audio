"""Durable listening-event projection of the authoritative native inventory.

This is deliberately separate from playback synchronization.  Establishing a
baseline or confirming an already completed episode is not another listen.
The provider must supply a complete, freshly verified account inventory; an
omitted row is never interpreted here as an explicit reset.

The caller owns its SQLite transaction.  A savepoint makes an update atomic
without committing any unrelated relay changes in an enclosing transaction.
Persisted event payloads are append-only and can be replayed unchanged after a
failed delivery or a restart.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


_INITIALIZED = 'native-projection:initialized:v1'
_DEVICE = 'books-audio-native'
_MAX_EPOCH = 253_402_300_799
_FIELDS = frozenset(('id', 'podcast', 'episode', 'guid', 'completed',
                     'position', 'total', 'updated_at', 'published_at'))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _number(value, *, positive=False):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (value > 0 if positive else value >= 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('invalid_native_tracking_number')
    return value


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError('invalid_native_tracking_identity')
    return value


def _url(value):
    _text(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError()
        parsed.port  # Reject invalid port syntax as part of input validation.
    except ValueError:
        raise ValueError('invalid_native_tracking_url') from None
    return value


def _optional_time(value, observed_at, *, activity=False):
    # A missing native activity time is not evidence that an unseen historical
    # episode was heard now.  An emitted edge can use observed_at as its fallback
    # only after that edge has been independently established.
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        return None
    if value > _MAX_EPOCH or activity and value > observed_at:
        raise ValueError('invalid_native_tracking_activity_time')
    return value


def _snapshot(row, observed_at):
    if not isinstance(row, dict) or set(row) - _FIELDS:
        raise ValueError('invalid_native_tracking_snapshot')
    try:
        result = dict(id=_text(row['id']), podcast=_url(row['podcast']),
                      episode=_url(row['episode']), completed=row['completed'],
                      position=_number(row['position']),
                      total=_number(row['total'], positive=True))
    except KeyError:
        raise ValueError('incomplete_native_tracking_snapshot') from None
    if type(result['completed']) is not bool:
        raise ValueError('invalid_native_tracking_completion')
    # Historical native positions can exceed a refreshed duration after an
    # explicit completion.  Only that completed state ignores the old seconds.
    if not result['completed'] and result['position'] > result['total']:
        raise ValueError('invalid_native_tracking_position')
    guid = row.get('guid')
    if guid is not None:
        result['guid'] = _text(guid)
    result['updated_at'] = _optional_time(row.get('updated_at'), observed_at, activity=True)
    result['published_at'] = _optional_time(row.get('published_at'), observed_at)
    return result


def _active(row):
    return row['completed'] or row['position'] > 0


class NativeProjection:
    """Translate native state edges into immutable, consumer-ready play events."""

    @staticmethod
    def ensure_schema(db):
        # executescript() implicitly commits on sqlite3; individual execute()
        # calls preserve an enclosing relay transaction.
        db.execute('''CREATE TABLE IF NOT EXISTS native_tracking_episodes (
            id TEXT PRIMARY KEY,
            snapshot TEXT NOT NULL,
            generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            observed_at REAL NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS native_tracking_events (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            episode_id TEXT,
            generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            reason TEXT NOT NULL,
            observed_at REAL NOT NULL,
            snapshot TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(episode_id, sequence)
        )''')

    @staticmethod
    def update(db, snapshots, observed_at, recovery_ids=()):
        """Persist a complete observation and return newly created play events.

        The first successful observation establishes the baseline, including an
        empty inventory.  Only explicitly listed, active recovery IDs emit on
        that first observation.  Recovery requests are recorded in its audit
        marker; repeating those IDs later cannot manufacture another event.
        An active row first discovered later requires recent native activity or
        a publication since the preceding complete observation.  Subscribing to
        an old catalog therefore does not manufacture thousands of new listens.

        Returned events include private ``_tracking_*`` identifiers for durable
        delivery bookkeeping.  An HTTP adapter may remove those fields from the
        GPodder wire response.  ``events()`` reads the original payloads rather
        than retimestamping them on a retry.
        """
        observed_at = _number(observed_at, positive=True)
        if observed_at > _MAX_EPOCH:
            raise ValueError('invalid_native_tracking_observation_time')
        if isinstance(snapshots, (dict, str, bytes)):
            raise ValueError('invalid_native_tracking_inventory')
        try:
            rows = [_snapshot(row, observed_at) for row in snapshots]
        except TypeError:
            raise ValueError('invalid_native_tracking_inventory') from None
        ids = {row['id'] for row in rows}
        if len(ids) != len(rows):
            raise ValueError('duplicate_native_tracking_id')
        if isinstance(recovery_ids, (str, bytes, dict)):
            raise ValueError('invalid_native_tracking_recovery_ids')
        try:
            recovered = {_text(value) for value in recovery_ids}
        except TypeError:
            raise ValueError('invalid_native_tracking_recovery_ids') from None
        savepoint = 'native_projection_' + uuid.uuid4().hex
        db.execute('SAVEPOINT ' + savepoint)
        try:
            NativeProjection.ensure_schema(db)
            initialized = db.execute(
                'SELECT 1 FROM native_tracking_events WHERE event_id=?',
                (_INITIALIZED,)).fetchone() is not None
            if not initialized and recovered - ids:
                raise ValueError('unknown_native_tracking_recovery_id')
            previous_poll = db.execute('''SELECT MAX(observed_at)
                FROM native_tracking_events WHERE kind IN ('baseline','observation')''').fetchone()[0]
            if previous_poll is not None and observed_at < previous_poll:
                raise ValueError('native_tracking_observation_regressed')
            emitted = []
            for row in sorted(rows, key=lambda value: value['id']):
                record = db.execute('''SELECT snapshot,generation,sequence,observed_at
                    FROM native_tracking_episodes WHERE id=?''', (row['id'],)).fetchone()
                old = json.loads(record[0]) if record else None
                generation, sequence = (record[1], record[2]) if record else (0, 0)
                if record and observed_at < record[3]:
                    raise ValueError('native_tracking_observation_regressed')

                if old is None:
                    recent_activity = (row['updated_at'] is not None and
                                       previous_poll is not None and
                                       row['updated_at'] > previous_poll)
                    recent_publication = (row['published_at'] is not None and
                                          previous_poll is not None and
                                          previous_poll <= row['published_at'] <= observed_at)
                    emit = _active(row) and (
                        (initialized and (recent_activity or recent_publication))
                        or (not initialized and row['id'] in recovered))
                    reason = 'new_episode' if initialized else 'explicit_recovery'
                else:
                    # An observed reset starts a new listening generation, even
                    # though stock Floppy cannot represent resetting its history.
                    reset = not row['completed'] and row['position'] == 0
                    restarted = (not row['completed'] and row['position'] > 0
                                 and old['completed'])
                    if (reset and _active(old)) or restarted:
                        generation += 1
                    if row['completed']:
                        emit = not old['completed']
                        reason = 'completed'
                    else:
                        emit = row['position'] > 0 and (
                            old['completed'] or row['position'] != old['position'])
                        reason = 'progress'

                if emit:
                    sequence += 1
                    event_id = hashlib.sha256(_encoded(
                        ['native-projection-v1', row['id'], generation, sequence]
                    ).encode()).hexdigest()
                    activity_time = row['updated_at']
                    if activity_time is None or record and activity_time <= record[3]:
                        # PinePods can change its native Completed flag without
                        # refreshing the listening-history timestamp.  A newly
                        # observed edge cannot predate the last observation of
                        # the opposite state.  Keep that stale source timestamp
                        # in the snapshot, but stamp this event once at capture.
                        activity_time = observed_at
                    stamp = datetime.fromtimestamp(activity_time, timezone.utc)
                    payload = dict(podcast=row['podcast'], episode=row['episode'],
                        device=_DEVICE, timestamp=stamp.isoformat().replace('+00:00', 'Z'),
                        action='play', started=0,
                        position=row['total'] if row['completed'] else row['position'],
                        total=row['total'], _tracking_event_id=event_id,
                        _tracking_episode_id=row['id'], _tracking_generation=generation,
                        _tracking_completed=row['completed'])
                    if row.get('guid') is not None:
                        payload['guid'] = row['guid']
                    db.execute('''INSERT INTO native_tracking_events
                        (event_id,kind,episode_id,generation,sequence,reason,
                         observed_at,snapshot,payload) VALUES(?,?,?,?,?,?,?,?,?)''',
                        (event_id, 'play', row['id'], generation, sequence, reason,
                         observed_at, _encoded(row), _encoded(payload)))
                    emitted.append(payload)
                db.execute('''INSERT INTO native_tracking_episodes
                    (id,snapshot,generation,sequence,observed_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot,
                    generation=excluded.generation,sequence=excluded.sequence,
                    observed_at=excluded.observed_at''',
                    (row['id'], _encoded(row), generation, sequence, observed_at))

            if not initialized:
                audit = dict(inventory_count=len(rows), recovery_ids=sorted(recovered),
                             observed_at=observed_at)
                db.execute('''INSERT INTO native_tracking_events
                    (event_id,kind,episode_id,generation,sequence,reason,
                     observed_at,snapshot,payload) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (_INITIALIZED, 'baseline', None, 0, 0, 'initial_baseline',
                     observed_at, _encoded(audit), '{}'))
            # Keep the full-inventory observation watermark separate from the
            # episode's own activity time.  This also advances an empty poll and
            # prevents a newly subscribed historical catalog becoming listens.
            audit = dict(inventory_count=len(rows), observed_at=observed_at)
            observation_id = hashlib.sha256(_encoded(
                ['native-observation-v1', observed_at, sorted(rows, key=lambda row: row['id'])]
            ).encode()).hexdigest()
            db.execute('''INSERT OR IGNORE INTO native_tracking_events
                (event_id,kind,episode_id,generation,sequence,reason,
                 observed_at,snapshot,payload) VALUES(?,?,?,?,?,?,?,?,?)''',
                (observation_id, 'observation', None, 0, 0, 'complete_inventory',
                 observed_at, _encoded(audit), '{}'))
            db.execute('RELEASE SAVEPOINT ' + savepoint)
            return emitted
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
            db.execute('RELEASE SAVEPOINT ' + savepoint)
            raise

    @staticmethod
    def events(db):
        """Read immutable play payloads in insertion order for durable delivery."""
        NativeProjection.ensure_schema(db)
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM native_tracking_events WHERE kind='play' ORDER BY rowid")]
