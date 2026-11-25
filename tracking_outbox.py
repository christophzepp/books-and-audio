"""Incremental index over the immutable native play journal (SQLite on the NVMe)."""
import json


BATCH_LIMIT = 256
RSS_REASONS = frozenset({
    'tracking_invalid_feed', 'tracking_feed_too_large', 'tracking_invalid_feed_identity',
    'tracking_invalid_feed_url', 'tracking_invalid_feed_redirect', 'tracking_feed_redirect_limit',
    'tracking_feed_unavailable', 'tracking_rss_identity_changed', 'tracking_rss_identity_unresolved',
    'tracking_rss_identity_ambiguous',
})


def ensure_schema(db, pending, blocked, last_poll):
    # Called inside Relay's existing startup transaction. Install the triggers
    # once, including for old binaries that continue to insert original rows
    # following a downgrade. Every journal/ack/supersession write maintains
    # this secondary index within the writer's own SQLite transaction.
    db.execute('''CREATE TABLE IF NOT EXISTS native_outbox (
        event_rowid INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, reason TEXT,
        last_attempt_at INTEGER, first_attempt_cursor INTEGER, last_attempt_cursor INTEGER)''')
    db.execute('CREATE INDEX IF NOT EXISTS native_outbox_turn ON native_outbox(last_attempt_cursor,event_rowid)')
    db.execute('CREATE INDEX IF NOT EXISTS native_outbox_blocked ON native_outbox(event_rowid) WHERE reason IS NOT NULL')
    db.execute('''CREATE INDEX IF NOT EXISTS native_latest_play ON native_tracking_events
        (episode_id,generation,sequence DESC) WHERE kind='play' ''')
    db.execute('''CREATE INDEX IF NOT EXISTS native_last_inventory ON native_tracking_events
        (observed_at) WHERE kind IN ('baseline','observation')''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_floppy_polls(
        id INTEGER PRIMARY KEY, cursor INTEGER NOT NULL UNIQUE, polled_at INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS native_receipt_cursor ON native_delivery_receipts(cursor)')
    db.execute('''CREATE TABLE IF NOT EXISTS native_outbox_stats (
        id INTEGER PRIMARY KEY CHECK(id=1), plays INTEGER NOT NULL)''')
    if db.execute('SELECT 1 FROM native_outbox_stats WHERE id=1').fetchone() is None:
        db.execute('''INSERT INTO native_outbox(event_rowid,event_id)
            SELECT e.rowid,e.event_id FROM native_tracking_events e WHERE e.kind='play'
            AND NOT EXISTS(SELECT 1 FROM seen WHERE hash='native:' || e.event_id)
            AND NOT EXISTS(SELECT 1 FROM native_tracking_superseded s WHERE s.event_id=e.event_id)''')
        db.execute("INSERT INTO native_outbox_stats SELECT 1,COUNT(*) FROM native_tracking_events WHERE kind='play'")
        if blocked:
            db.executemany("UPDATE native_outbox SET reason='legacy_blocked',last_attempt_at=? WHERE event_id=?",
                           ((last_poll, ident) for ident in blocked))
        if pending:
            db.executemany('''UPDATE native_outbox SET reason=NULL,last_attempt_at=?,first_attempt_cursor=?,
                           last_attempt_cursor=? WHERE event_id=?''',
                           ((last_poll, pending['timestamp'], pending['timestamp'], f[7:])
                            for f in pending['fingerprints'] if f.startswith('native:')))
    db.execute('''CREATE TRIGGER IF NOT EXISTS track_new_native_play AFTER INSERT ON native_tracking_events
        WHEN NEW.kind='play' BEGIN
            INSERT INTO native_outbox(event_id) VALUES(NEW.event_id);
            UPDATE native_outbox_stats SET plays=plays+1 WHERE id=1;
        END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS track_native_confirmation AFTER INSERT ON seen
        WHEN substr(NEW.hash,1,7)='native:' BEGIN
            DELETE FROM native_outbox WHERE event_id=substr(NEW.hash,8);
        END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS track_native_supersession AFTER INSERT ON native_tracking_superseded
        BEGIN DELETE FROM native_outbox WHERE event_id=NEW.event_id; END''')


def candidates(db, limit=BATCH_LIMIT):
    # Reserve capacity for both first attempts and retries. One bad feed can
    # neither monopolize the next poll nor starve forever as a catalog grows.
    def rows(condition, size):
        return list(db.execute('SELECT event_rowid,event_id FROM native_outbox WHERE last_attempt_cursor IS '
            + condition + ' NULL ORDER BY last_attempt_cursor,event_rowid LIMIT ?', (size,)))

    new = rows('', limit)
    retry = rows('NOT', max(limit // 2, limit-len(new)))
    selected = sorted(new[:limit-len(retry)] + retry)
    for _, ident in selected:
        payload, successor = db.execute('''SELECT e.payload,(
            SELECT n.event_id FROM native_tracking_events n WHERE n.kind='play'
            AND n.episode_id=e.episode_id AND n.generation=e.generation ORDER BY n.sequence DESC LIMIT 1
            ) FROM native_tracking_events e WHERE e.event_id=?''', (ident,)).fetchone()
        yield json.loads(payload), successor


def record_attempt(db, ident, cursor, now, reason):
    db.execute('''UPDATE native_outbox SET reason=?,last_attempt_at=?,
        first_attempt_cursor=COALESCE(first_attempt_cursor,?),last_attempt_cursor=? WHERE event_id=?''',
        (reason, now, cursor, cursor, ident))
