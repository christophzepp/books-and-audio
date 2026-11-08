"""Attest completed official read-alongs against a verified same-file audio pair.

This never registers an audio pair or writes application progress. BookOrbit's
file hash is a partial MD5, not a full content checksum; audio continuity uses
the existing independent file, mtime, timeline and chapter evidence unchanged.
"""
from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import PurePosixPath
from uuid import UUID

from mapping import build_mapping, validate_native_readalong, audio_path
from revision import mapping_identity_fingerprint, reconcile_manifest_revision


class NativeEnrollmentPending(RuntimeError):
    """A verified candidate is still settling, without changing its baseline."""


class NativeMediaVerificationRequired(ValueError):
    """Current API identity lacks its historical byte witness; never write."""


def _definitive_original_failure(error):
    """Only an unusable original response revokes its historical byte proof."""
    from bo_client import RequestError
    prefix = 'GET /api/v1/books/files/{file_id}/download: '
    if not isinstance(error, RequestError) or not str(error).startswith(prefix):
        return False
    status = getattr(error, 'status', None)
    return (status in (404, 410) or status == 200 and str(error)[len(prefix):] in {
        'invalid original-file response', 'invalid original-file body',
        'original-file size changed', 'original-file body incomplete'})


def _observation_clock(bo):
    clock = getattr(getattr(bo, 'observation_metrics', None), 'clock', time.time)
    when = clock() if callable(clock) else time.time()
    return when if type(when) in (int, float) and math.isfinite(when) else time.time()


def _revoke_original(bo, expected, proof, service, origin=None):
    """Retain approvals but prevent known bad originals reusing old witnesses."""
    from media_observation import ObservationStore
    cache = getattr(bo, '_native_full_hash_cache', None)
    if isinstance(cache, dict):cache.clear()
    bo._native_full_hash_cache = {}
    observations = ObservationStore(getattr(bo, 'observation_root', None))
    observations.forget('native-readalong', expected['abs_id'])
    observations.put('native-revocation', expected['abs_id'],
                     {'current': proof, 'service': service},
                     {'revoked_at': _observation_clock(bo), 'origin': signature(origin) if origin is not None else None})


def _original_revoked(bo, expected, proof, service, origin):
    from media_observation import ObservationStore
    observations = ObservationStore(getattr(bo, 'observation_root', None))
    saved = observations.get('native-revocation', expected['abs_id'],
                             {'current': proof, 'service': service})
    if saved is None:return False
    # A deliberate, newer full-file user review can supersede the known
    # failure. Its normal origin checks still run before reaching this point.
    provenance = origin.get('sha256_evidence') if isinstance(origin, dict) else None
    if (isinstance(saved, dict) and set(saved) == {'revoked_at', 'origin'}
            and type(saved['revoked_at']) in (int, float) and math.isfinite(saved['revoked_at'])
            and signature(origin) != saved['origin']
            and _user_review_provenance(provenance)):
        reviewed = _timestamp(provenance['reviewed_at']).timestamp()
        if saved['revoked_at'] < reviewed <= _observation_clock(bo):return False
    return True


def settings(config):
    value = config.get('auto_native_readalong', {})
    if (not isinstance(value, dict) or set(value) - {'enabled', 'stable_seconds'}
            or not isinstance(value.get('enabled', False), bool)):
        raise ValueError('Invalid automatic native read-along settings')
    seconds = value.get('stable_seconds', 300)
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or not 30 <= seconds <= 86400):
        raise ValueError('Native read-along stability must be between 30 and 86400 seconds')
    return value.get('enabled', False), float(seconds)


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Invalid native file identity')
    return value


def _timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (TypeError, AttributeError, ValueError):
        raise ValueError('Native content timestamp is missing') from None


def _file_evidence(file, book_id, folder, folder_config=None):
    pin = validate_native_readalong({'bo_id': book_id, 'overlay_file_id': file['id'],
                                    'overlay_path': file['absolutePath'],
                                    'overlay_size': file['sizeBytes']}, book_id, folder, folder_config=folder_config)
    if file.get('format') != 'epub' or file.get('role') not in ('primary', 'content'):
        raise ValueError('Native EPUB is not a content file')
    version, file_hash = file.get('contentVersion'), file.get('fileHash')
    if not isinstance(file_hash, str) or re.fullmatch('[0-9a-f]{32}', file_hash) is None:
        raise ValueError('Native EPUB content fingerprint is missing')
    _timestamp(version)
    return {'pin': pin, 'file_hash': file_hash, 'content_version': version}


