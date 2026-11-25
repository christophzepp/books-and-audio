"""Surface outstanding Floppy deliveries using only the NVMe relay journal and scoped evidence."""
import json
import os
import sqlite3
import threading
import time

from admin_delivery import relay_context
from floppy_evidence import MAX_AGE, attach_floppy_evidence, digest, load_snapshot, number, scope as evidence_scope
from tracking_delivery import _read, _state, episode_record

_LOCK = threading.Lock()
INTERVAL = 60
PAGE = 40
SETTING = 'floppy_delivery_alerts'

# Never display raw exception strings, URLs or third-party response bodies.
REASONS = {
    'native_episode_missing': ('Die Folge fehlt im letzten PinePods-Bestand.', 'Prüfe in PinePods, ob das Abo und die Folge vorhanden sind.'),
    'native_feed_changed': ('Die Feed-Adresse hat sich geändert.', 'Vergleiche die Feed-Adresse des Abos mit der aktuellen RSS-Adresse.'),
    'native_position_too_short': ('Der Hörstand ist zu kurz für einen GPodder-Eintrag.', 'Prüfe den Hörstand nach dem nächsten Hören.'),
    'tracking_feed_unavailable': ('Der RSS-Feed war beim Abruf nicht erreichbar.', 'Prüfe die Erreichbarkeit des Feeds. Das Relay versucht es erneut.'),
    'tracking_invalid_feed': ('Die Antwort des Feeds war kein lesbares RSS-Dokument.', 'Öffne den RSS-Feed und prüfe seine aktuelle Adresse.'),
    'tracking_feed_too_large': ('Der Feed überschreitet die zulässige Größe.', 'Prüfe, ob der Anbieter einen kleineren regulären Episodenfeed führt.'),
    'tracking_rss_identity_changed': ('Die Zuordnung zur Audiodatei hat sich geändert.', 'Vergleiche die Folge und ihre Audiodatei in PinePods mit dem aktuellen RSS-Eintrag.'),
    'tracking_rss_identity_unresolved': ('Im Feed fehlt ein eindeutiger Treffer für die Folge.', 'Prüfe die Folge, ihre GUID und die Audio-Adresse im RSS-Feed.'),
    'tracking_rss_identity_ambiguous': ('Mehrere RSS-Einträge kommen für die Folge infrage.', 'Vergleiche GUID und Audio-Adresse der betroffenen RSS-Einträge.'),
    'offered': ('Das Relay hat die Folge bereitgestellt. Die Cursor-Bestätigung steht aus.', 'Prüfe in Floppy unter „Daten importieren → GPodder“ den letzten Abruf.'),
    'ready': ('Der Hörstand wartet auf die nächste GPodder-Auslieferung.', 'Prüfe in Floppy den nächsten GPodder-Abruf.'),
    'proof_wait': ('Der GPodder-Empfang ist bestätigt. Ein aktueller Floppy-Nachweis steht aus.', 'Prüfe den letzten GPodder-Import und den Zeitpunkt des Floppy-Nachweises.'),
    'target_unconfirmed': ('Der aktuelle Floppy-Nachweis bestätigt diesen Hörstand noch nicht.', 'Vergleiche den Eintrag der Folge in Floppy mit dem aktuellen PinePods-Hörstand.'),
    'resolver_unavailable': ('Die Prüfung der Episodenzuordnung ist fehlgeschlagen.', 'Prüfe den aktuellen Feed und die Zustellhistorie der Folge.'),
    'legacy_blocked': ('Vor dem Update war die Folge gesperrt. Der genaue Grund wird neu geprüft.', 'Sieh nach dem nächsten GPodder-Abruf erneut in die Zustellhistorie.'),
}
for _code in ('tracking_invalid_feed_identity', 'tracking_invalid_feed_url', 'tracking_invalid_feed_redirect',
              'tracking_feed_redirect_limit'):
    REASONS[_code] = ('Die Feed-Adresse oder eine Weiterleitung konnte nicht bestätigt werden.',
                      'Prüfe die hinterlegte RSS-Adresse und ihr Weiterleitungsziel.')


def profile(view, kind):
    active = [p for p in view.get('profiles', ()) if p.get('kind') == kind and p.get('active') is True]
    return active[0] if len(active) == 1 else {}


def notification_guard(view):
    pine, ntfy = profile(view, 'pinepods'), profile(view, 'ntfy')
    p = pine.get('settings', {})
    n = ntfy.get('settings', {})
    fields = [pine.get('id'), p.get('url'), p.get('username'), ntfy.get('id'), n.get('url')]
    return digest(fields) if all(fields) else None


