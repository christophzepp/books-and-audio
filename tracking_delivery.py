"""Read-only, per-episode evidence from the private Floppy relay on the NVMe."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def lookup_key(feed, enclosure):
    identity = json.dumps([feed, enclosure], separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def ensure_delivery_schema(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(native_tracking_episodes)')}
    if 'lookup_hash' not in columns:
        db.execute('ALTER TABLE native_tracking_episodes ADD COLUMN lookup_hash TEXT')
    if 'scan_number' not in columns:
        db.execute('ALTER TABLE native_tracking_episodes ADD COLUMN scan_number INTEGER')
    # Older binaries can run after a rollback. They retain any existing new
    # columns but do not refresh them when an enclosure changes.
    db.create_function('relay_lookup_key', 2, lookup_key, deterministic=True)
    calculated = "relay_lookup_key(json_extract(snapshot,'$.podcast'),json_extract(snapshot,'$.episode'))"
    db.execute('UPDATE native_tracking_episodes SET lookup_hash=' + calculated +
               ' WHERE lookup_hash IS NOT ' + calculated)
    db.execute('CREATE INDEX IF NOT EXISTS native_lookup_hash ON native_tracking_episodes(lookup_hash)')
    db.execute('''CREATE TABLE IF NOT EXISTS native_delivery_receipts (
        event_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, offered_at INTEGER, acknowledged_at INTEGER,
        wire_hash TEXT NOT NULL, wire_guid_hash TEXT, wire_timestamp TEXT NOT NULL,
        wire_position REAL NOT NULL, wire_total REAL NOT NULL
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS native_wire_lookup ON native_delivery_receipts(wire_hash)')


def record_batch(db, pending, offered_at=None, acknowledged_at=None):
    for fingerprint, action in zip(pending['fingerprints'], pending['actions'], strict=True):
        if not fingerprint.startswith('native:'):
            continue
        guid = action.get('guid')
        values = (fingerprint[7:], pending['timestamp'], offered_at, acknowledged_at,
                  lookup_key(action['podcast'], action['episode']),
                  lookup_key(action['podcast'], guid) if guid else None,
                  action['timestamp'], action['position'], action['total'])
        db.execute('''INSERT INTO native_delivery_receipts(event_id,cursor,offered_at,acknowledged_at,
                wire_hash,wire_guid_hash,wire_timestamp,wire_position,wire_total)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                offered_at=COALESCE(native_delivery_receipts.offered_at,excluded.offered_at),
                acknowledged_at=COALESCE(native_delivery_receipts.acknowledged_at,excluded.acknowledged_at)
            WHERE native_delivery_receipts.cursor=excluded.cursor''', values)


def _state(db, key):
    row = db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _read(db_path, upstream, username, user_id, operation):
    try:
        uri = Path(db_path).absolute().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            mode = _state(db, 'delivery_mode')
            if mode == 'legacy-actions':
                return {'state': 'legacy'}
            if mode != 'native-state':
                return {'state': 'unavailable'}
            scope = json.loads(_state(db, 'native_scope'))
            if ((upstream is not None and upstream.rstrip('/') != scope[0])
                    or (username is not None and username != scope[1])):
                return {'state': 'scope_mismatch'}
            if user_id is not None:
                verified_id = scope[2] or (_state(db, 'native_scan') or {}).get('source', {}).get('user_id')
                if type(user_id) is not int or user_id != verified_id:
                    return {'state': 'scope_mismatch'}
            return operation(db)
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError, IndexError, AttributeError):
        return {'state': 'unavailable'}


def search_episode_delivery(db_path, *, query='', offset=0, limit=40, upstream=None,
                            username=None, user_id=None):
    if (not isinstance(query, str) or len(query) > 256 or type(offset) is not int
            or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('invalid_delivery_search')

    def select(db):
        # Bind all user input. Literal substring matching makes '%' and '_'
        # ordinary search terms; Unicode casefold includes German titles.
        db.create_function('fold', 1, lambda value: (value or '').casefold())
        rows = db.execute('''SELECT id,json_extract(snapshot,'$.title') AS title,
                json_extract(snapshot,'$.show') AS show,json_extract(snapshot,'$.published_at') AS published
            FROM native_tracking_episodes WHERE ?='' OR instr(fold(title),?) OR instr(fold(show),?) OR instr(id,?)
            ORDER BY published DESC,id LIMIT ? OFFSET ?''', (query, query.casefold(),
                query.casefold(), query, limit + 1, offset)).fetchall()
        return dict(state='ok', offset=offset, has_more=len(rows) > limit,
                    rows=[dict(episode_id=i, title=t, show=s, published_at=p) for i,t,s,p in rows[:limit]])
    return _read(db_path, upstream, username, user_id, select)


def read_episode_delivery(db_path, *, episode_id=None, lookup_hash=None, feed_url=None,
                          enclosure_url=None, upstream=None, username=None, user_id=None):
    if feed_url is not None or enclosure_url is not None:
        if lookup_hash is not None or not all(isinstance(v, str) and v for v in (feed_url, enclosure_url)):
            raise ValueError('invalid_delivery_lookup')
        lookup_hash = lookup_key(feed_url, enclosure_url)
    if ((episode_id is None) == (lookup_hash is None)
            or episode_id is not None and (not isinstance(episode_id, str)
                or not re.fullmatch(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,18}){2}', episode_id))
            or lookup_hash is not None and (not isinstance(lookup_hash, str)
                or not re.fullmatch(r'[0-9a-f]{64}', lookup_hash))):
        raise ValueError('invalid_delivery_lookup')

    return _read(db_path, upstream, username, user_id,
                 lambda db: episode_record(db, episode_id=episode_id, lookup_hash=lookup_hash))


def episode_record(db, *, episode_id=None, lookup_hash=None):
    key = 'id' if episode_id is not None else 'lookup_hash'
    records = db.execute('SELECT id,snapshot,generation,sequence,observed_at,scan_number FROM native_tracking_episodes '
                         'WHERE ' + key + '=? LIMIT 2', (episode_id or lookup_hash,)).fetchall()
    if len(records) != 1:
        return {'state': 'ambiguous' if records else 'not_found'}
    ident, raw, generation, sequence, observed_at, scan_number = records[0]
    snapshot = json.loads(raw)
    source = {k: snapshot.get(k) for k in ('position', 'total', 'completed', 'published_at', 'updated_at')}
    latest_scan = _state(db, 'native_inventory_scan')
    source.update(observed_at=observed_at, present_in_last_scan=(
        scan_number == latest_scan if latest_scan is not None else None))
    latest = db.execute('''SELECT event_id,observed_at,payload FROM native_tracking_events
        WHERE episode_id=? AND generation=? AND sequence=? AND kind='play' ''',
        (ident, generation, sequence)).fetchone()
    event = dict(stage='no_event' if generation else 'baseline', ready_at=None,
                 offered_at=None, cursor=None, acknowledged_at=None)
    identity = lookup_key(snapshot['podcast'], snapshot['episode'])
    multiplicity = db.execute('SELECT count(*) FROM native_tracking_episodes WHERE lookup_hash=?',
                              (identity,)).fetchone()[0]
    match = dict(lookup_hash=identity, unique=multiplicity == 1, wire_hash=None, wire_guid_hash=None,
                 wire_unique=False, wire_timestamp=None, wire_position=None, wire_total=None)
    if latest:
        event_id, ready_at, payload = latest
        event.update(stage='ready', ready_at=ready_at, action_timestamp=json.loads(payload)['timestamp'])
        receipt = db.execute('SELECT cursor,offered_at,acknowledged_at,wire_hash,wire_guid_hash,'
                             'wire_timestamp,wire_position,wire_total FROM native_delivery_receipts '
                             'WHERE event_id=?', (event_id,)).fetchone()
        if receipt:
            event.update(cursor=receipt[0], offered_at=receipt[1], acknowledged_at=receipt[2],
                         stage='acknowledged' if receipt[2] is not None else 'offered')
            other = db.execute('''SELECT 1 FROM native_delivery_receipts r
                JOIN native_tracking_events e ON e.event_id=r.event_id
                WHERE r.wire_hash=? AND e.episode_id!=? LIMIT 1''', (receipt[3], ident)).fetchone()
            match.update(wire_hash=receipt[3], wire_guid_hash=receipt[4], wire_unique=other is None,
                         wire_timestamp=receipt[5], wire_position=receipt[6], wire_total=receipt[7])
        if event['acknowledged_at'] is None and db.execute(
                'SELECT 1 FROM seen WHERE hash=?', ('native:' + event_id,)).fetchone():
            # An older binary may have confirmed this event before the
            # upgrade or during a rollback. Only the fact is known.
            event['stage'] = 'acknowledged'
        elif not receipt:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_outbox'").fetchone():
                reason = db.execute('SELECT reason FROM native_outbox WHERE event_id=?', (event_id,)).fetchone()
                if reason and reason[0]:
                    event.update(stage='blocked', reason=reason[0])
            elif event_id in (_state(db, 'native_blocked') or []):
                event['stage'] = 'blocked'
    return dict(state='ok', episode_id=ident, title=snapshot.get('title'), show=snapshot.get('show'),
                source=source, event=event, target={'state': 'unverified'}, delivery_match=match)