def inspect(expected, abs_item, detail, manifest, capability, job, *, folder_config=None):
    """Return fresh proof and a mapping; reject any unaccounted ebook or audio."""
    try:
        proof, current = _inspect(expected, abs_item, detail, manifest, capability, job, folder_config)
        _origin(proof, None)
        return proof, current
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError('Incomplete native read-along evidence') from None


def _inspect(expected, abs_item, detail, manifest, capability, job, folder_config=None):
    book_id = _positive_id(expected['bo_id'])
    selected = _positive_id(detail['readAloudSync']['overlayFileId'])
    rows = capability['existingReadAlongs']
    if not isinstance(rows, list) or len(rows) != 1 or _positive_id(rows[0]['fileId']) != selected:
        raise ValueError('Native read-along selection is ambiguous')
    job_id = rows[0]['generatedByJobId']
    try:
        if not isinstance(job_id, str) or str(UUID(job_id, version=4)) != job_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('Selected read-along has no official generation job') from None
    latest = capability.get('latestJob')
    if latest is not None and latest.get('status') in ('queued', 'processing', 'awaiting_review'):
        raise ValueError('A native generation job is still active')
    if (job['id'] != job_id or _positive_id(job['bookId']) != book_id or _positive_id(job['outputFileId']) != selected
            or job['status'] not in ('completed', 'completed_with_warnings')
            or job['stage'] != 'completed' or job.get('errorCode') is not None
            or job.get('cancelRequested') is not False):
        raise ValueError('Native generation job is not successfully published for this book')
    audio_ids = job['input']['audioFileIds']
    source_id = _positive_id(job['input']['epubFileId'])
    if (not isinstance(audio_ids, list) or not audio_ids
            or [_positive_id(x) for x in audio_ids] != [x['fileId'] for x in manifest['assets']]
            or source_id == selected):
        raise ValueError('Native job did not use the complete original audiobook in order')
    files = detail['files']
    if (not isinstance(files, list) or len({f['id'] for f in files}) != len(files)
            or len({f['absolutePath'] for f in files}) != len(files)):
        raise ValueError('Native file inventory is ambiguous')
    by_id = {f['id']: f for f in files}
    overlay, source = by_id[selected], by_id[source_id]
    if ((overlay.get('mediaOverlay') or {}).get('available') is not True
            or (source.get('mediaOverlay') or {}).get('available') is True):
        raise ValueError('Native source and overlay roles are inconsistent')
    overlay_evidence = _file_evidence(overlay, book_id, expected['folder'], folder_config)
    source_evidence = _file_evidence(source, book_id, expected['folder'], folder_config)
    if (rows[0]['sizeBytes'] != overlay['sizeBytes']
            or job['input']['sourceBytes'] != source['sizeBytes'] + sum(by_id[x]['sizeBytes'] for x in audio_ids)):
        raise ValueError('Native job source inventory changed')
    # Only the exact plain EPUB named by the completed job is exempted. The
    # ordinary mapper still rejects every other ebook and checks every audio.
    audio_detail = dict(detail, files=[f for f in files if f['id'] != source_id])
    current = build_mapping(abs_item, audio_detail, manifest,
                            native_readalong=overlay_evidence['pin'], folder_config=folder_config)
    reconcile_manifest_revision(expected, current)
    proof = {'schema': 1, 'bo_id': book_id, 'job_id': job_id, 'completed_at': job['completedAt'],
             'audio_identity': mapping_identity_fingerprint(current),
             'overlay': overlay_evidence, 'source': source_evidence}
    return proof, current


def _identity(proof):
    """Exclude only API row-update times and the separately checked full hashes."""
    value = copy.deepcopy(proof)
    value.pop('schema', None)
    value.pop('sha256_evidence', None)
    for role in ('source', 'overlay'):
        value[role].pop('content_version', None)
        value[role].pop('sha256', None)
    return value