def preference(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
    return json.loads(row[0]) if row else {}


def poll_window(db, cursor, enabled_since):
    if cursor is None:
        return [], []
    rows = db.execute('SELECT cursor,polled_at FROM native_floppy_polls WHERE cursor>=? ORDER BY cursor LIMIT 2',
                      (cursor,)).fetchall()
    enabled = db.execute('SELECT cursor,polled_at FROM native_floppy_polls WHERE cursor>=? AND polled_at>? '
                         'ORDER BY cursor LIMIT 2', (cursor, enabled_since)).fetchall() if enabled_since else []
    return rows, enabled


def make_case(episode, title, show, code, attempted, polls):
    reason, action = REASONS.get(code, REASONS['resolver_unavailable'])
    return dict(episode_id=episode, title=title, show=show, code=code, reason=reason, action=action,
                last_attempt_at=attempted, poll_count=len(polls))


def collect(relay, db, account, target, scope_key, pref, now):
    previous = db.execute('SELECT poll_id FROM floppy_monitor WHERE scope=?', (scope_key,)).fetchone()
    last_poll = relay.execute('SELECT COALESCE(MAX(id),0) FROM native_floppy_polls').fetchone()[0]
    checkpoint = previous[0] if previous else last_poll
    existing = dict(db.execute('SELECT event_id,episode_id FROM floppy_cases WHERE scope=?', (scope_key,)))
    enabled_since = pref.get('at', 0)
    cases = {}
    for event, episode, title, show, code, attempted, first, offered in relay.execute("""
            SELECT o.event_id,e.episode_id,
                   COALESCE(json_extract(s.snapshot,'$.title'),json_extract(e.snapshot,'$.title')),
                   COALESCE(json_extract(s.snapshot,'$.show'),json_extract(e.snapshot,'$.show')),
                   o.reason,o.last_attempt_at,o.first_attempt_cursor,r.offered_at
            FROM native_outbox o CROSS JOIN native_tracking_events e ON e.event_id=o.event_id
            LEFT JOIN native_tracking_episodes s ON s.id=e.episode_id
            LEFT JOIN native_delivery_receipts r ON r.event_id=o.event_id"""):
        polls, eligible = poll_window(relay, first, enabled_since)
        case = make_case(episode, title, show, code or ('offered' if offered else 'ready'),
                         attempted or offered, polls)
        cases[event] = (case, len(eligible) == 2)
    if not target['floppy_user'] or not target['relay_url'] or not target['path']:
        return cases, last_poll
    watched = {e:i for e,i in existing.items() if e not in cases}
    # Older confirmations have no observation of an actual stock-Floppy poll.
    # Follow only newly polled receipts, plus individual outstanding cases.
    for event, episode in relay.execute("""
            SELECT r.event_id,e.episode_id FROM native_floppy_polls p
            CROSS JOIN native_delivery_receipts r ON r.cursor=p.cursor
            CROSS JOIN native_tracking_events e ON e.event_id=r.event_id WHERE p.id>?""", (checkpoint,)):
        if event not in cases:
            watched[event] = episode
    if not watched:
        return cases, last_poll
    try:
        snapshot = load_snapshot(target['path'])
        expected = evidence_scope(account['upstream'], account['username'], target['floppy_user'], target['relay_url'])
        if (not isinstance(snapshot, dict) or snapshot.get('version') != 1 or snapshot.get('scope') != expected
                or not number(snapshot.get('observed_at')) or not now-MAX_AGE <= snapshot['observed_at'] <= now
                or not number(snapshot.get('synced_at')) or snapshot['synced_at'] > snapshot['observed_at']
                or type(snapshot.get('cursor')) is not int or not isinstance(snapshot.get('episodes'), dict)):
            snapshot = {}
    except (OSError, ValueError, TypeError, RecursionError):
        snapshot = {}
    excluded = snapshot.get('excluded')
    excluded = set(excluded) if isinstance(excluded,list) and all(isinstance(k,str) for k in excluded) else None
    for event, episode in watched.items():
        receipt = relay.execute("""SELECT r.cursor,r.acknowledged_at,p.polled_at FROM native_delivery_receipts r
            JOIN native_floppy_polls p ON p.cursor=r.cursor JOIN native_tracking_events e ON e.event_id=r.event_id
            JOIN native_tracking_episodes s ON s.id=e.episode_id AND s.generation=e.generation AND s.sequence=e.sequence
            WHERE r.event_id=? AND r.acknowledged_at IS NOT NULL""", (event,)).fetchone()
        if not receipt:
            continue
        cursor, ack, last_attempt = receipt
        record = episode_record(relay, episode_id=episode)
        if record.get('state') != 'ok':
            continue
        proof = attach_floppy_evidence(record, **target, upstream=account['upstream'], username=account['username'],
                                      snapshot=snapshot, now=now)
        if proof['target']['state'] == 'verified':
            continue
        polls, eligible = poll_window(relay, cursor, enabled_since)
        src = record['source']
        observed = src.get('observed_at')
        evidence_polls = eligible if len(eligible) == 2 else polls
        identity = record['delivery_match']['wire_hash']
        target_row = (snapshot.get('episodes') or {}).get(identity)
        interpretable = (isinstance(target_row,dict) and type(target_row.get('completed')) is bool
                         and type(target_row.get('seconds')) is int and number(target_row.get('updated_at')))
        absence_checked = (snapshot.get('truncated') is False and excluded is not None and identity not in excluded
                           and identity not in (snapshot.get('episodes') or {}))
        current = bool((interpretable or absence_checked) and evidence_polls and snapshot['cursor'] >= evidence_polls[-1][0]
                       and snapshot['synced_at'] >= max(record['event']['ready_at'], evidence_polls[-1][1] - 1)
                       and number(observed) and now-MAX_AGE <= observed <= now
                       and src['present_in_last_scan'] is True and record['delivery_match']['unique']
                       and record['delivery_match']['wire_unique'])
        code = 'target_unconfirmed' if current else 'proof_wait'
        cases[event] = (make_case(episode, record['title'], record['show'], code, last_attempt, polls),
                        current and len(eligible) == 2)
    return cases, last_poll


def reconcile(store, env=None, now=None):
    env = os.environ if env is None else env
    now = int(time.time() if now is None else now)
    view = store.list_connections()
    context = relay_context(store, env, view=view)
    if context is None:
        with _LOCK, store.db() as db:
            db.execute('UPDATE floppy_monitor SET checked_at=0 WHERE checked_at!=0')
            db.execute('DELETE FROM settings WHERE key=?', (SETTING,))
        return {'state':'scope_mismatch'}
    path, account = context
    target = dict(path=env.get('FLOPPY_EVIDENCE_FILE', '/tracking-proof/floppy.json'),
                  floppy_user=env.get('FLOPPY_EVIDENCE_USER', ''), relay_url=env.get('FLOPPY_EVIDENCE_RELAY_URL', ''))
    guard = notification_guard(view)
    with _LOCK, store.db() as db:
        pref = preference(db)
        def read(relay):
            native = _state(relay, 'native_scope')
            scan = _state(relay, 'native_scan') or {}
            actual_user = json.loads(native)[2] or (scan.get('source') or {}).get('user_id')
            scope_key = digest([profile(view, 'pinepods').get('id'), native, actual_user, target['floppy_user'], target['relay_url']])
            active = bool(guard and pref.get('guard') == guard and pref.get('scope') == scope_key)
            cases, last_poll = collect(relay, db, account, target, scope_key, pref if active else {}, now)
            return dict(state='ok', scope=scope_key, cases=cases, poll_id=last_poll,
                        guard=guard, notification_enabled=active)
        result = _read(path, account['upstream'], account['username'], account.get('user_id'), read)
        if result['state'] != 'ok':
            # Fail closed even if a previously eligible batch has not yet been sent.
            db.execute('UPDATE floppy_monitor SET checked_at=0 WHERE checked_at!=0')
            return result
        cases, poll_id = result.pop('cases'), result.pop('poll_id')
        key = result['scope']
        db.execute('BEGIN IMMEDIATE')
        if pref and not result['notification_enabled']:
            db.execute('DELETE FROM settings WHERE key=?', (SETTING,))
        db.execute('DELETE FROM floppy_monitor WHERE scope!=?', (key,))
        db.execute('DELETE FROM floppy_cases WHERE scope!=?', (key,))
        old = {row[0] for row in db.execute('SELECT event_id FROM floppy_cases WHERE scope=?', (key,))}
        db.executemany('DELETE FROM floppy_cases WHERE scope=? AND event_id=?', [(key, e) for e in old - cases.keys()])
        for event, (row, eligible) in cases.items():
            db.execute('''INSERT INTO floppy_cases(scope,event_id,episode_id,report,eligible) VALUES(?,?,?,?,?)
                ON CONFLICT(scope,event_id) DO UPDATE SET report=excluded.report,eligible=excluded.eligible
                WHERE report!=excluded.report OR eligible!=excluded.eligible''',
                (key, event, row['episode_id'], json.dumps(row, ensure_ascii=False), int(eligible)))
        # Only incidents eligible for a push need a worker heartbeat. Idle UI polling can be read-only.
        heartbeat = now if any(eligible for _, eligible in cases.values()) else 0
        db.execute('INSERT INTO floppy_monitor VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET '
                   'poll_id=excluded.poll_id,checked_at=excluded.checked_at '
                   'WHERE poll_id!=excluded.poll_id OR checked_at!=excluded.checked_at', (key, poll_id, heartbeat))
        return result


def report(store, offset=0, **args):
    status = reconcile(store, **args)
    if status['state'] != 'ok':
        return status
    with store.db() as db:
        db.execute('BEGIN')
        key = status['scope']
        total = db.execute('SELECT COUNT(*) FROM floppy_cases WHERE scope=?', (key,)).fetchone()[0]
        rows = db.execute('SELECT report FROM floppy_cases WHERE scope=? '
                          "ORDER BY eligible DESC,COALESCE(json_extract(report,'$.last_attempt_at'),0) DESC,event_id LIMIT ? OFFSET ?",
                          (key, PAGE, offset))
        return dict(status, rows=[json.loads(row[0]) for row in rows], total=total, offset=offset,
                    has_more=offset+PAGE < total)


def configure(store, payload, **args):
    if set(payload) != {'enabled', 'scope', 'guard'} or type(payload['enabled']) is not bool:
        raise ValueError('invalid_alert_option')
    status = reconcile(store, **args)
    if (status.get('state') != 'ok' or not status.get('guard') or payload['scope'] != status['scope']
            or payload['guard'] != status['guard']):
        raise RuntimeError('profile_changed')
    with _LOCK, store.db() as db:
        value = dict(guard=status['guard'], scope=status['scope'], at=int(args.get('now') or time.time()))
        if not payload['enabled']:
            value = {}
        elif status['notification_enabled']:
            return {'enabled': True}
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (SETTING,json.dumps(value)))
        db.execute('UPDATE floppy_cases SET eligible=0 WHERE eligible!=0')
    return {'enabled':payload['enabled']}


