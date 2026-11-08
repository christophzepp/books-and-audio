"""Journaled repair of PinePods RSS duration metadata from current audio headers.

No playback/progress, subscription or direct database writes in PinePods.
Bounded external audio probes may use automatically removed private temp files.
The coordinator calls this only after its ordinary duration guard has failed;
all existing progress guards remain authoritative after a successful repair.
"""
from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid


NAMESPACE = '_pine_metadata:'
FULL_PROBE_INTERVAL = 900
SOURCE_MAPPING_KEYS = ('id', 'podcast_id', 'episode_id', 'abs_library', 'path', 'worker_path',
                       'size', 'duration', 'mtime', 'fingerprint', 'feed_url', 'enclosure_url')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def api_source(bridge, mapping, *, require_guid=False):
    """Current API identity only; never evidence that the media file is intact.

    This is sufficient to retain a *negative* retry deferral. Positive proofs,
    due probes and writes still obtain the complete local source below.
    """
    from podcast_sync import build_mapping, settings
    cfg = settings(bridge)
    if mapping.get('_pine_unavailable'):
        raise ValueError('PinePods source identity is unavailable')
    reader = getattr(bridge.abs, 'episode_item', None)
    item = reader(mapping['podcast_id'], mapping['episode_id']) if getattr(bridge.abs, '_observation_only', False) is True and callable(reader) else bridge.abs.item(mapping['podcast_id'])
    episodes = item.get('media', {}).get('episodes')
    if not isinstance(episodes, list):
        raise ValueError('Incomplete Audiobookshelf source inventory')
    matches = [row for row in episodes if isinstance(row, dict) and row.get('id') == mapping['episode_id']]
    if len(matches) != 1:
        raise ValueError('Audiobookshelf source identity changed')
    fresh = build_mapping(item, matches[0], cfg)
    if (fresh is None or any(fresh.get(key) != mapping.get(key) for key in SOURCE_MAPPING_KEYS)
            or require_guid and fresh.get('abs_guid') != mapping.get('abs_guid')):
        raise ValueError('Audiobookshelf source identity changed')
    return dict(mapping={key: mapping[key] for key in SOURCE_MAPPING_KEYS},
                jellyfin=copy.deepcopy(mapping.get('jf')))


def same_api_source(source, current):
    return (isinstance(source, dict) and source.get('mapping') == current['mapping']
            and source.get('jellyfin') == current['jellyfin'])


def result(outcome, reason, **fields):
    return dict(outcome=outcome, reason=reason, **fields)


