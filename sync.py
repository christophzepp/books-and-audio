"""Personal progress coordinator, with explicit BookOrbit or BookBridge ownership."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet
import requests

from abs_client import ABSClient
from bo_client import BOClient
from core import decide
from mapping import build_mapping, bo_to_canonical, canonical_to_bo, validate_native_readalong
from revision import mapping_fingerprint, mapping_identity_fingerprint, reconcile_manifest_revision
from discovery import discover
import native_discovery

UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def equal(a, b):
    return a['status'] == b['status'] and abs(a['position']-b['position']) <= 1


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid nonnegative position')
    return float(value)


def normalize_abs(raw, mapping):
    if raw is None:
        return dict(position=0, status='not_started', missing=True, fingerprint=mapping['fingerprint'])
    pos = numeric(raw['currentTime'])
    duration = mapping['abs_tracks'][-1]['start'] + mapping['abs_tracks'][-1]['duration']
    if pos > duration + 1 or not isinstance(raw['isFinished'], bool):
        raise ValueError('invalid ABS progress')
    return dict(position=min(pos, duration), status='finished' if raw['isFinished'] else 'in_progress',
                missing=False, fingerprint=mapping['fingerprint'])


def normalize_bo(raw, detail, mapping, allowed_manifest_revisions=()):
    rs = detail.get('readStatus')
    status = rs.get('status') if isinstance(rs, dict) else None
    if status not in (None, 'unread', 'want_to_read', 'reading', 'rereading', 'read'):
        raise ValueError('BookOrbit status requires manual decision')
    pos = 0 if raw is None else bo_to_canonical(mapping, raw['assetId'], raw['positionMs'])
    if raw is not None:
        if (raw.get('manifestRevision') not in {mapping['bo_revision'], *allowed_manifest_revisions}
                or not isinstance(raw.get('revision'), int) or isinstance(raw.get('revision'), bool)):
            raise ValueError('BookOrbit playback manifest/revision changed')
    if status == 'read':
        normalized = 'finished'
    elif status in ('reading', 'rereading') or (status is None and pos > 0):
        normalized = 'in_progress'
    else:
        if pos > 1:
            raise ValueError('unread BookOrbit status with existing audio position')
        normalized = 'not_started'
    return dict(position=pos, status=normalized, missing=raw is None and normalized == 'not_started', playback_missing=raw is None,
                fingerprint=mapping['fingerprint'])


def day_from_ms(value):
    if value is None:
        return None
    from zoneinfo import ZoneInfo
    return dt.datetime.fromtimestamp(value/1000, UTC).astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()


def ms_from_date(value):
    if not value:
        return None
    from zoneinfo import ZoneInfo
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Berlin'))
    return int(parsed.timestamp()*1000)


def discovery_message(issue):
    reason = issue['reason']
    if 'duplicate' in reason or 'overlap' in reason:
        explanation = 'Mehrere Eintraege oder ueberlappende Buchordner verhindern eine eindeutige Zuordnung.'
    elif 'reserved' in reason:
        explanation = 'Dieser Ordner oder diese Datei gehoert bereits zu einer gespeicherten Zuordnung.'
    elif 'compatible' in reason:
        explanation = 'Die Audiodateien oder ihre Laufzeiten stimmen noch nicht vollstaendig ueberein.'
    elif 'reading_status' in reason:
        explanation = 'Der vorhandene Lesestatus braucht eine gesonderte Entscheidung.'
    elif 'scope' in reason:
        explanation = 'Der Eintrag liegt ausserhalb der freigegebenen Hoerbuchbibliothek.'
    elif 'unavailable' in reason or 'missing_or_invalid' in reason:
        explanation = 'Der Eintrag ist noch nicht vollstaendig verfuegbar; die Pruefung wird wiederholt.'
    else:
        explanation = 'Die Bibliotheksangaben konnten nicht vollstaendig geprueft werden; die Pruefung wird wiederholt.'
    return 'Automatische Hoerbuch-Zuordnung zurueckgestellt: ' + issue.get('title', 'Bibliothekspruefung') + ' — ' + explanation


class MappingPaused(RuntimeError):
    """An intentional per-book pause; never a failed synchronization."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Bridge:
    def __init__(self, directory, config, credentials, clock=time.time):
        from io_policy import settings as io_settings
        io_settings(config)
        self.root, self.config, self.clock = Path(directory), config, clock
        self.abs = ABSClient(config['abs_url'], credentials['abs_token'])
        self.provider = config.get('mapping_provider', 'bookorbit')
        if self.provider not in ('bookorbit', 'bookbridge'):
            raise ValueError('Unknown mapping provider')
        self.bo, self.bookbridge = None, None
        if (self.provider == 'bookorbit' and config.get('bo_url')
                and credentials.get('bo_username') and credentials.get('bo_password')):
            self.bo = BOClient(config['bo_url'], credentials['bo_username'], credentials['bo_password'])
            self.bo.folder_config = config
        if ((self.provider == 'bookbridge' or config.get('bookbridge', {}).get('enabled'))
                and all(config.get('bookbridge', {}).get(k) for k in ('url', 'username', 'version'))
                and credentials.get('bookbridge_password')):
            from bookbridge_client import BookBridgeClient
            bb = config['bookbridge']
            self.bookbridge = BookBridgeClient(bb['url'], bb['username'], credentials['bookbridge_password'],
                                               expected_version=bb['version'])
        self.ntfy = credentials.get('ntfy_authorization')
        self.pinepods = None
        if config.get('pinepods', {}).get('enabled'):
            from pinepods_client import PinepodsClient
            pine = config['pinepods']
            self.pinepods = PinepodsClient(pine['url'], pine['username'], credentials['pinepods_password'],
                device_id=pine.get('device_id', 'book-progress-sync'),
                completion_margin_seconds=pine.get('completion_margin_seconds', 60),
                api_key=credentials.get('pinepods_api_key'), server_version=pine.get('server_version', 'unverified'))
        self.external_write_guard = None
        self.jellyfin = None
        if config.get('jellyfin', {}).get('enabled'):
            from jellyfin_client import JellyfinClient
            j = config['jellyfin']
            self.jellyfin = JellyfinClient(j['url'], credentials['jellyfin_token'], j['user_id'], j['username'], j['version'])
        self.db = sqlite3.connect(self.root/'state.sqlite')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, book TEXT NOT NULL, value TEXT NOT NULL, status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, book TEXT, kind TEXT, detail TEXT);
          CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS auto_mappings (id TEXT PRIMARY KEY, bo_id INTEGER UNIQUE NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS mapping_revision_overrides
            (id TEXT PRIMARY KEY, bo_id INTEGER UNIQUE NOT NULL, base_fingerprint TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS mapping_revision_history
            (id TEXT NOT NULL, revision TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(id,revision));
          CREATE TABLE IF NOT EXISTS mapping_revision_evidence
            (id TEXT NOT NULL, revision TEXT NOT NULL, identity_hash TEXT NOT NULL, PRIMARY KEY(id,revision));
          CREATE TABLE IF NOT EXISTS native_readalong_enrollments
            (id TEXT PRIMARY KEY, bo_id INTEGER UNIQUE NOT NULL, proof TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS native_readalong_observations
            (id TEXT PRIMARY KEY, signature TEXT NOT NULL, since REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS native_readalong_witnesses
            (id TEXT PRIMARY KEY, bo_id INTEGER NOT NULL, proof TEXT NOT NULL);
        ''')
        self.db.commit()

    def load(self, id):
        row = self.db.execute('SELECT value FROM state WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, id, value):
        encoded=dump(value)
        current=self.db.execute('SELECT value FROM state WHERE id=?',(id,)).fetchone()
        if current and current[0]==encoded:
            return
        self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (id, encoded))
        self.db.commit()

    def event(self, id, kind, detail):
        self.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)', (now(), id, kind, dump(detail)))
        self.db.commit()
        print(dump(dict(at=now(), book=id, event=kind, detail=detail)), flush=True)

    def alert(self, id, text):
        self.db.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)', (digest([id,text]), text))
        self.db.commit()

    def deliver_notifications(self):
        if not self.ntfy:
            return
        s = requests.Session(); s.trust_env = False
        for id, body in self.db.execute('SELECT id,body FROM notifications LIMIT 5').fetchall():
            try:
                r = s.post(self.config['ntfy_url'], headers={'Authorization': self.ntfy,
                    'Title':'Books & Audio', 'Tags':'books', 'Priority':'3'},
                    data=body.encode(), timeout=(5,10), allow_redirects=False)
                if 200 <= r.status_code < 300:
                    self.db.execute('DELETE FROM notifications WHERE id=?', (id,)); self.db.commit()
            except requests.RequestException:
                break

    def guard(self):
        if getattr(self, 'external_write_guard', None): self.external_write_guard()
        gate = json.loads((self.root/'deployment.json').read_text())
        if 'folder_mappings' in self.config:
            from watch_deployment import validate_folder_aliases
            validate_folder_aliases(self.config, gate, now=self.clock())
        permitted = gate.get('bookbridge_ok') if getattr(self, 'provider', 'bookorbit') == 'bookbridge' else gate.get('ok')
        if permitted is not True or not 0 <= self.clock()-gate.get('checked_at',0) <= 150:
            raise RuntimeError('Deployment- oder BookBridge-Pruefung fehlt/gesperrt')
        if self.abs.version() != self.config['abs_version']:
            raise RuntimeError('Audiobookshelf-Version hat sich geaendert')
        if getattr(self, 'provider', 'bookorbit') == 'bookorbit':
            if getattr(self, 'bookbridge', None) is not None:
                evidence = self.bookbridge.probe()
                if evidence.get('abs_enabled') is not False or evidence.get('bookorbit_enabled') is not False:
                    raise RuntimeError('BookBridge-Fortschrittsübernahme muss im BookOrbit-Modus deaktiviert sein')
            for state_id, value in self.db.execute('SELECT id,value FROM state'):
                if state_id.startswith('_bookbridge_') and json.loads(value).get('pending'):
                    raise RuntimeError('Ein BookBridge-Abgleich muss vor dem Providerwechsel geklärt werden')
        if getattr(self, 'provider', 'bookorbit') == 'bookbridge':
            evidence = self.bookbridge.probe()
            wanted = self.config['bookbridge'].get('expected_abs_username')
            user = self.abs._request('GET', '/api/me', '/api/me')
            if (not wanted or user.get('username') != wanted or evidence.get('abs_username') != wanted
                    or evidence.get('abs_enabled') is not True or evidence.get('bookorbit_enabled') is not False):
                raise RuntimeError('BookBridge muss mit demselben Audiobookshelf-Konto und ohne BookOrbit verbunden sein')
            # Switching providers may not hide a partial old-graph transaction.
            if self.db.execute("SELECT 1 FROM operations WHERE status='pending' AND substr(book,1,9)!='_podcast:' LIMIT 1").fetchone():
                raise RuntimeError('Ungeklaerte Schreibvorgaenge verhindern den Providerwechsel')
            for state_id, value in self.db.execute('SELECT id,value FROM state'):
                if not state_id.startswith(('_bookbridge_', '_podcast:')) and json.loads(value).get('pending'):
                    raise RuntimeError('Ungeklaerte Schreibvorgaenge verhindern den Providerwechsel')

    def _seed_mappings(self):
        mappings = list(self.config['mappings'])
        for id, bo_id, value in self.db.execute('SELECT id,bo_id,value FROM auto_mappings ORDER BY id'):
            mapping = json.loads(value)
            if mapping['abs_id'] != id or mapping['bo_id'] != bo_id:
                raise RuntimeError('Gespeicherte automatische Zuordnung ist widerspruechlich')
            mappings.append(mapping)
        if (len({m['abs_id'] for m in mappings}) != len(mappings)
                or len({m['bo_id'] for m in mappings}) != len(mappings)):
            raise RuntimeError('Mehrdeutige gespeicherte Hoerbuch-Zuordnung')
        return mappings

    def paused_mappings(self, mappings=None):
        """Validate pauses against all reserved identities, including auto-maps.

        Configuration is loaded at process startup. A deployment must restart
        the service after changing it; every write still rechecks this loaded
        configuration so callers cannot bypass the pause via apply().
        """
        pauses = self.config.get('paused_mappings', {})
        if not isinstance(pauses, dict):
            raise ValueError('paused_mappings muss ein Objekt aus Buch-ID und Pausengrund sein')
        known = {m['abs_id'] for m in (self._seed_mappings() if mappings is None else mappings)}
        for id, reason in pauses.items():
            if not isinstance(id, str) or not id or id != id.strip() or not id.isprintable():
                raise ValueError('paused_mappings enthaelt eine ungueltige Buch-ID')
            if id not in known:
                raise ValueError('paused_mappings enthaelt eine nicht zugeordnete Buch-ID')
            if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
                    or not reason.isprintable()):
                raise ValueError('paused_mappings braucht einen lesbaren, nicht leeren Pausengrund')
        return dict(pauses)

    def require_active_mapping(self, id):
        reason = self.paused_mappings().get(id)
        if reason is not None:
            raise MappingPaused(reason)

    def native_readalong(self, mappings=None):
        """Explicit existing-pair exceptions only; discovery never gets these pins."""
        pins = self.config.get('native_readalong', {})
        if not isinstance(pins, dict):
            raise ValueError('native_readalong muss ein Objekt aus vorhandener Buch-ID und EPUB-Freigabe sein')
        known = {m['abs_id']: m for m in (self._seed_mappings() if mappings is None else mappings)}
        checked = {}
        for id, pin in pins.items():
            if (not isinstance(id, str) or not id or id != id.strip() or not id.isprintable()
                    or id not in known):
                raise ValueError('native_readalong enthaelt eine ungueltige oder nicht zugeordnete Buch-ID')
            mapping = known[id]
            validated = validate_native_readalong(pin, mapping['bo_id'], mapping['folder'], folder_config=self.config)
            row = self.db.execute('SELECT bo_id,proof FROM native_readalong_enrollments WHERE id=?', (id,)).fetchone()
            if row and row[0] == mapping['bo_id']:
                proof = json.loads(row[1])
                provenance = proof.get('sha256_evidence', {}) if isinstance(proof, dict) else {}
                if (native_discovery._strong(proof) and provenance.get('kind') == 'user-review'
                        and provenance.get('superseded_pin_sha256') == native_discovery.signature(validated)
                        and proof.get('bo_id') == mapping['bo_id']
                        and proof.get('audio_identity') == mapping_identity_fingerprint(mapping)):
                    for role in ('source', 'overlay'):
                        validate_native_readalong(proof[role]['pin'], mapping['bo_id'], mapping['folder'], folder_config=self.config)
                    native_discovery._origin(proof, proof)
                    # This exact configured exception was explicitly superseded.
                    # Ordinary collect() still validates current files every cycle.
                    continue
            checked[id] = validated
        return checked

    def paused_result(self, mapping, reason):
        count = self.db.execute("SELECT COUNT(*) FROM operations WHERE book=? AND status='pending'",
                                (mapping['abs_id'],)).fetchone()[0]
        return {'abs_id': mapping['abs_id'], 'bo_id': mapping['bo_id'],
                'title': mapping['title'], 'reason': reason,
                'pending_operations': count, 'requires_review': count > 0}

    def list_mappings(self):
        if getattr(self, 'provider', 'bookorbit') == 'bookbridge':
            return list(self.load('_bookbridge_mapping_inventory').get('mappings', []))
        mappings = self._seed_mappings()
        self.paused_mappings(mappings)
        self.native_readalong(mappings)
        by_id = {m['abs_id']: m for m in mappings}
        for id, bo_id, base_fp, value in self.db.execute(
                'SELECT id,bo_id,base_fingerprint,value FROM mapping_revision_overrides'):
            seed = by_id.get(id)
            updated = json.loads(value)
            if (seed is None or seed['bo_id'] != bo_id or updated.get('abs_id') != id
                    or updated.get('bo_id') != bo_id or seed['fingerprint'] != base_fp):
                raise RuntimeError('Gespeicherte Manifestrevision passt nicht mehr zur urspruenglichen Zuordnung')
            by_id[id] = reconcile_manifest_revision(seed, updated)['mapping']
        return [by_id[m['abs_id']] for m in mappings]

    def refresh_mappings(self, apply):
        mappings = self.list_mappings()
        settings = self.config.get('auto_discovery', {})
        if not settings.get('enabled'):
            return mappings, {'enabled': False}
        interval = numeric(settings.get('interval_seconds', 300))
        stable = numeric(settings.get('stable_seconds', 300))
        if interval < 60 or stable < 300:
            raise RuntimeError('Automatische Zuordnung braucht mindestens 300 Sekunden Stabilitaet')
        previous = self.load('_discovery')
        timestamp = self.clock()
        elapsed = timestamp - previous.get('last_scan_at', 0)
        if apply and previous.get('last_scan_at') is not None and 0 <= elapsed < interval:
            return mappings, dict(previous.get('summary', {}), enabled=True, scanned=False)
        try:
            native_enabled, native_stable = native_discovery.settings(self.config)
            result = discover(self.abs, self.bo, mappings,
                settings['abs_library_id'], settings['bo_library_ids'],
                previous.get('candidates', {}), timestamp,
                stable_seconds=max(stable, native_stable) if native_enabled else stable,
                native_readalong_enabled=native_enabled,
                native_witnesses=previous.get('native_witnesses', {}), folder_config=self.config)
        except Exception as exc:
            # Do not leak response bodies, exception messages or credentials.
            result = {'scan_ok': False, 'new_mappings': [], 'candidates': {},
                      'issues': [{'key': 'scan', 'reason': 'Bibliotheksabfrage fehlgeschlagen (' + type(exc).__name__ + ')'}],
                      'summary': {}}
        new = result['new_mappings'] if result['scan_ok'] else []
        summary = dict(result['summary'], enabled=True, scanned=True,
                       scan_ok=result['scan_ok'], added=len(new))
        if not apply:
            return mappings + new, summary
        self.guard()
        # Commit each new identity together with the discovery observation,
        # event and notification. A restart can never lose its registration.
        old_issues = {x['key']: x['reason'] for x in previous.get('issues', [])}
        state = {'last_scan_at': timestamp, 'candidates': result['candidates'] if result['scan_ok'] else {},
                 'native_witnesses': result.get('native_witnesses', previous.get('native_witnesses', {})),
                 'issues': result['issues'], 'summary': summary}
        with self.db:
            for mapping in new:
                if (any(m['abs_id'] == mapping['abs_id'] or m['bo_id'] == mapping['bo_id'] for m in mappings)
                        or self.load(mapping['abs_id'])
                        or self.db.execute('SELECT 1 FROM operations WHERE book=?', (mapping['abs_id'],)).fetchone()):
                    raise RuntimeError('Neue Zuordnung kollidiert mit bestehendem Verlauf')
                self.db.execute('INSERT INTO auto_mappings VALUES (?,?,?)',
                                (mapping['abs_id'], mapping['bo_id'], dump(mapping)))
                native_proof = result.get('native_readalongs', {}).get(mapping['abs_id'])
                if native_proof is not None:
                    self.db.execute('INSERT INTO native_readalong_enrollments VALUES (?,?,?)',
                                    (mapping['abs_id'], mapping['bo_id'], dump(native_proof)))
                mappings.append(mapping)
                detail = {'title': mapping['title'], 'bo_id': mapping['bo_id'], 'fingerprint': mapping['fingerprint']}
                self.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)',
                                (now(), mapping['abs_id'], 'auto_mapped', dump(detail)))
                message = 'Hoerbuch automatisch zugeordnet: ' + mapping['title'] + '. Fuer den Abgleich von Hoerposition und Status aufgenommen.'
                self.db.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',
                                (digest(['auto_mapped', mapping['abs_id'], mapping['fingerprint']]), message))
            for issue in result['issues']:
                if old_issues.get(issue['key']) != issue['reason']:
                    message = discovery_message(issue)
                    self.db.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',
                                    (digest(['discovery', issue['key'], issue['reason']]), message))
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('_discovery', dump(state)))
        self._new_native_pairs = set(result.get('native_readalongs', {})) if result['scan_ok'] else set()
        return mappings, summary

    def _read_mapping(self, expected):
        self.require_active_mapping(expected['abs_id'])
        pin = self.native_readalong().get(expected['abs_id'])
        ai = self.abs.item(expected['abs_id'])
        bd = self.bo.detail(expected['bo_id'])
        bm = self.bo.manifest(expected['bo_id'])
        enabled, _ = native_discovery.settings(self.config)
        if pin is None and enabled and self._has_native_candidate(bd):
            accepted = self.db.execute('SELECT bo_id,proof FROM native_readalong_enrollments WHERE id=?',
                                       (expected['abs_id'],)).fetchone()
            trusted = json.loads(accepted[1]) if accepted and accepted[0] == expected['bo_id'] else None
            proof, current = native_discovery.collect(self.bo, expected, ai, bd, bm, trusted=trusted, cache_ok=True)
            if not accepted or accepted != (expected['bo_id'], dump(proof)):
                raise ValueError('Native read-along has not completed verified enrollment')
            return current, bd
        m = build_mapping(ai, bd, bm, native_readalong=pin, folder_config=self.config)
        return m, bd

    @staticmethod
    def _has_native_candidate(detail):
        return ((detail.get('readAloudSync') or {}).get('state') == 'enabled'
                or any(str(f.get('format', '')).lower() == 'epub' for f in detail.get('files', [])))

    def prepare_native_readalong(self, expected, apply):
        enabled, stable = native_discovery.settings(self.config)
        id = expected['abs_id']
        if not enabled or id in self.native_readalong():
            return
        self.require_active_mapping(id)
        try:
            ai = self.abs.item(id)
            bd = self.bo.detail(expected['bo_id'])
            if not self._has_native_candidate(bd):
                if apply:
                    with self.db:
                        self.db.execute('DELETE FROM native_readalong_observations WHERE id=?', (id,))
                return
            bm = self.bo.manifest(expected['bo_id'])
            accepted = self.db.execute('SELECT bo_id,proof FROM native_readalong_enrollments WHERE id=?', (id,)).fetchone()
            pending_witness = self.db.execute('SELECT bo_id,proof FROM native_readalong_witnesses WHERE id=?', (id,)).fetchone()
            witness = pending_witness or accepted
            trusted = json.loads(witness[1]) if witness and witness[0] == expected['bo_id'] else None
            proof, _ = native_discovery.collect(self.bo, expected, ai, bd, bm, trusted=trusted,
                                                 cache_ok=pending_witness is None and accepted is not None)
            encoded = dump(proof)
            if accepted == (expected['bo_id'], encoded):
                return
            if accepted is not None:
                old_proof = json.loads(accepted[1])
                if (old_proof['job_id'] == proof['job_id']
                        and any(old_proof[role]['file_hash'] != proof[role]['file_hash']
                                or old_proof[role]['pin']['overlay_size'] != proof[role]['pin']['overlay_size']
                                for role in ('source', 'overlay'))):
                    raise ValueError('Changed native content requires a new completed generation job')
            if self.db.execute("SELECT 1 FROM operations WHERE book=? AND status='pending'", (id,)).fetchone():
                raise RuntimeError('Pending operation prevents native read-along enrollment')
            stored = self.load(id)
            # Validate a present baseline without replacing it or adopting live progress.
            self._revision_state(stored, expected, expected)
            timestamp = numeric(self.clock())
            fingerprint = native_discovery.signature(proof)
            observation = self.db.execute('SELECT signature,since FROM native_readalong_observations WHERE id=?', (id,)).fetchone()
            same = observation is not None and observation[0] == fingerprint and 0 <= timestamp - observation[1]
            since = observation[1] if same else timestamp
            if not same or timestamp - since < stable:
                if apply:
                    self.guard()
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO native_readalong_observations VALUES (?,?,?)',
                                        (id, fingerprint, since))
                        self.db.execute('INSERT OR REPLACE INTO native_readalong_witnesses VALUES (?,?,?)',
                                        (id, expected['bo_id'], encoded))
                raise native_discovery.NativeEnrollmentPending('Native read-along awaits a second stable observation')
            if not apply:
                raise native_discovery.NativeEnrollmentPending('Verified native read-along enrollment is proposed')
            self.guard()
            # Re-read every identity immediately before the local-only acceptance.
            fresh, _ = native_discovery.collect(self.bo, expected, self.abs.item(id),
                                                self.bo.detail(expected['bo_id']), self.bo.manifest(expected['bo_id']), trusted=proof)
            if fresh != proof:
                raise ValueError('Native read-along changed during enrollment')
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                current = next((m for m in self.list_mappings() if m['abs_id'] == id), None)
                if (current != expected or self.load(id) != stored
                        or self.db.execute("SELECT 1 FROM operations WHERE book=? AND status='pending'", (id,)).fetchone()):
                    raise RuntimeError('Sync state changed during native read-along enrollment')
                self.db.execute('INSERT OR REPLACE INTO native_readalong_enrollments VALUES (?,?,?)',
                                (id, expected['bo_id'], encoded))
                self.db.execute('DELETE FROM native_readalong_observations WHERE id=?', (id,))
                self.db.execute('DELETE FROM native_readalong_witnesses WHERE id=?', (id,))
                self.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)',
                                (now(), id, 'native_readalong_enrolled', dump({'proof': proof, 'progress_unchanged': True})))
            # A separate cycle performs ordinary conflict/settling checks. Enrollment
            # never creates a baseline, replay, initialization or progress write.
            raise native_discovery.NativeEnrollmentPending('Native read-along enrolled; progress remains unchanged')
        except native_discovery.NativeEnrollmentPending:
            raise
        except Exception:
            if apply:
                with self.db:
                    self.db.execute('DELETE FROM native_readalong_observations WHERE id=?', (id,))
            raise

    def _read_progress(self, mapping, detail, extra_revisions=(), history_mapping=None):
        ar = self.abs.progress(mapping['abs_id']); br = self.bo.playback(mapping['bo_id'])
        allowed = set(extra_revisions)
        if br is not None and br.get('manifestRevision') != mapping['bo_revision']:
            # Playback rows can legitimately retain a previous manifest revision.
            # Accept only an individually verified revision of this unchanged
            # mapping, never an arbitrary old revision returned by the server.
            history_mapping = history_mapping or mapping
            row = self.db.execute('SELECT value FROM mapping_revision_overrides WHERE id=?',
                                  (history_mapping['abs_id'],)).fetchone()
            if row and json.loads(row[0])['fingerprint'] == history_mapping['fingerprint']:
                known = self.db.execute(
                    '''SELECT h.fingerprint,e.identity_hash FROM mapping_revision_history AS h
                       LEFT JOIN mapping_revision_evidence AS e ON h.id=e.id AND h.revision=e.revision
                       WHERE h.id=? AND h.revision=?''',
                    (mapping['abs_id'], br.get('manifestRevision'))).fetchone()
                if known:
                    historical = dict(mapping, bo_revision=br['manifestRevision'])
                    if (known[0] != mapping_fingerprint(historical)
                            or known[1] != mapping_identity_fingerprint(mapping)):
                        raise RuntimeError('Historische Manifestrevision gehoert zu einer anderen Dateizuordnung')
                    allowed.add(br['manifestRevision'])
        a = normalize_abs(ar, mapping)
        b = normalize_bo(br, detail, mapping, allowed)
        raw = {'abs': ar, 'bo': br, 'bo_status': detail.get('readStatus')}
        return {'abs': a, 'bo': b}, raw

    def read_pair(self, expected):
        m, bd = self._read_mapping(expected)
        if m['fingerprint'] != expected['fingerprint']:
            raise RuntimeError('Dateizuordnung oder Manifest hat sich geaendert')
        if 'bo_assets' in expected or 'bo_chapters' in expected:
            reconcile_manifest_revision(expected, m)
        return self._read_progress(expected, bd)

    @staticmethod
    def _revision_state(stored, previous, updated):
        state = json.loads(dump(stored))
        if 'baseline' in state:
            baseline = state['baseline']
            if not isinstance(baseline, dict) or set(baseline) != {'abs', 'bo'}:
                raise RuntimeError('Unvollstaendige Baseline verhindert Manifestuebernahme')
            for side in ('abs', 'bo'):
                if (not isinstance(baseline[side], dict)
                        or baseline[side].get('fingerprint') != previous['fingerprint']):
                    raise RuntimeError('Baseline gehoert zu einer anderen Dateizuordnung')
                baseline[side]['fingerprint'] = updated['fingerprint']
        # A revision change restarts the normal progress settling period.
        state.pop('candidate', None)
        return state

    def _jellyfin_revision_state(self, stored, updated, previous=None):
        """Refresh only proven ABS/BO revision fingerprints in a JF baseline.

        Jellyfin's own track fingerprint and all playback/history values stay
        unchanged. A subsequent normal bind/read still rejects changed JF
        tracks. Older baselines require independently preserved revision proof;
        a matching legacy fingerprint alone cannot prove unchanged media.
        """
        if not isinstance(stored, dict):
            raise RuntimeError('Jellyfin-Baseline ist unvollstaendig')
        state = json.loads(dump(stored))
        if 'baseline' not in state:
            return state
        baseline = state['baseline']
        if (not isinstance(baseline, dict) or set(baseline) != {'abs', 'bo', 'jf'}
                or any(not isinstance(value, dict) for value in baseline.values())):
            raise RuntimeError('Jellyfin-Baseline ist unvollstaendig')
        old = baseline['abs'].get('fingerprint')
        if (not isinstance(old, str) or len(old) != 64 or any(c not in '0123456789abcdef' for c in old)
                or baseline['bo'].get('fingerprint') != old):
            raise RuntimeError('Jellyfin-Baseline gehoert zu verschiedenen Dateizuordnungen')
        if old == updated['fingerprint']:
            return state
        if stored.get('pending'):
            raise RuntimeError('Offene Jellyfin-Uebernahme verhindert Manifestwechsel')
        identity = mapping_identity_fingerprint(updated)
        if previous is not None and old == previous['fingerprint']:
            if (mapping_fingerprint(previous) != old
                    or mapping_identity_fingerprint(previous) != identity):
                raise RuntimeError('Jellyfin-Baseline hat andere Dateievidenz')
        else:
            history = self.db.execute(
                '''SELECT h.revision,e.identity_hash FROM mapping_revision_history AS h
                   LEFT JOIN mapping_revision_evidence AS e ON h.id=e.id AND h.revision=e.revision
                   WHERE h.id=? AND h.fingerprint=?''', (updated['abs_id'], old)).fetchall()
            if (len(history) != 1 or history[0][1] != identity
                    or mapping_fingerprint(dict(updated, bo_revision=history[0][0])) != old):
                raise RuntimeError('Jellyfin-Baseline hat keine bestaetigte historische Dateievidenz')
        for side in ('abs', 'bo'):
            baseline[side]['fingerprint'] = updated['fingerprint']
        state.pop('candidate', None)
        return state

    def _jellyfin_revision_snapshot(self, updated, previous=None):
        key = '_jellyfin_audio:' + updated['abs_id']
        row = self.db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
        raw = row[0] if row else None
        stored = json.loads(raw) if raw is not None else {}
        return raw, self._jellyfin_revision_state(stored, updated, previous)

    def _revision_idle(self, id):
        key = '_jellyfin_audio:' + id
        if (self.db.execute("SELECT 1 FROM operations WHERE book IN (?,?) AND status='pending' LIMIT 1",
                            (id, key)).fetchone()
                or self.load(key).get('pending')):
            raise RuntimeError('Offene Uebernahme verhindert Manifestwechsel')

    def _save_jellyfin_revision(self, mapping, before, updated):
        if before is None or json.loads(before) == updated:
            return
        self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                        ('_jellyfin_audio:' + mapping['abs_id'], dump(updated)))
        detail = dict(title=mapping['title'], old_fingerprint=json.loads(before)['baseline']['abs']['fingerprint'],
                      new_fingerprint=mapping['fingerprint'], progress_unchanged=True)
        self.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)',
                        (now(), mapping['abs_id'], 'jellyfin_revision_baseline_updated', dump(detail)))

    def _refresh_jellyfin_revision(self, mapping, core, apply):
        before, updated = self._jellyfin_revision_snapshot(mapping)
        if before is None or json.loads(before) == updated:
            return mapping
        self._revision_idle(mapping['abs_id'])
        if not apply:
            return dict(mapping, _jellyfin_revision_preview=updated)
        self.guard()
        self.require_active_mapping(mapping['abs_id'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._revision_idle(mapping['abs_id'])
            current = next((m for m in self.list_mappings() if m['abs_id'] == mapping['abs_id']), None)
            row = self.db.execute('SELECT value FROM state WHERE id=?',
                                  ('_jellyfin_audio:' + mapping['abs_id'],)).fetchone()
            if (current is None or current['fingerprint'] != mapping['fingerprint']
                    or self.load(mapping['abs_id']) != core or row != (before,)
                    or self._jellyfin_revision_state(json.loads(before), mapping) != updated):
                raise RuntimeError('Jellyfin-Baseline wurde waehrend der Manifestuebernahme geaendert')
            self._save_jellyfin_revision(mapping, before, updated)
        return mapping

    def _commit_revision(self, previous, updated, before_state, after_state, before_jellyfin):
        id = previous['abs_id']
        self.require_active_mapping(id)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._revision_idle(id)
            current = next((m for m in self.list_mappings() if m['abs_id'] == id), None)
            if current is None or current['fingerprint'] != previous['fingerprint'] or self.load(id) != before_state:
                raise RuntimeError('Lokaler Sync-Zustand wurde waehrend der Manifestuebernahme geaendert')
            row = self.db.execute('SELECT value FROM state WHERE id=?', ('_jellyfin_audio:' + id,)).fetchone()
            if (row[0] if row else None) != before_jellyfin:
                raise RuntimeError('Jellyfin-Baseline wurde waehrend der Manifestuebernahme geaendert')
            jf_state = self._jellyfin_revision_state(json.loads(before_jellyfin) if before_jellyfin is not None else {},
                                                     updated, previous)
            seed = next(m for m in self._seed_mappings() if m['abs_id'] == id)
            self.db.execute('INSERT OR REPLACE INTO mapping_revision_overrides VALUES (?,?,?,?)',
                            (id, updated['bo_id'], seed['fingerprint'], dump(updated)))
            for mapping in (previous, updated):
                known = self.db.execute('SELECT fingerprint FROM mapping_revision_history WHERE id=? AND revision=?',
                                        (id, mapping['bo_revision'])).fetchone()
                if known and known[0] != mapping['fingerprint']:
                    raise RuntimeError('Historische Manifestrevision ist widerspruechlich')
                self.db.execute('INSERT OR IGNORE INTO mapping_revision_history VALUES (?,?,?)',
                                (id, mapping['bo_revision'], mapping['fingerprint']))
                identity_hash = mapping_identity_fingerprint(mapping)
                proof = self.db.execute('SELECT identity_hash FROM mapping_revision_evidence WHERE id=? AND revision=?',
                                        (id, mapping['bo_revision'])).fetchone()
                if proof and proof[0] != identity_hash:
                    raise RuntimeError('Historische Manifestrevision hat andere Dateievidenz')
                self.db.execute('INSERT OR IGNORE INTO mapping_revision_evidence VALUES (?,?,?)',
                                (id, mapping['bo_revision'], identity_hash))
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (id, dump(after_state)))
            self._save_jellyfin_revision(updated, before_jellyfin, jf_state)
            detail = {'title': updated['title'], 'old_revision': previous['bo_revision'],
                      'new_revision': updated['bo_revision'], 'old_fingerprint': previous['fingerprint'],
                      'new_fingerprint': updated['fingerprint'], 'progress_unchanged': True}
            self.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)',
                            (now(), id, 'manifest_revision_updated', dump(detail)))

    def prepare_pair(self, expected, stored, apply):
        # Preview evidence is local to this call, never persisted or trusted
        # from a configuration/previous mapping supplied to the apply path.
        expected = {key: value for key, value in expected.items() if key != '_jellyfin_revision_preview'}
        current, detail = self._read_mapping(expected)
        if current['fingerprint'] == expected['fingerprint']:
            if 'bo_assets' in expected or 'bo_chapters' in expected:
                reconcile_manifest_revision(expected, current)
            pair, raw = self._read_progress(expected, detail)
            expected = self._refresh_jellyfin_revision(expected, stored, apply)
            return expected, stored, pair, raw, False
        report = reconcile_manifest_revision(expected, current)
        self._revision_idle(expected['abs_id'])
        updated = report['mapping']
        state = self._revision_state(stored, expected, updated)
        before_jellyfin, jf_state = self._jellyfin_revision_snapshot(updated, expected)
        # Validate the actual playback row before recording the revision change.
        # Its old manifest can be the immediately previous or an already known
        # revision. There is no synthetic playback write just to refresh it.
        pair, raw = self._read_progress(updated, detail, {expected['bo_revision']}, history_mapping=expected)
        if apply:
            self.guard()
            self._commit_revision(expected, updated, stored, state, before_jellyfin)
        elif before_jellyfin is not None and json.loads(before_jellyfin) != jf_state:
            updated = dict(updated, _jellyfin_revision_preview=jf_state)
        return updated, state, pair, raw, True

    def write_record(self, oid, id, record, status='pending'):
        self.db.execute('INSERT OR REPLACE INTO operations VALUES (?,?,?,?)', (oid,id,dump(record),status))
        self.db.commit()

    def apply(self, mapping, pair, raw, direction, baseline):
        id = mapping['abs_id']; source,target = ('abs','bo') if direction=='abs_to_bo' else ('bo','abs')
        self.require_active_mapping(id)
        self.native_readalong()
        native_discovery.settings(self.config)
        goal = pair[source]
        if goal['status']=='not_started':
            raise RuntimeError('Zuruecksetzen auf ungelesen wird nicht automatisch ausgefuehrt')
        abs_finish = None
        if target=='abs' and goal['status']=='finished':
            abs_finish = ms_from_date((raw.get('bo_status') or {}).get('finishedAt'))
            if abs_finish is None and pair['abs']['status']=='finished':
                abs_finish = (raw['abs'] or {}).get('finishedAt')
            if abs_finish is None:
                raise RuntimeError('Abschlussdatum fehlt; keine erfundene Abschlusszeit uebertragen')
        if source=='abs':
            lib = self.bo.library(mapping['bo_library_id'])
            threshold = lib.get('markAsFinishedPercentComplete')
            threshold = 98 if threshold is None else numeric(threshold)
            asset,local = canonical_to_bo(mapping,goal['position'])
            bt = next(t for t in mapping['bo_tracks'] if t['id']==asset)
            total = mapping['bo_tracks'][-1]['start']+mapping['bo_tracks'][-1]['duration']
            percentage = (bt['start']+local/1000)/total*100
            if goal['status']!='finished' and percentage >= threshold:
                raise RuntimeError('BookOrbit wuerde diesen noch nicht abgeschlossenen Titel automatisch abschliessen')
            if baseline is None and pair[target]['status']=='finished' and goal['status']!='finished':
                raise RuntimeError('Erstbestand darf kein erneutes Hoeren ausloesen')
        self.guard()
        fresh,fr = self.read_pair(mapping)
        if digest(fr)!=digest(raw):
            raise RuntimeError('Fortschritt wurde unmittelbar vor der Uebernahme geaendert')
        oid=str(uuid.uuid4())
        record={'at':now(),'direction':direction,'before':raw,'before_state':pair,
                'goal':goal,'stage':'prepared','mapping':mapping['fingerprint']}
        self.write_record(oid,id,record)
        latest = raw

        def checked_read():
            current,current_raw=self.read_pair(mapping)
            if digest(current_raw)!=digest(latest):
                raise RuntimeError('Parallele Aenderung zwischen Teilschritten')
            return current

        def step(label,fn,expected_position,allowed_statuses):
            nonlocal latest
            self.require_active_mapping(id)
            self.guard()
            current,current_raw=self.read_pair(mapping)
            # The entire last observed source and target must still match.
            if digest(current_raw)!=digest(latest):
                raise RuntimeError('Parallele Aenderung waehrend der Uebernahme')
            record['stage']='sending_'+label; record['last_verified']=latest
            self.write_record(oid,id,record)
            # Last gate immediately before remote I/O, also after journalling.
            self.require_active_mapping(id)
            fn(current_raw)
            current,latest=self.read_pair(mapping)
            if not equal(current[source],goal):
                raise RuntimeError('Quelle wurde waehrend der Uebernahme geaendert')
            if abs(current[target]['position']-expected_position)>1 or current[target]['status'] not in allowed_statuses:
                raise RuntimeError('Zielwirkung des Teilschritts nicht bestaetigt')
            record['stage']='verified_'+label;record['last_verified']=latest
            self.write_record(oid,id,record)
            return current

        if target=='bo':
            asset,local=canonical_to_bo(mapping,goal['position'])
            if raw['bo'] is None or abs(pair[target]['position']-goal['position'])>1:
                step('position', lambda r:self.bo.write_position(mapping['bo_id'],asset,local,
                     0 if r['bo'] is None else r['bo']['revision'],mapping['bo_revision'],oid,now()),
                     goal['position'], ({'in_progress','finished'} if goal['status']=='finished' or pair[target]['status']=='finished' else {'in_progress'}) | ({'not_started'} if goal['position']==0 else set()))
            current=checked_read()
            # Correct first-import dates, and explicit status, without re-writing an unchanged reread.
            initial = raw['bo_status'] is None
            if current['bo']['status']!=goal['status'] or initial:
                previous_status=raw.get('bo_status') or {}
                started = day_from_ms(raw['abs'].get('startedAt')) or previous_status.get('startedAt')
                finished = (day_from_ms(raw['abs'].get('finishedAt')) or previous_status.get('finishedAt')) if goal['status']=='finished' else None
                wanted='read' if goal['status']=='finished' else 'reading'
                step('status',lambda r:self.bo.write_status(mapping['bo_id'],wanted,started_at=started,finished_at=finished),goal['position'],{goal['status']})
        else:
            total=mapping['abs_tracks'][-1]['start']+mapping['abs_tracks'][-1]['duration']
            finished=abs_finish
            if raw['abs'] and raw['abs']['isFinished'] and goal['status']!='finished':
                step('reopen',lambda r:self.abs.write_finished(id,False),0,{'in_progress'})
            current=checked_read()
            if current['abs']['missing'] or abs(current['abs']['position']-goal['position'])>1:
                step('position',lambda r:self.abs.write_position(id,goal['position'],total),goal['position'],{'in_progress'})
            current=checked_read()
            if current['abs']['status']!=goal['status']:
                step('status',lambda r:self.abs.write_finished(id,goal['status']=='finished',finished_at=finished),goal['position'],{goal['status']})
        final,after=self.read_pair(mapping)
        if not equal(final['abs'],goal) or not equal(final['bo'],goal):
            raise RuntimeError('Uebernahme nach Schreiben nicht bestaetigt')
        record.update(stage='complete',after=after)
        self.write_record(oid,id,record,'complete')
        self.event(id,'synced',{'title':mapping['title'],'direction':direction,'seconds':round(goal['position'],3),'status':goal['status']})
        return final

    def cycle(self, apply=False, settle=30):
        if getattr(self, 'provider', 'bookorbit') == 'bookbridge':
            from provider_runtime import BookBridgeCoordinator
            return BookBridgeCoordinator(self).cycle(apply, settle)
        # Invalid/unknown pause entries fail before any application API call.
        self.paused_mappings()
        self.native_readalong()
        native_discovery.settings(self.config)
        self.guard()
        if apply:self.save('_global_error',{})
        self._new_native_pairs = set()
        mappings, discovery = self.refresh_mappings(apply)
        result={'at':now(),'mode':'apply' if apply else 'dry-run','mapped':len(mappings),
                'synced':0,'unchanged':0,'waiting':0,'blocked':[],'paused':[],'proposed':[],
                'revisions_updated':0,'revisions_proposed':0}
        if discovery.get('enabled'):
            result['discovery'] = discovery
        jf_audio = None
        if self.jellyfin:
            from jellyfin_audio import AudioCoordinator
            jf_audio = AudioCoordinator(self, self.jellyfin)
            result['jellyfin_audio'] = {'matched':0,'awaiting_index':[]}
        for mapping in mappings:
            if getattr(self,"serve_preview",None):self.serve_preview()
            id=mapping['abs_id']; stored=self.load(id)
            try:
                self.require_active_mapping(id)
                if id in self._new_native_pairs:
                    raise native_discovery.NativeEnrollmentPending('New native audio pair registered; progress remains unchanged')
                self.prepare_native_readalong(mapping, apply)
                mapping,stored,pair,raw,revised = self.prepare_pair(mapping,stored,apply)
                if revised:
                    result['revisions_updated' if apply else 'revisions_proposed'] += 1
                old_bo=(stored.get('baseline') or {}).get('bo',{})
                if raw['bo'] is None and old_bo.get('playback_missing') is False:
                    raise RuntimeError('BookOrbit-Audioposition ist verschwunden; kein Zuruecksetzen')
                if raw['bo'] is None and pair['bo']['status']!='not_started' and pair['abs']['position']>1:
                    raise RuntimeError('BookOrbit-Status hat keine zuordenbare Audioposition')
                pending=self.db.execute("SELECT id,value FROM operations WHERE book=? AND status='pending'",(id,)).fetchall()
                if pending:
                    # Never replay an uncertain write. Only accept a fully reached goal.
                    if len(pending)!=1:
                        raise RuntimeError('Mehrere ungeklaerte Schreibvorgaenge')
                    oid,rec=pending[0];rec=json.loads(rec)
                    if equal(pair['abs'],rec['goal']) and equal(pair['bo'],rec['goal']):
                        if apply:
                            rec.update(stage='reconciled',after=raw);self.write_record(oid,id,rec,'complete')
                            stored={'baseline':pair};self.save(id,stored)
                    else:
                        raise RuntimeError('Unterbrochene Uebernahme muss anhand des Journals geprueft werden')
                if jf_audio:
                    jf_action = jf_audio.handle(mapping,pair,raw,apply,settle)
                    if jf_action:
                        result['jellyfin_audio']['matched'] += 1
                        if jf_action == 'proposed': result['proposed'].append({'title':mapping['title'],'direction':'jellyfin_three_way'})
                        else: result[jf_action] += 1
                        continue
                    result['jellyfin_audio']['awaiting_index'].append(mapping['title'])
                decision=decide(pair['abs'],pair['bo'],stored.get('baseline'))
                action=decision['action']
                if action=='conflict':
                    raise RuntimeError(decision['reason'])
                if action=='baseline':
                    result['unchanged']+=1
                    if apply:self.save(id,{'baseline':pair})
                    continue
                proposal={'title':mapping['title'],'direction':action,'abs':pair['abs'],'bo':pair['bo']}
                result['proposed'].append(proposal)
                if not apply:continue
                signature=digest(raw)
                candidate=stored.get('candidate',{})
                if candidate.get('signature')!=signature:
                    candidate={'signature':signature,'since':self.clock()}
                    stored['candidate']=candidate;self.save(id,stored)
                if self.clock()-candidate['since']<settle:
                    result['waiting']+=1;continue
                final=self.apply(mapping,pair,raw,action,stored.get('baseline'))
                self.save(id,{'baseline':final});result['synced']+=1
            except native_discovery.NativeEnrollmentPending as exc:
                result['waiting'] += 1
                result.setdefault('native_readalong', []).append({'title': mapping['title'], 'reason': str(exc)})
            except MappingPaused as exc:
                # Preserve the entire baseline/candidate/journal, including a
                # partial operation. A pause neither reconciles nor discards it.
                result['paused'].append(self.paused_result(mapping, exc.reason))
            except Exception as exc:
                # Client exceptions contain only sanitized method/path/status; no body or credentials.
                error=str(exc) if isinstance(exc,(RuntimeError,ValueError)) or type(exc).__name__=='RequestError' else type(exc).__name__
                issue={'title':mapping['title'],'reason':error};result['blocked'].append(issue)
                if apply and stored.get('last_error')!=error:
                    stored['last_error']=error;self.save(id,stored)
                    self.event(id,'blocked',issue)
                    self.alert(id,'Hoerbuch-Sync zurueckgestellt: '+mapping['title']+' — '+error)
        if self.jellyfin and (self.config['jellyfin'].get('ebooks') or self.config['jellyfin'].get('auto_ebooks')):
            from jellyfin_ebook import EbookCoordinator
            result['ebooks']=EbookCoordinator(self).cycle(apply,settle)
        (self.root/'last-run.json.tmp').write_text(dump(result))
        os.replace(self.root/'last-run.json.tmp',self.root/'last-run.json')
        print(dump({k:v for k,v in result.items() if k not in ('proposed',)}),flush=True)
        if apply:self.deliver_notifications()
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',default='/state')
    parser.add_argument('--config',default='/config/config.json');parser.add_argument('--credentials',default='/config/credentials.enc')
    parser.add_argument('--key',default='/config/secret.key');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--loop',action='store_true');parser.add_argument('--settle',type=int,default=30)
    parser.add_argument('--health',action='store_true');args=parser.parse_args()
    os.umask(0o077);root=Path(args.state);root.mkdir(parents=True,exist_ok=True)
    if args.health:
        r=json.loads((root/'last-run.json').read_text());age=time.time()-dt.datetime.fromisoformat(r['at']).timestamp()
        raise SystemExit(0 if age<240 and not r.get('fatal') else 1)
    lock=(root/'sync.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    config=json.loads(Path(args.config).read_text())
    credentials=json.loads(Fernet(Path(args.key).read_bytes()).decrypt(Path(args.credentials).read_bytes()))
    if args.loop and os.environ.get('BOOK_PROGRESS_ADMIN_CONTROL') == '1':
        from admin_runtime import run_admin_loop
        return run_admin_loop(args, root, config, credentials, Bridge)
    bridge=Bridge(root,config,credentials)
    while True:
        try:bridge.cycle(args.apply,args.settle)
        except Exception as exc:
            msg=str(exc) if isinstance(exc,(RuntimeError,ValueError)) or type(exc).__name__=='RequestError' else type(exc).__name__
            print(dump({'at':now(),'fatal':msg}),flush=True)
            if args.apply and bridge.load('_global_error').get('error')!=msg:
                bridge.alert('global','Hoerbuch-Sync gesperrt: '+msg)
                bridge.save('_global_error',{'error':msg})
            if args.apply:bridge.deliver_notifications()
            (root/'last-run.json').write_text(dump({'at':now(),'fatal':msg}))
            if not args.loop:raise SystemExit(1)
        if not args.loop:break
        time.sleep(config.get('interval_seconds',60))


if __name__=='__main__':main()