def watch(store, stop):
    while not stop.is_set():
        try:
            reconcile(store)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        stop.wait(INTERVAL)


def send_pending(admin, cfg, creds, *, now=None, post=None):
    """Called by the single owning worker, which has access to the configured ntfy credential."""
    now = int(time.time() if now is None else now)
    with admin.store.db() as db:
        pref = preference(db)
        if not pref or not creds.get('ntfy_authorization') or not cfg.get('ntfy_url'):
            return 0
        view = admin.manager.public()
        if (notification_guard(view) != pref.get('guard') or cfg['ntfy_url'] != profile(view,'ntfy')['settings']['url']
                or not cfg.get('pinepods',{}).get('enabled')):
            return 0
        scope_key = pref['scope']
        db.execute('BEGIN IMMEDIATE')
        if preference(db) != pref:
            return 0
        rows = db.execute('''SELECT c.event_id FROM floppy_cases c JOIN floppy_monitor m ON m.scope=c.scope
                             WHERE c.scope=? AND c.eligible=1 AND c.notified=0 AND m.checked_at BETWEEN ? AND ?''',
                          (scope_key, now-INTERVAL-15, now)).fetchall()
        if not rows:
            return 0
        import requests
        with requests.Session() as session:
            session.trust_env = False
            send = post or session.post
            text = (f'{len(rows)} offene Podcast-Zustellung(en) nach mindestens zwei Floppy-Abrufen. '
                    'Öffne „Floppy-Zustellungen“ in Books & Audio für Gründe und nächste Schritte.')
            try:
                response = send(cfg['ntfy_url'], headers={'Authorization':creds['ntfy_authorization'],
                                'Title':'Books & Audio', 'Tags':'headphones', 'Priority':'3'},
                                data=text.encode(), timeout=(2,3), allow_redirects=False)
            except requests.RequestException:
                return 0
            if not 200 <= response.status_code < 300:
                return 0
        db.executemany('UPDATE floppy_cases SET notified=1 WHERE scope=? AND event_id=?',
                       [(scope_key,row[0]) for row in rows])
        return len(rows)