def _strong(proof):
    provenance = proof.get('sha256_evidence') if isinstance(proof, dict) else None
    valid_provenance = (provenance == {'kind': 'publication'} or
                        (isinstance(provenance, dict) and set(provenance) == {'kind', 'review_sha256'}
                         and provenance['kind'] == 'reviewed-existing'
                         and isinstance(provenance['review_sha256'], str)
                         and re.fullmatch('[0-9a-f]{64}', provenance['review_sha256']))
                        or _worker_provenance(provenance) or _user_review_provenance(provenance))
    return (isinstance(proof, dict) and proof.get('schema') == 2 and valid_provenance
            and all(isinstance(proof.get(role), dict)
                    and isinstance(proof[role].get('sha256'), str)
                    and re.fullmatch('[0-9a-f]{64}', proof[role]['sha256'])
                    for role in ('source', 'overlay')))


def _user_review_provenance(value):
    keys = {'kind', 'review_sha256', 'reviewed_at'}
    if not (isinstance(value, dict) and set(value) in (keys, keys | {'superseded_pin_sha256'})
            and value.get('kind') == 'user-review' and isinstance(value.get('review_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', value['review_sha256'])):
        return False
    if 'superseded_pin_sha256' in value and (not isinstance(value['superseded_pin_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['superseded_pin_sha256'])):
        return False
    try:
        _timestamp(value['reviewed_at'])
        return True
    except ValueError:
        return False


def _worker_provenance(value):
    if not (isinstance(value, dict)
            and set(value) == {'kind', 'receipt_sha256', 'input_captured_at', 'completed_witness_at'}
            and value.get('kind') == 'worker-artifact'
            and isinstance(value.get('receipt_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', value['receipt_sha256'])):
        return False
    try:
        return _timestamp(value['input_captured_at']) <= _timestamp(value['completed_witness_at'])
    except ValueError:
        return False


def _receipt_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate worker witness field')
        value[key] = item
    return value


def _read_worker_receipt(job_id):
    """Only the configured, read-only collector directory can supply witnesses.

    Never search the media pool or fetch a receipt from an application URL.
    Absence preserves the ordinary strict publication-time rule.
    """
    root = os.environ.get('BOOK_PROGRESS_WORKER_WITNESS_DIR')
    if not root:
        return None
    path = PurePosixPath(root)
    if not path.is_absolute() or '..' in path.parts or str(path) != root:
        raise ValueError('Invalid worker witness directory')
    limit = 32 * 1024 * 1024  # Matches the collector's bound for long, multi-track books.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_mode & 0o022:
            raise ValueError('Worker witness directory is not private')
        try:
            fd = os.open(job_id + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022 or not 0 < before.st_size <= limit:
                raise ValueError('Unsafe worker witness file')
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                raw = handle.read(limit + 1)
            after = os.fstat(fd)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if len(raw) != before.st_size or any(getattr(before, x) != getattr(after, x) for x in fields):
                raise ValueError('Worker witness changed during read')
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    try:
        value = json.loads(raw, object_pairs_hook=_receipt_json,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid witness number')))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('Invalid worker witness JSON') from None
    return value, hashlib.sha256(raw).hexdigest()


def _receipt_file(value, *, hashed=False):
    keys = {'path', 'size', 'mtime_ns', 'ctime_ns', 'dev', 'ino'} | ({'sha256'} if hashed else set())
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('Incomplete worker file evidence')
    path = PurePosixPath(value['path']) if isinstance(value['path'], str) else None
    if path is None or not path.is_absolute() or '..' in path.parts or str(path) != value['path']:
        raise ValueError('Invalid worker file path')
    if any(isinstance(value[x], bool) or not isinstance(value[x], int) or value[x] < 0
           for x in ('size', 'mtime_ns', 'ctime_ns', 'dev', 'ino')) or value['size'] <= 0 or value['ino'] <= 0:
        raise ValueError('Invalid worker file stat evidence')
    if hashed and (not isinstance(value['sha256'], str) or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None):
        raise ValueError('Missing worker full-file SHA256')
    return value


def _worker_origin(proof, current, detail, manifest, job, folder_config=None):
    loaded = _read_worker_receipt(proof['job_id'])
    if loaded is None:
        return None
    receipt, digest = loaded
    return worker_receipt_origin(receipt, digest, proof, current, detail, manifest, job, folder_config=folder_config)


def worker_receipt_origin(receipt, digest, proof, current, detail, manifest, job, *, folder_config=None):
    """Validate collector evidence without API calls or progress/state writes.

    The caller supplies the SHA256 of the exact private receipt file. Production
    calls this only through the protected directory loader above; the pure
    function also permits isolated worker-contract tests with explicit fixtures.
    Returned origin is not enrollment: full current downloads and stability
    checks in collect remain mandatory before ordinary synchronization.
    """
    if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
        raise ValueError('Missing worker receipt digest')
    expected_keys = {'schema', 'kind', 'job_id', 'generation_id', 'request_sha256', 'request', 'input_captured_at',
                     'completed_witness_at', 'source', 'audio', 'output', 'report', 'markers', 'worker'}
    if (not isinstance(receipt, dict) or set(receipt) != expected_keys
            or receipt['schema'] != 1 or isinstance(receipt['schema'], bool)
            or receipt['kind'] != 'official-worker-completion' or receipt['job_id'] != proof['job_id']):
        raise ValueError('Invalid official worker receipt')
    try:
        if str(UUID(receipt['generation_id'], version=4)) != receipt['generation_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid worker generation identity') from None
    request = receipt['request']
    request_keys = {'jobId', 'epubPath', 'audioPaths', 'workDir', 'language', 'granularity', 'model',
                    'sourceBytes', 'audioDurationSeconds', 'decisions', 'resumeFrom', 'maxOutputBytes'}
    if (not isinstance(request, dict) or set(request) != request_keys
            or hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest() != receipt['request_sha256']
            or request['jobId'] != proof['job_id']
            or request['model'] != 'quartznet15x5'):
        raise ValueError('Worker request identity changed')
    if not (_timestamp(job['createdAt']) <= _timestamp(receipt['input_captured_at'])
            <= _timestamp(receipt['completed_witness_at']) <= _timestamp(proof['completed_at'])):
        raise ValueError('Worker receipt was not captured before publication')
    for key in ('language', 'granularity', 'sourceBytes'):
        if request[key] != job['input'][key]:
            raise ValueError('Worker request does not match completed job')
    if request['decisions'] != job['decisions']:
        raise ValueError('Worker decisions changed after capture')
    duration = request['audioDurationSeconds']
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                 or not math.isfinite(duration) or duration <= 0
                                 or abs(duration - sum(f['durationSeconds'] for f in detail['files']
                                                      if f['id'] in job['input']['audioFileIds'])) > 0.001):
        raise ValueError('Worker audio duration no longer matches its sources')
    source, output = (_receipt_file(receipt[x], hashed=True) for x in ('source', 'output'))
    pin = proof['source']['pin']
    if (source['path'] != pin['overlay_path'] or source['size'] != pin['overlay_size']
            or request['epubPath'] != source['path'] or output['size'] != proof['overlay']['pin']['overlay_size']):
        raise ValueError('Worker source or output does not match selected files')
    work_dir = PurePosixPath(request['workDir'])
    if (not work_dir.is_absolute() or '..' in work_dir.parts or str(work_dir) != request['workDir']
            or work_dir.name != proof['job_id'] or output['path'] != str(work_dir / 'output.epub')):
        raise ValueError('Worker output does not belong to its job workspace')
    if (isinstance(request['maxOutputBytes'], bool) or not isinstance(request['maxOutputBytes'], int)
            or not output['size'] <= request['maxOutputBytes'] <= 100 * 1024 ** 3):
        raise ValueError('Invalid worker output size limit')
    worker = receipt['worker']
    if (not isinstance(worker, dict) or set(worker) != {'job_id', 'status', 'output_bytes', 'accepted'}
            or worker['job_id'] != proof['job_id'] or worker['status'] != 'completed'
            or worker['output_bytes'] != output['size']):
        raise ValueError('Worker did not finish the witnessed output')
    accepted = worker['accepted']
    if (not isinstance(accepted, dict) or set(accepted) != {'seq', 'at', 'attempt'}
            or any(isinstance(accepted[x], bool) or not isinstance(accepted[x], int) or accepted[x] <= 0
                   for x in ('seq', 'attempt'))
            or not _timestamp(receipt['input_captured_at']) <= _timestamp(accepted['at'])
                <= _timestamp(receipt['completed_witness_at'])):
        raise ValueError('Worker acceptance does not belong to the witnessed attempt')
    audio = receipt['audio']
    if not isinstance(audio, list) or len(audio) != len(manifest['assets']):
        raise ValueError('Worker audio inventory is incomplete')
    by_id = {f['id']: f for f in detail['files']}
    by_path = {f['path']: f for f in current['files']}
    for row, asset in zip(audio, manifest['assets']):
        row = _receipt_file(row)
        file = by_id[asset['fileId']]
        if row['path'] != file['absolutePath'] or row['size'] != file['sizeBytes']:
            raise ValueError('Worker audio identity or order changed')
        absolute = by_path[audio_path(row['path'], 'bo', folder_config)]
        if abs(row['mtime_ns'] / 1_000_000 - absolute['mtimeMs']) >= 1:
            raise ValueError('Worker audio timestamp changed')
        public_id = asset['assetId'][4:] if asset['assetId'].startswith('aud_') else ''
        etag = hashlib.sha256(f"{public_id}:{row['size']}:{row['mtime_ns'] // 1_000_000}".encode()).hexdigest()
        if not public_id or etag != asset['etag']:
            raise ValueError('Worker audio no longer matches BookOrbit file evidence')
    if request['audioPaths'] != [row['path'] for row in audio]:
        raise ValueError('Worker request reordered its audio inputs')
    if source['size'] + sum(row['size'] for row in audio) != request['sourceBytes']:
        raise ValueError('Worker source byte count changed')
    report, markers = receipt['report'], receipt['markers']
    if (not isinstance(report, dict) or set(report) != {'sha256', 'size'}
            or not isinstance(report['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', report['sha256'])
            or isinstance(report['size'], bool) or not isinstance(report['size'], int) or report['size'] <= 0
            or not isinstance(markers, dict) or set(markers) != {'aligning', 'preparing_audio', 'marking_up_ebook'}):
        raise ValueError('Worker stage or report evidence is incomplete')
    for stage, marker in markers.items():
        if (not isinstance(marker, dict) or marker.get('stage') != stage
                or not isinstance(marker.get('inputs'), dict)
                or not isinstance(marker.get('fingerprint'), str)
                or not re.fullmatch('[0-9a-f]{64}', marker['fingerprint'])
                or _timestamp(marker.get('finishedAt')) > _timestamp(receipt['completed_witness_at'])):
            raise ValueError('Worker stage evidence changed')
    result = copy.deepcopy(proof)
    result['schema'] = 2
    result['sha256_evidence'] = {'kind': 'worker-artifact', 'receipt_sha256': digest,
        'input_captured_at': receipt['input_captured_at'], 'completed_witness_at': receipt['completed_witness_at']}
    result['source']['sha256'], result['overlay']['sha256'] = source['sha256'], output['sha256']
    return result


def _origin(current, trusted):
    """A late API timestamp needs a previously witnessed, identical publication.

    A trusted witness may be accepted or pending. Its origin is either the
    strict API publication-time check or a durable worker completion-barrier
    receipt; both include full SHA256 of source EPUB and generated output.
    It is evidence only, never progress-write authority.
    """
    if isinstance(trusted, dict) and trusted.get('schema') == 2 and not _strong(trusted):
        raise ValueError('Native full-file witness is incomplete')
    if _strong(trusted) and trusted.get('job_id') == current['job_id']:
        if _identity(trusted) != _identity(current):
            raise ValueError('Changed native content identity requires a new completed generation job')
        worker_origin = _worker_provenance(trusted.get('sha256_evidence'))
        user_origin = _user_review_provenance(trusted.get('sha256_evidence'))
        if worker_origin and _timestamp(trusted['sha256_evidence']['completed_witness_at']) > _timestamp(trusted['completed_at']):
            raise ValueError('Worker witness was captured after publication')
        if user_origin and _timestamp(trusted['sha256_evidence']['reviewed_at']) < max(
                _timestamp(trusted['completed_at']), *(_timestamp(trusted[role]['content_version']) for role in ('source', 'overlay'))):
            raise ValueError('User review predates its observed evidence')
        for role in ('source', 'overlay'):
            origin = _timestamp(trusted[role]['content_version'])
            if not worker_origin and not user_origin and origin > _timestamp(trusted['completed_at']):
                raise ValueError('Native witness has no strict publication origin')
            if _timestamp(current[role]['content_version']) < origin:
                raise ValueError('Native content timestamp moved backwards')
        return trusted
    for role in ('overlay', 'source'):
        if _timestamp(current[role]['content_version']) > _timestamp(current['completed_at']):
            raise ValueError('Native ' + role + ' changed after its completed generation job')
    return None


def _collect_hashed(bo, expected, abs_item, detail, manifest, capability, job, trusted, cache_ok, folder_config=None, *, logical_only=False):
    if type(logical_only) is not bool or logical_only and not cache_ok:
        raise ValueError('Invalid native verification mode')
    folder_config = folder_config if folder_config is not None else getattr(bo, "folder_config", None)
    proof, current = _inspect(expected, abs_item, detail, manifest, capability, job, folder_config)
    try:
        origin = _origin(proof, trusted)
    except ValueError:
        # A receipt can bootstrap a previously unwitnessed publication. It
        # must never replace a strong witness that now disagrees with reality.
        if (isinstance(trusted, dict) and trusted.get('schema') == 2
                and (not _strong(trusted) or trusted.get('job_id') == proof['job_id'])):
            raise
        if not any(_timestamp(proof[role]['content_version']) > _timestamp(proof['completed_at'])
                   for role in ('source', 'overlay')):
            raise
        seed = _worker_origin(proof, current, detail, manifest, job, folder_config)
        if seed is None:
            raise
        origin = _origin(proof, seed)
    binding=getattr(bo,'observation_binding',None)
    service = binding() if callable(binding) else None
    key = signature({'current': proof, 'origin': origin, 'service': service})
    if cache_ok and _original_revoked(bo, expected, proof, service, origin):
        raise NativeMediaVerificationRequired('Native original-file verification failed; explicit media verification is required')
    cache = getattr(bo, '_native_full_hash_cache', {})
    digests = cache.get(key) if cache_ok and origin is not None else None
    from media_observation import ObservationStore
    from io_metrics import increment
    observations=ObservationStore(getattr(bo,'observation_root',None))
    observation_key=expected['abs_id']
    if cache_ok and origin is not None and digests is None:
        saved=observations.get('native-readalong',observation_key,key)
        if isinstance(saved,dict) and saved.get('invalid') is True:
            raise ValueError('Native full-file SHA256 changed; a new completed generation job is required')
        if (isinstance(saved,dict) and set(saved)=={'source','overlay'}
                and all(isinstance(saved[r],str) and re.fullmatch('[0-9a-f]{64}',saved[r]) for r in saved)):
            digests=saved
            increment(getattr(bo,'observation_metrics',bo),'native_observation_durable_hits')
    physically_checked = digests is None
    if physically_checked:
        if logical_only:
            raise NativeMediaVerificationRequired('Native media verification required for current file version')
        digests = {}
        for role in ('source', 'overlay'):
            pin = proof[role]['pin']
            try:digest = bo.file_sha256(pin['overlay_file_id'], pin['overlay_size'])
            except Exception as error:
                if _definitive_original_failure(error):_revoke_original(bo, expected, proof, service, origin)
                raise
            if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
                _revoke_original(bo, expected, proof, service, origin)
                raise ValueError('Native original-file SHA256 could not be verified')
            digests[role] = digest
        # The download server does not lock files. Re-read all metadata/job/
        # audio evidence; reject a concurrent content-version or identity edit.
        fresh_detail, fresh_manifest = bo.detail(expected['bo_id']), bo.manifest(expected['bo_id'])
        fresh_capability, fresh_job = _read_job(bo, expected['bo_id'])
        fresh, fresh_mapping = _inspect(expected, abs_item, fresh_detail, fresh_manifest, fresh_capability, fresh_job, folder_config)
        if fresh != proof or fresh_mapping != current:
            raise ValueError('Native evidence changed during full-file hashing')
    if origin is not None:
        if any(digests[role] != origin[role]['sha256'] for role in ('source', 'overlay')):
            _revoke_original(bo, expected, proof, service, origin)
            observations.put('native-readalong',observation_key,key,{'invalid':True})
            raise ValueError('Native full-file SHA256 changed; a new completed generation job is required')
        result = copy.deepcopy(origin)  # Preserve the actual initially witnessed API times.
    else:
        result = copy.deepcopy(proof)
        result['schema'] = 2
        result['sha256_evidence'] = {'kind': 'publication'}
        for role in ('source', 'overlay'):
            result[role]['sha256'] = digests[role]
    if origin is not None:
        # Unchanged current API evidence reuses historical hashes in idle and
        # progress-only transfers. Explicit physical validation replaces a
        # failed checkpoint only after verifying the original bytes again.
        if observations.get('native-readalong',observation_key,key)!=digests:
            observations.put('native-readalong',observation_key,key,digests)
        cache[key] = dict(digests)
        if len(cache) > 256:
            cache.pop(next(iter(cache)))
        bo._native_full_hash_cache = cache
    if physically_checked:observations.forget('native-revocation', observation_key)
    return result, current


def collect(bo, expected, abs_item, detail, manifest, *, trusted=None, cache_ok=False, logical_only=False):
    """Validate current APIs against historical bytes, or explicitly read bytes.

    logical_only never downloads original files, including on a cache miss.
    Enrollment, changed-version preparation and manual checks may rebuild them.
    """
    capability, job = _read_job(bo, expected['bo_id'])
    return _collect_hashed(bo, expected, abs_item, detail, manifest, capability, job, trusted, cache_ok,
                           logical_only=logical_only)


def collect_for_review(bo, expected, abs_item, detail, manifest):
    """Read untrusted current bytes for an explicit human review, never enroll.

    This deliberately does not infer historical SHA continuity. It preserves
    every structural/job/source/audio check and returns schema 1, which cannot
    authorize automatic synchronization. Only manual_review.approve can create
    a new explicit user-review origin after another identical full read.
    """
    folder_config = getattr(bo, 'folder_config', None)
    capability, job = _read_job(bo, expected['bo_id'])
    proof, current = _inspect(expected, abs_item, detail, manifest, capability, job, folder_config)
    _timestamp(proof['completed_at'])
    binding = getattr(bo, 'observation_binding', None)
    service = binding() if callable(binding) else None
    hashes = {}
    for role in ('source', 'overlay'):
        pin = proof[role]['pin']
        try:digest = bo.file_sha256(pin['overlay_file_id'], pin['overlay_size'])
        except Exception as error:
            if _definitive_original_failure(error):_revoke_original(bo, expected, proof, service)
            raise
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            _revoke_original(bo, expected, proof, service)
            raise ValueError('Native original-file SHA256 could not be verified')
        hashes[role] = digest
    fresh_detail, fresh_manifest = bo.detail(expected['bo_id']), bo.manifest(expected['bo_id'])
    fresh_capability, fresh_job = _read_job(bo, expected['bo_id'])
    fresh, fresh_mapping = _inspect(expected, abs_item, fresh_detail, fresh_manifest, fresh_capability, fresh_job, folder_config)
    if fresh != proof or fresh_mapping != current:
        raise ValueError('Native evidence changed during manual review hashing')
    for role in ('source', 'overlay'):
        proof[role]['sha256'] = hashes[role]
    return proof, current


def collect_new(bo, abs_item, detail, manifest, *, trusted=None, folder_config=None):
    """Construct a new pair using the same evidence as ordinary audio discovery.

    No historical identity is invented: the discovery caller must independently
    observe the full returned mapping and proof twice before registration.
    """
    folder_config = folder_config if folder_config is not None else getattr(bo, 'folder_config', None)
    capability, job = _read_job(bo, detail['id'])
    try:
        source_id = _positive_id(job['input']['epubFileId'])
        selected = _positive_id(detail['readAloudSync']['overlayFileId'])
        audio_detail = dict(detail, readAloudSync={'state': 'unavailable'},
                            files=[f for f in detail['files'] if f['id'] not in (source_id, selected)])
        expected = build_mapping(abs_item, audio_detail, manifest, folder_config=folder_config)
        return _collect_hashed(bo, expected, abs_item, detail, manifest, capability, job, trusted, False, folder_config)
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Incomplete native discovery evidence') from None


def _read_job(bo, book_id):
    book_id = _positive_id(book_id)
    capability = bo._request('GET', f'/api/v1/books/{book_id}/read-aloud',
                             '/api/v1/books/{book_id}/read-aloud')
    rows = capability.get('existingReadAlongs') if isinstance(capability, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Native read-along selection is ambiguous')
    job_id = rows[0].get('generatedByJobId')
    try:
        if not isinstance(job_id, str) or str(UUID(job_id, version=4)) != job_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('Selected read-along has no official generation job') from None
    job = bo._request('GET', f'/api/v1/read-aloud-jobs/{job_id}', '/api/v1/read-aloud-jobs/{job_id}')
    return capability, job


def accepted_files(connection):
    """IDs reserved from unrelated ebook sync; callers must not use this as write authority."""
    result = {}
    for book_id, encoded in connection.execute('SELECT bo_id,proof FROM native_readalong_enrollments'):
        proof = json.loads(encoded)
        result[book_id] = {proof[role]['pin']['overlay_file_id'] for role in ('source', 'overlay')}
    return result