def same_except_duration(before, after, duration):
    """The repair must preserve the raw completed/position values, including old ends."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if after.get('duration') != duration:
        return False
    left, right = dict(before), dict(after)
    left.pop('duration', None); right.pop('duration', None)
    left_meta, right_meta = dict(left.pop('metadata', {})), dict(right.pop('metadata', {}))
    left_meta.pop('episodeduration', None)
    if right_meta.pop('episodeduration', None) != duration:
        return False
    return left == right and left_meta == right_meta


def metadata_probe_order(bridge, mappings):
    """Fair, read-only ordering; never reserves or expands the attempt budget.

    A fixed feed order plus retry cooldown can repeatedly consume the budget
    on early transport failures before later episodes are ever checked. Give
    never-checked episodes precedence, then retry the oldest completed check.
    Unknown/corrupt journal data is left to the existing per-episode guards.
    Open intents go first for readback-only recovery, never a repeated POST.
    Stable ties preserve the original inventory order and all mapping objects.
    """
    def priority(mapping):
        try:
            record = bridge.load(NAMESPACE + mapping['id'])
        except (TypeError, ValueError):
            record = None
        if not isinstance(record, dict):
            return (1, 0)
        if record.get('stage') == 'intent':
            return (0, 0)
        checked = record.get('updated_at')
        if type(checked) not in (int, float) or not math.isfinite(checked) or checked < 0:
            return (1, 0)
        return (2, checked)
    return sorted(mappings, key=priority)


class PineMetadataRepair:
    """One instance per cycle. At most three candidate repair attempts.

    Preview performs no new API/media requests or local writes. Failed media
    evidence is checked again tomorrow (transport failures after 15 minutes),
    so a few unprobeable episodes cannot consume every future cycle's budget.
    The shared media-probe window is 45 seconds, including at most one full-file
    fallback; all other candidates use at most ten seconds of that window.
    """
    def __init__(self, bridge, max_repairs=3, probe_timeout=10, *, probe=None, allow_full_probe=True):
        if type(max_repairs) is not int or not 1 <= max_repairs <= 3:
            raise ValueError('Invalid PinePods repair budget')
        if type(probe_timeout) not in (int, float) or not math.isfinite(probe_timeout) or not 1 <= probe_timeout <= 10:
            raise ValueError('Invalid PinePods probe timeout')
        self.b = bridge
        self.remaining = max_repairs
        self.probe_timeout = probe_timeout
        self.probe = probe
        if type(allow_full_probe) is not bool:
            raise ValueError('Invalid PinePods full probe switch')
        self.allow_full_probe = allow_full_probe
        self.full_probe_used = False
        self.probe_deadline = None
        self.current_full_probe_at = None

    def _measure(self, url, *, previous_evidence=None, last_full_probe_at=None):
        self.current_full_probe_at = None
        probe = self.probe
        storage_kwargs = {}
        if probe is None:
            from pine_media_probe import measure, private_temp_root
            probe = measure
            try:
                state_root = Path(self.b.root)
                if not state_root.is_absolute() or state_root.resolve(strict=True) != state_root:
                    raise ValueError('Unexpected state directory')
                probe_root = state_root / 'pine-media-probes'
                probe_root.mkdir(mode=0o700, exist_ok=True)
                storage_kwargs['temp_root'] = private_temp_root(probe_root)
            except (OSError, ValueError, TypeError):
                return {'known': False, 'reason': 'probe_storage_unavailable'}
        if self.probe_deadline is None:
            self.probe_deadline = time.monotonic() + 45
        remaining = self.probe_deadline - time.monotonic()
        if remaining < 1:
            return {'known': False, 'reason': 'probe_cycle_budget'}
        kwargs = dict(timeout_seconds=min(self.probe_timeout, remaining), **storage_kwargs)
        if previous_evidence is not None:
            kwargs['previous_evidence'] = previous_evidence
        evidence = probe(url, **kwargs)
        if (self.allow_full_probe and isinstance(evidence, dict)
                and evidence.get('known') is False and evidence.get('reason') in
                {'mpeg_audio_without_frame_count_tag', 'id3_exceeds_prefix', 'no_valid_frame_count_header'}):
            remaining = self.probe_deadline - time.monotonic()
            if (type(last_full_probe_at) in (int, float) and math.isfinite(last_full_probe_at)
                    and self.b.clock() - last_full_probe_at < FULL_PROBE_INTERVAL):
                return {'known': False, 'reason': 'full_probe_cooldown',
                        'next_full_probe_at': last_full_probe_at + FULL_PROBE_INTERVAL}
            if not self.full_probe_used and remaining >= 1:
                self.full_probe_used = True
                self.current_full_probe_at = self.b.clock()
                evidence = probe(url, timeout_seconds=min(45, remaining), full_probe=True, **storage_kwargs)
            else:
                # No evidence failure occurred: this episode simply did not
                # receive this cycle's one expensive slot. Do not defer it for
                # a day and thereby starve the remaining initial inventory.
                return {'known': False, 'reason': 'full_probe_cycle_budget'}
        return evidence

    def _cache_fields(self, record, source):
        """Keep the original full-file proof even when a later read is deferred.

        This is not permission to reuse a duration: the media probe must still
        fetch and compare a strong validator, exact length, URL and prefix.
        """
        if not isinstance(record, dict) or record.get('source') != source:
            return {}
        fields = {}
        from pine_media_probe import valid_result
        # Deferral is not evidence of a different file. Retain the last valid
        # measurement separately from the latest failed/budgeted attempt.
        for evidence in (record.get('candidate_evidence'), record.get('evidence'), record.get('full_proof')):
            if (isinstance(evidence, dict) and evidence.get('known') is True
                    and valid_result(evidence, source['mapping']['enclosure_url'])):
                fields.update(candidate_evidence=copy.deepcopy(evidence), candidate_evidence_sha256=digest(evidence))
                break
        full_at = record.get('last_full_probe_at')
        if type(full_at) in (int, float) and math.isfinite(full_at) and 0 <= full_at <= self.b.clock():
            fields['last_full_probe_at'] = full_at
        proof, proof_at = record.get('full_proof'), record.get('full_proof_at')
        if (type(proof_at) in (int, float) and math.isfinite(proof_at)
                and 0 <= self.b.clock() - proof_at <= 30 * 86400
                and isinstance(proof, dict) and proof.get('known') is True
                and proof.get('header', {}).get('source') == 'full_mp3_packets'):
            if valid_result(proof, source['mapping']['enclosure_url']):
                fields.update(full_proof=copy.deepcopy(proof), full_proof_at=proof_at)
        return fields

    def _pending_progress(self, mapping):
        from podcast_status_sync import NAMESPACE as STATUS_NAMESPACE
        keys=(mapping['id'],STATUS_NAMESPACE+mapping['id'])
        return any(bool(self.b.load(key).get('pending'))
            or self.b.db.execute("SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1",
                                 (key,)).fetchone() is not None for key in keys)

    def _source(self, mapping):
        from podcast_sync import settings
        from io_metrics import increment
        increment(self.b, 'podcast_metadata_source_checks')
        current = api_source(self.b, mapping)
        cfg = settings(self.b)
        worker = Path(mapping['worker_path'])
        root = Path(cfg['worker_root'])
        if (not worker.is_absolute() or not root.is_absolute() or worker.resolve() != worker
                or root.resolve() != root or not worker.is_relative_to(root)):
            raise ValueError('Unexpected podcast media path')
        info = worker.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_size != mapping['size']
                or abs(info.st_mtime_ns / 1_000_000 - mapping['mtime']) > 2):
            raise ValueError('Podcast media file changed')
        return dict(mapping=current['mapping'],
                    file=dict(device=info.st_dev, inode=info.st_ino, size=info.st_size,
                              mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns),
                    jellyfin=current['jellyfin'])

    def _persist(self, key, record, stage, reason):
        """State, immutable operation ID and sanitized event commit together.

        Production Bridge already uses FULL SQLite durability. Check it before
        the first intent and additionally fsync the DB and containing directory.
        """
        if self.b.db.execute('PRAGMA synchronous').fetchone()[0] not in (2, 3):
            raise RuntimeError('PinePods repair requires durable SQLite settings')
        record = dict(record, stage=stage, updated_at=self.b.clock())
        status = 'pending' if stage == 'intent' else 'complete'
        detail = dict(reason=reason, operation_id=record['operation_id'], stage=stage,
                      evidence_sha256=record.get('evidence_sha256'),
                      target_duration=record.get('target_duration'))
        at = dt.datetime.fromtimestamp(self.b.clock(), dt.timezone.utc).isoformat()
        with self.b.db:
            self.b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)', (key, encoded(record)))
            self.b.db.execute('INSERT OR REPLACE INTO operations(id,book,value,status) VALUES(?,?,?,?)',
                              (record['operation_id'], key, encoded(record), status))
            self.b.db.execute('INSERT INTO events(at,book,kind,detail) VALUES(?,?,?,?)',
                              (at, key, 'pine_duration_' + stage, encoded(detail)))
        dbpath = self.b.root / 'state.sqlite'
        for path in (dbpath, self.b.root):
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def _defer(self, key, source, reason, *, delay=86400, evidence=None):
        previous = self._cache_fields(self.b.load(key), source)
        record = dict(schema=1, operation_id='pine-metadata-' + uuid.uuid4().hex,
                      source=source, next_probe_at=self.b.clock() + delay, **previous)
        if self.current_full_probe_at is not None:
            record['last_full_probe_at'] = self.current_full_probe_at
        if evidence:
            record['evidence_sha256'] = digest(evidence)
            record['evidence'] = evidence
            from pine_media_probe import valid_result
            if evidence.get('known') is True and valid_result(evidence, source['mapping']['enclosure_url']):
                record.update(candidate_evidence=copy.deepcopy(evidence), candidate_evidence_sha256=digest(evidence))
                if evidence.get('header', {}).get('source') == 'full_mp3_packets':
                    record.update(full_proof=copy.deepcopy(evidence), full_proof_at=self.current_full_probe_at or self.b.clock())
        self._persist(key, record, 'deferred', reason)
        return result('waiting', reason)

    def _recover(self, key, record, mapping, source):
        # An interrupted intent is never sent again automatically. The sole
        # automatic recovery is proof that the first POST already took effect.
        if record.get('source') != source:
            return result('waiting', 'interrupted_repair_source_changed')
        current = self.b.pinepods.inspect_episode_metadata(mapping['feed_url'], mapping['enclosure_url'], mapping['duration'])
        if same_except_duration(record.get('before'), current, record.get('target_duration')):
            self._persist(key, record, 'verified', 'interrupted_repair_verified')
            return result('corrected', 'interrupted_repair_verified', duration=current['duration'])
        return result('waiting', 'interrupted_repair_requires_review')

    def repair_if_needed(self, mapping, apply):
        if not apply:
            return result('waiting', 'preview_no_metadata_repair')
        if not getattr(self.b, 'pinepods', None):
            return result('nochange', 'pinepods_disabled')
        if self._pending_progress(mapping):
            return result('waiting', 'progress_transfer_pending')
        key = NAMESPACE + mapping['id']
        record = self.b.load(key)
        # Atomically written intents have a matching operation row. Corrupt or
        # unexpected journal state is a review condition, never a fresh attempt.
        if record and (record.get('schema') != 1 or record.get('stage') not in ('intent', 'verified', 'deferred')):
            return result('waiting', 'metadata_journal_requires_review')
        pending = self.b.db.execute("SELECT id,value FROM operations WHERE book=? AND status='pending'", (key,)).fetchall()
        try:
            if pending and (len(pending) != 1 or record.get('stage') != 'intent'
                            or pending[0][0] != record.get('operation_id') or json.loads(pending[0][1]) != record):
                return result('waiting', 'metadata_journal_requires_review')
        except (TypeError, ValueError):
            return result('waiting', 'metadata_journal_requires_review')
        if record.get('stage') == 'intent' and not pending:
            return result('waiting', 'metadata_journal_requires_review')
        if self.remaining <= 0 and record.get('stage') != 'intent':
            return result('waiting', 'metadata_repair_cycle_budget')
        source = None
        self.current_full_probe_at = None
        try:
            retry_at = record.get('next_probe_at', 0)
            full_at = record.get('last_full_probe_at')
            # Upgrade the old per-URL daily-budget deferral. A publisher can
            # rotate its ad cut within a day; one attempt every 15 minutes is
            # still bounded by the cycle's single full download and deadline.
            if (type(full_at) in (int, float) and math.isfinite(full_at)
                    and type(retry_at) in (int, float) and math.isfinite(retry_at)
                    and abs(retry_at - full_at - 86400) < .01):
                retry_at = min(retry_at, full_at + FULL_PROBE_INTERVAL)
            # A cooldown cannot authorize a repair or renew any evidence.
            # Preserve that negative decision without waking unchanged media.
            # An intent must always use the full local source for readback.
            if (record.get('stage') != 'intent'
                    and type(retry_at) in (int, float) and math.isfinite(retry_at)
                    and retry_at > self.b.clock()
                    and same_api_source(record.get('source'), api_source(self.b, mapping))):
                from io_metrics import increment
                increment(self.b, 'podcast_metadata_backoff_skips')
                return result('waiting', 'metadata_probe_backoff')
            source = self._source(mapping)
            if record.get('stage') == 'intent':
                return self._recover(key, record, mapping, source)
            self.remaining -= 1
            before = self.b.pinepods.inspect_episode_metadata(mapping['feed_url'], mapping['enclosure_url'], mapping['duration'])
            if before['duration'] > 0 and abs(before['duration'] - mapping['duration']) <= 2:
                return result('nochange', 'duration_already_matches')
            if before['downloaded']:
                return self._defer(key, source, 'pinepods_downloaded_copy')
            if not before['completed'] and (before['position'] or 0) > 0:
                return self._defer(key, source, 'active_pinepods_episode', delay=900)
            cache = self._cache_fields(record, source)
            evidence = self._measure(mapping['enclosure_url'], previous_evidence=cache.get('full_proof'),
                                     last_full_probe_at=cache.get('last_full_probe_at'))
            if not isinstance(evidence, dict) or evidence.get('known') is not True:
                reason = evidence.get('reason', '') if isinstance(evidence, dict) else ''
                if reason in ('probe_cycle_budget', 'full_probe_cycle_budget'):
                    return result('waiting', 'metadata_repair_cycle_budget')
                if reason == 'full_probe_cooldown':
                    return self._defer(key, source, 'metadata_full_probe_cooldown',
                                       delay=max(1, evidence['next_full_probe_at'] - self.b.clock()))
                delay = 900 if reason in ('probe_timeout', 'probe_failed', 'probe_process_failed') else 86400
                return self._defer(key, source, 'audio_header_unverified', delay=delay, evidence=evidence)
            from pine_media_probe import valid_result
            if not valid_result(evidence, mapping['enclosure_url']):
                return self._defer(key, source, 'audio_header_evidence_invalid', evidence=evidence)
            measured = evidence.get('duration_seconds')
            if (type(measured) not in (int, float) or not math.isfinite(measured) or not 1 <= measured <= 172800
                    or abs(measured - mapping['duration']) > 2):
                return self._defer(key, source, 'measured_audio_duration_differs', evidence=evidence)
            target = int(round(measured))
            if abs(target - mapping['duration']) > 2:
                return self._defer(key, source, 'rounded_audio_duration_differs', evidence=evidence)
            # A changed file, metadata row, active player or pending transfer
            # invalidates the probe before an intent or mutation can be issued.
            if self._source(mapping) != source or self._pending_progress(mapping):
                return result('waiting', 'source_or_progress_changed_during_probe')
            if getattr(self.b, 'external_write_guard', None):
                self.b.external_write_guard()
            fresh = self.b.pinepods.inspect_episode_metadata(mapping['feed_url'], mapping['enclosure_url'], mapping['duration'])
            if fresh != before:
                return result('waiting', 'pinepods_changed_during_probe')
            intent = dict(schema=1, operation_id='pine-metadata-' + uuid.uuid4().hex,
                          source=source, before=before, target_duration=target,
                          evidence=evidence, evidence_sha256=digest(evidence), **cache)
            if self.current_full_probe_at is not None:
                intent['last_full_probe_at'] = self.current_full_probe_at
            if evidence.get('header', {}).get('source') == 'full_mp3_packets':
                # Only an actual complete probe creates/renews this proof.
                # A validated cache hit must retain its original age.
                intent['full_proof'] = copy.deepcopy(evidence)
                intent['full_proof_at'] = self.current_full_probe_at or self.b.clock()
            intent = self._persist(key, intent, 'intent', 'measured_duration_repair')
            # From here, any exception leaves a durable intent. A later cycle
            # only reads it back; it never blindly retries the metadata POST.
            self.b.pinepods.write_episode_duration(before, target)
            after = self.b.pinepods.inspect_episode_metadata(mapping['feed_url'], mapping['enclosure_url'], mapping['duration'])
            if not same_except_duration(before, after, target) or self._source(mapping) != source:
                return result('waiting', 'metadata_repair_readback_changed')
            self._persist(key, intent, 'verified', 'measured_duration_repaired')
            return result('corrected', 'measured_duration_repaired', duration=target)
        except Exception:
            # Exceptions can contain signed feed URLs from another API. Never
            # echo them to public status/event logs; the intent stays private.
            if source is not None and self.b.load(key).get('stage') != 'intent':
                try:
                    return self._defer(key, source, 'metadata_repair_inspection_or_transport_failed', delay=900)
                except Exception:
                    pass
            return result('waiting', 'metadata_repair_inspection_or_transport_failed')


def repair_if_needed(bridge, mapping, apply):
    """Convenience API; caller should retain its per-cycle manager explicitly."""
    manager = getattr(bridge, '_pine_metadata_repair', None)
    if manager is None:
        manager = PineMetadataRepair(bridge)
        bridge._pine_metadata_repair = manager
    return manager.repair_if_needed(mapping, apply)
