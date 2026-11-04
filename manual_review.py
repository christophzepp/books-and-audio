"""Explicit per-title native file review; never writes media or app progress.

The existing sync worker must call these functions serially while holding its
sync lock. Authentication and the user's explicit confirmation belong to the
GUI queue. A preview is valid only in this Bridge instance for ten minutes.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import uuid

import native_discovery as native

SCHEMA = 'book-progress-sync.manual-native-review.v1'
TTL = 600
NATIVE_DIAGNOSTICS = {
    'Selected read-along has no official generation job': 'Für diese Read-Along-Datei ist kein offizieller Erzeugungsauftrag nachweisbar.',
    'Native read-along selection is ambiguous': 'Es gibt keine eindeutig ausgewählte Read-Along-Datei mit Erzeugungsnachweis.',
    'A native generation job is still active': 'Die Read-Along-Erzeugung ist noch nicht abgeschlossen.',
    'Native generation job is not successfully published for this book': 'Der Erzeugungsauftrag ist nicht erfolgreich für diese Datei abgeschlossen.',
    'Native job did not use the complete original audiobook in order': 'Der Auftrag verwendet nicht das vollständig zugeordnete Hörbuch in der richtigen Reihenfolge.',
}


class ReviewRefused(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    def canonical_numbers(item):
        if type(item) is float and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, dict): return {k: canonical_numbers(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)): return [canonical_numbers(v) for v in item]
        return item
    return hashlib.sha256(encode(canonical_numbers(value)).encode()).hexdigest()


def _clock(bridge):
    value = bridge.clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReviewRefused('Invalid review clock')
    return float(value)


def _iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def _connection_binding(bridge):
    """Private memory-only digest; never return secrets or this digest to GUI."""
    ab, bo, jf = bridge.abs, bridge.bo, bridge.jellyfin
    def auth(client, session):
        return getattr(getattr(client, session, None), 'headers', {}).get('Authorization') if client else None
    value = {'config': bridge.config, 'abs_url': getattr(ab, 'base_url', None), 'abs_auth': auth(ab, 'session'),
        'bo_url': getattr(bo, 'base_url', None), 'bo_username': getattr(bo, '_username', None),
        'bo_password': getattr(bo, '_password', None), 'jf_url': getattr(jf, 'url', None), 'jf_auth': auth(jf, 's')}
    return (id(ab), id(bo), id(jf), digest(value))


def _local(bridge, abs_id):
    mappings = bridge.list_mappings()
    selected = [m for m in mappings if m['abs_id'] == abs_id]
    if len(selected) != 1:
        raise ReviewRefused('Only an existing, unique audiobook mapping can be reviewed')
    mapping = selected[0]
    accepted = bridge.db.execute('SELECT bo_id,proof FROM native_readalong_enrollments WHERE id=?', (abs_id,)).fetchone()
    if accepted and accepted[0] != mapping['bo_id']:
        raise ReviewRefused('Accepted proof belongs to a different BookOrbit target')
    target_state = {key: bridge.load(key) for key in (abs_id, '_jellyfin_audio:' + abs_id)}
    bridge._revision_state(target_state[abs_id], mapping, mapping)
    return {'mapping': mapping, 'accepted_row': list(accepted) if accepted else None,
        'state': target_state,
        'observation': bridge.db.execute('SELECT signature,since FROM native_readalong_observations WHERE id=?', (abs_id,)).fetchone(),
        'witness': bridge.db.execute('SELECT bo_id,proof FROM native_readalong_witnesses WHERE id=?', (abs_id,)).fetchone()}


def _blockers(bridge, abs_id):
    result = []
    try:
        bridge.guard()
    except Exception:
        result.append({'code': 'deployment_blocked', 'message': 'Dienst-, Verbindungs- oder Versionsprüfung ist gesperrt.'})
    if not native.settings(bridge.config)[0]:
        result.append({'code': 'native_disabled', 'message': 'Die automatische Read-Along-Zuordnung ist deaktiviert.'})
    if abs_id in bridge.paused_mappings():
        result.append({'code': 'mapping_paused', 'message': 'Dieser Titel ist ausdrücklich pausiert.'})
    pending = bridge.db.execute("SELECT id,book FROM operations WHERE status='pending' ORDER BY id").fetchall()
    if pending:
        result.append({'code': 'pending_operation', 'message': 'Eine Fortschrittsübernahme ist noch nicht abgeschlossen.',
                       'operations': [{'id': row[0], 'abs_id': row[1]} for row in pending]})
    for key, value in bridge.db.execute('SELECT id,value FROM state ORDER BY id'):
        if json.loads(value).get('pending'):
            result.append({'code': 'pending_coordinator', 'message': 'Ein Dienstabgleich ist noch nicht abgeschlossen.', 'state_key': key})
    return result


def _diff(old, new, path=''):
    if isinstance(old, dict) and isinstance(new, dict):
        result = []
        for key in sorted(set(old) | set(new)):
            result.extend(_diff(old.get(key), new.get(key), path + ('.' if path else '') + key))
        return result
    return [] if old == new else [{'field': path, 'before': old, 'after': new}]


def _snapshot(bridge, abs_id):
    local = _local(bridge, abs_id)
    mapping = local['mapping']
    blockers = _blockers(bridge, abs_id)
    ai, bd, bm = bridge.abs.item(abs_id), bridge.bo.detail(mapping['bo_id']), bridge.bo.manifest(mapping['bo_id'])
    proof, current = native.collect_for_review(bridge.bo, mapping, ai, bd, bm)
    timestamp = _clock(bridge)
    if any(native._timestamp(value).timestamp() > timestamp for value in
           (proof['completed_at'], proof['source']['content_version'], proof['overlay']['content_version'])):
        blockers.append({'code': 'future_evidence', 'message': 'Die Dateinachweise liegen zeitlich in der Zukunft.'})
    if _local(bridge, abs_id) != local:
        raise ReviewRefused('Local synchronization state changed during the review')
    accepted = json.loads(local['accepted_row'][1]) if local['accepted_row'] else None
    if accepted is not None:
        if (not isinstance(accepted, dict) or type(accepted.get('schema')) is not int
                or accepted['schema'] not in (1, 2) or accepted.get('bo_id') != mapping['bo_id']
                or accepted['schema'] == 2 and not native._strong(accepted)
                or not isinstance(accepted.get('audio_identity'), str)
                or not re.fullmatch('[0-9a-f]{64}', accepted['audio_identity'])):
            raise ReviewRefused('Accepted native proof is malformed')
        if str(uuid.UUID(accepted['job_id'], version=4)) != accepted['job_id']:
            raise ReviewRefused('Accepted native job identity is malformed')
        native._timestamp(accepted['completed_at'])
        for role in ('source', 'overlay'):
            native.validate_native_readalong(accepted[role]['pin'], mapping['bo_id'], mapping['folder'], folder_config=bridge.config)
            native._timestamp(accepted[role]['content_version'])
            if not isinstance(accepted[role]['file_hash'], str) or not re.fullmatch('[0-9a-f]{32}', accepted[role]['file_hash']):
                raise ReviewRefused('Accepted native file identity is malformed')
    comparison = copy.deepcopy(proof)
    comparison.pop('schema', None)
    old = copy.deepcopy(accepted) if accepted else {}
    old.pop('schema', None); old.pop('sha256_evidence', None)
    files = {role: {'file_id': proof[role]['pin']['overlay_file_id'], 'path': proof[role]['pin']['overlay_path'],
                    'size': proof[role]['pin']['overlay_size'], 'sha256': proof[role]['sha256'],
                    'content_version': proof[role]['content_version']} for role in ('source', 'overlay')}
    record = {'schema': SCHEMA, 'abs_id': abs_id, 'bo_id': mapping['bo_id'], 'title': mapping['title'],
        'scope': 'existing-audiobook-source-and-readalong', 'files': files,
        'legacy_pin': copy.deepcopy(bridge.config.get('native_readalong', {}).get(abs_id)),
        'proof': proof, 'mapping': current, 'expected_mapping': mapping, 'accepted_proof': accepted,
        'differences': _diff(old, comparison),
        'reason': 'Vorhandene Dateifreigabe prüfen' if accepted else 'Für diese Dateien fehlt eine bestätigte Freigabe',
        'blockers': blockers, 'can_approve': not blockers, 'status': 'blocked' if blockers else 'reviewable',
        'local_state_digest': digest(local)}
    return record, local


def preview(bridge, abs_id):
    """Return file evidence or an unapprovable record; issue no API mutations."""
    if not isinstance(abs_id, str) or not abs_id or abs_id.startswith('_') or len(abs_id) > 200:
        raise ReviewRefused('Invalid audiobook target')
    timestamp = _clock(bridge)
    try:
        record, local = _snapshot(bridge, abs_id)
    except Exception as exc:
        record = {'schema': SCHEMA, 'abs_id': abs_id, 'can_approve': False, 'status': 'blocked',
            'blockers': [{'code': 'identity_or_api_failure', 'message': str(exc) if isinstance(exc, ReviewRefused)
                else NATIVE_DIAGNOSTICS.get(str(exc), 'Dateien, Auftrag und Hörbuch konnten nicht vollständig und eindeutig geprüft werden.'),
                'error_type': type(exc).__name__}]}
        local = None
    timestamp = _clock(bridge)
    record.update(captured_at=_iso(timestamp), expires_at=_iso(timestamp + TTL))
    record['review_digest'] = digest(record)
    if record['can_approve']:
        cache = getattr(bridge, '_manual_reviews', {})
        cache[record['review_digest']] = {'record': copy.deepcopy(record), 'local': local,
            'connection': _connection_binding(bridge), 'created': timestamp}
        while len(cache) > 20:
            cache.pop(next(iter(cache)))
        bridge._manual_reviews = cache
    return record


def _backup(bridge, record):
    directory = bridge.root / 'manual-review-backups'
    if directory.is_symlink(): raise ReviewRefused('Unsafe review backup directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077: raise ReviewRefused('Review backup directory is not private')
    stem = record['review_digest'][:16] + '-' + uuid.uuid4().hex
    path = directory / (stem + '.sqlite')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.close(fd)
    with sqlite3.connect(path) as destination:
        bridge.db.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ReviewRefused('Review backup integrity check failed')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    evidence = directory / (stem + '.json')
    fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write((encode(record) + '\n').encode()); handle.flush(); os.fsync(handle.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return str(path)


def approve(bridge, review_record, supplied_digest):
    """Adopt only the exact displayed bytes, retaining every progress baseline."""
    if not isinstance(review_record, dict) or not isinstance(supplied_digest, str):
        raise ReviewRefused('An issued review and its exact digest are required')
    cache = getattr(bridge, '_manual_reviews', {})
    issued = cache.get(supplied_digest)
    if not issued or review_record != issued['record'] or review_record.get('review_digest') != supplied_digest:
        raise ReviewRefused('Review was not issued by this worker or was modified')
    without = {k: v for k, v in review_record.items() if k != 'review_digest'}
    if digest(without) != supplied_digest or not review_record.get('can_approve'):
        raise ReviewRefused('Review digest is invalid or this case cannot be approved')
    elapsed = _clock(bridge) - issued['created']
    if not 0 <= elapsed <= TTL:
        raise ReviewRefused('Review expired; refresh the evidence')
    if _connection_binding(bridge) != issued['connection']:
        raise ReviewRefused('Service connections changed; refresh the evidence')
    if bridge.db.in_transaction:
        raise ReviewRefused('Another local transaction is active')
    fresh, local = _snapshot(bridge, review_record['abs_id'])
    if fresh != {k: v for k, v in review_record.items() if k not in ('review_digest', 'captured_at', 'expires_at')} or local != issued['local']:
        raise ReviewRefused('Files, mapping or pending state changed; refresh the evidence')
    if not fresh['can_approve'] or _connection_binding(bridge) != issued['connection']:
        raise ReviewRefused('The final safety checks do not permit this approval')
    approved_at = _iso(_clock(bridge))
    proof = copy.deepcopy(fresh['proof']); proof['schema'] = 2
    proof['sha256_evidence'] = {'kind': 'user-review', 'review_sha256': supplied_digest, 'reviewed_at': approved_at}
    if fresh['legacy_pin'] is not None:
        proof['sha256_evidence']['superseded_pin_sha256'] = native.signature(fresh['legacy_pin'])
    native._origin(fresh['proof'], proof)  # Validate actual dates; never manufacture an earlier origin.
    bridge.guard()
    backup = _backup(bridge, review_record)
    with bridge.db:
        bridge.db.execute('BEGIN IMMEDIATE')
        if _local(bridge, review_record['abs_id']) != local or _blockers(bridge, review_record['abs_id']):
            raise ReviewRefused('Synchronization state changed before approval')
        if _connection_binding(bridge) != issued['connection']:
            raise ReviewRefused('Service connections changed before approval')
        id, bo_id = fresh['abs_id'], fresh['bo_id']
        if local['accepted_row']:
            cursor = bridge.db.execute('UPDATE native_readalong_enrollments SET proof=? WHERE id=? AND bo_id=? AND proof=?',
                (encode(proof), id, bo_id, local['accepted_row'][1]))
            if cursor.rowcount != 1: raise ReviewRefused('Approved proof changed concurrently')
        else:
            bridge.db.execute('INSERT INTO native_readalong_enrollments VALUES (?,?,?)', (id, bo_id, encode(proof)))
        bridge.db.execute('DELETE FROM native_readalong_observations WHERE id=?', (id,))
        bridge.db.execute('DELETE FROM native_readalong_witnesses WHERE id=?', (id,))
        bridge.db.execute('INSERT INTO events(at,book,kind,detail) VALUES (?,?,?,?)',
            (approved_at, id, 'native_readalong_user_approved', encode({'review_digest': supplied_digest,
             'proof': proof, 'backup': backup, 'progress_unchanged': True})))
    cache.pop(supplied_digest, None)
    return {'status': 'approved', 'abs_id': id, 'bo_id': bo_id, 'review_digest': supplied_digest,
        'approved_at': approved_at, 'backup': backup, 'progress_unchanged': True,
        'next': 'The ordinary synchronization cycle will still check progress conflicts.'}
