"""Read-only native evidence and enrollment without baseline/progress resets."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_discovery
import sync
from mapping import build_mapping
from test_sync import FakeABS, FakeBO, fixture
from test_mapping import fixture as multi_fixture
from test_discovery import FakeABS as DiscoveryABS, FakeBO as DiscoveryBO, ABS_LIBRARY, book as discovery_book
from discovery import discover


JOB_ID = '12345678-1234-4234-8234-123456789abc'


def evidence(base=fixture):
    ai, bd, bm = base()
    expected = build_mapping(ai, bd, bm)
    duration = bm['totalDurationMs'] / 1000
    bd['readAloudSync'] = dict(mode='auto', state='enabled', unavailableReason=None, overlayFileId=19,
                              audioDurationSeconds=duration, overlayDurationSeconds=duration,
                              durationDifferenceSeconds=0, durationDifferenceRatio=0)
    for id, name, size, overlay in ((19, 'CD 0/generated.readaloud.epub', 4321, True),
                                     (20, 'source.epub', 123, False)):
        bd['files'].append(dict(id=id, absolutePath='/audiobooks/Example/' + name,
                                sizeBytes=size, format='epub', role='content',
                                fileHash=str(id)[0] * 32, contentVersion='2026-10-31T01:00:00.000Z',
                                mediaOverlay={'available': overlay}))
    cap = dict(existingReadAlongs=[dict(fileId=19, sizeBytes=4321, generatedByJobId=JOB_ID)], latestJob=None)
    job = dict(id=JOB_ID, bookId=23, outputFileId=19, status='completed', stage='completed',
               errorCode=None, cancelRequested=False, completedAt='2026-10-31T01:01:00.000Z',
               input=dict(epubFileId=20, audioFileIds=[x['fileId'] for x in bm['assets']],
                          sourceBytes=123 + sum(x['sizeBytes'] for x in bm['assets'])))
    return expected, ai, bd, bm, cap, job


class NativeEvidenceTests(unittest.TestCase):
    def test_completed_job_exempts_only_its_source_and_preserves_audio_mapping(self):
        values = evidence(multi_fixture)
        before = copy.deepcopy(values)
        proof, current = native_discovery.inspect(*values)
        self.assertEqual(current, values[0])
        self.assertEqual(proof['source']['pin']['overlay_file_id'], 20)
        self.assertEqual(proof['overlay']['pin']['overlay_file_id'], 19)
        self.assertEqual(values, before)

    def test_incomplete_reordered_or_duplicate_job_audio_is_rejected(self):
        for ids in ([11], [12, 11], [11, 11], [11, 12, 13], [True, 12]):
            values = evidence(multi_fixture)
            values[-1]['input']['audioFileIds'] = ids
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                native_discovery.inspect(*values)

    def test_current_audio_must_equal_all_historical_identity_evidence(self):
        for change in ('mtime', 'etag', 'chapter', 'path', 'size', 'duration', 'library'):
            expected, ai, bd, bm, cap, job = evidence()
            if change == 'mtime': ai['media']['tracks'][0]['metadata']['mtimeMs'] += 1
            if change == 'etag': bm['assets'][0]['etag'] = 'c' * 64
            if change == 'chapter': bm['chapters'] = [dict(title='New', assetId='aud_1', sequence=0,
                                                          startMs=0, endMs=1000, assetOffsetMs=0)]
            if change == 'path':
                ai['media']['tracks'][0]['metadata']['path'] = bd['files'][0]['absolutePath'] = '/audiobooks/Example/new.mp3'
            if change == 'size':
                ai['media']['tracks'][0]['metadata']['size'] += 1
                bd['files'][0]['sizeBytes'] += 1; bm['assets'][0]['sizeBytes'] += 1
                job['input']['sourceBytes'] += 1
            if change == 'duration':
                ai['media']['tracks'][0]['duration'] += 0.1; ai['media']['duration'] += 0.1
            if change == 'library': bd['libraryId'] = 2
            with self.subTest(change=change), self.assertRaises(ValueError):
                native_discovery.inspect(expected, ai, bd, bm, cap, job)

    def test_missing_historical_identity_evidence_is_not_invented(self):
        for field in ('bo_assets', 'bo_chapters'):
            values = evidence(); del values[0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                native_discovery.inspect(*values)

    def test_unknown_mode_wrong_selected_overlay_and_incompatible_duration_fail(self):
        for field, value in (('mode', 'disabled'), ('mode', 'unknown'), ('state', 'unavailable'),
                             ('overlayFileId', 20), ('unavailableReason', 'duration_mismatch'),
                             ('overlayDurationSeconds', 20)):
            values = evidence(); values[2]['readAloudSync'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                native_discovery.inspect(*values)

    def test_failed_unpublished_wrong_book_or_imported_outputs_are_refused(self):
        for field, value in (('status', 'awaiting_review'), ('status', 'failed'), ('bookId', 24),
                             ('outputFileId', 21), ('stage', 'publishing'), ('cancelRequested', True),
                             ('errorCode', 'unknown')):
            values = evidence(); values[-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                native_discovery.inspect(*values)
        values = evidence(); values[-2]['existingReadAlongs'][0]['generatedByJobId'] = None
        with self.assertRaises(ValueError): native_discovery.inspect(*values)

    def test_multiple_overlays_active_jobs_and_unrelated_ebooks_are_refused(self):
        for change in ('multiple', 'active', 'extra_epub', 'extra_pdf', 'source_overlay', 'missing_source', 'source_bytes'):
            values = evidence(); _, _, bd, _, cap, job = values
            if change == 'multiple': cap['existingReadAlongs'].append(dict(cap['existingReadAlongs'][0], fileId=21))
            if change == 'active': cap['latestJob'] = {'status': 'processing'}
            if change.startswith('extra_'):
                ext = change.removeprefix('extra_')
                bd['files'].append(dict(id=21, absolutePath='/audiobooks/Example/extra.' + ext,
                                        sizeBytes=1, format=ext))
            if change == 'source_overlay': bd['files'][-1]['mediaOverlay']['available'] = True
            if change == 'missing_source': bd['files'].pop()
            if change == 'source_bytes': job['input']['sourceBytes'] += 1
            with self.subTest(change=change), self.assertRaises(ValueError): native_discovery.inspect(*values)

    def test_plain_source_must_have_bound_content_identity_inside_same_book(self):
        for field, value in (('absolutePath', '/audiobooks/Other/source.epub'), ('fileHash', None),
                             ('contentVersion', 'bad'), ('role', 'sidecar'), ('id', 19)):
            values = evidence(); values[2]['files'][-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): native_discovery.inspect(*values)

    def test_completed_with_warnings_and_revision_only_changes_are_supported(self):
        values = evidence(); values[-1]['status'] = 'completed_with_warnings'
        proof, _ = native_discovery.inspect(*values)
        values[3]['revision'] = 'b' * 64
        revised, mapping = native_discovery.inspect(*values)
        self.assertEqual(proof, revised)
        self.assertEqual(mapping['bo_revision'], 'b' * 64)

    def test_output_version_after_completed_job_is_not_accepted_as_native(self):
        values = evidence(); values[2]['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        with self.assertRaisesRegex(ValueError, 'after its completed'):
            native_discovery.inspect(*values)


class NativeEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mapping, ai, bd, bm, self.cap, self.job = evidence()
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.bo._request = self.request
        self.hashes = {19: 'a' * 64, 20: 'b' * 64}
        self.hash_reads = []
        def file_hash(file_id, size):
            self.hash_reads.append((file_id, size))
            return self.hashes[file_id]
        self.bo.file_sha256 = file_hash
        self.requests = []; self.clock = 1000
        self.config = dict(abs_url='http://abs', bo_url='http://bo', abs_version='2.37.1',
                           mappings=[self.mapping], auto_native_readalong={'enabled': True, 'stable_seconds': 30})
        with patch('sync.ABSClient', return_value=self.abs), patch('sync.BOClient', return_value=self.bo):
            self.bridge = sync.Bridge(self.root, self.config,
                                      dict(abs_token='dummy', bo_username='dummy', bo_password='dummy'),
                                      clock=lambda: self.clock)
        self.addCleanup(self.bridge.db.close)
        self.abs.set(10); self.bo.set(10)
        pair = {'abs': sync.normalize_abs(self.abs.raw, self.mapping),
                'bo': sync.normalize_bo(self.bo.raw, self.bo.bd, self.mapping)}
        self.initial_state = dict(baseline=pair, candidate={'signature': 'preserve', 'since': 900})
        self.bridge.save('abs-book', self.initial_state)

    def request(self, method, path, label, **kwargs):
        self.assertEqual(method, 'GET'); self.requests.append(path)
        return copy.deepcopy(self.cap if path.endswith('/read-aloud') else self.job)

    def cycle(self, apply=True):
        (self.root / 'deployment.json').write_text(json.dumps(dict(ok=True, checked_at=self.clock)))
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=apply, settle=0)

    def accept(self):
        self.assertEqual(self.cycle()['waiting'], 1)
        self.clock += 30
        self.assertEqual(self.cycle()['waiting'], 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 1)

    def count(self, table):
        return self.bridge.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_two_observations_enroll_without_any_state_or_progress_write(self):
        self.accept()
        self.assertEqual(self.bridge.load('abs-book'), self.initial_state)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.count('operations'), 0)
        self.assertEqual(native_discovery.accepted_files(self.bridge.db), {23: {19, 20}})
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(self.bridge.list_mappings(), [self.mapping])

    def test_ordinary_new_audio_progress_flows_only_after_enrollment(self):
        self.accept(); self.abs.set(25)
        result = self.cycle()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(self.bo.raw['positionMs'], 25000)

    def test_conflicting_new_progress_is_not_overwritten_by_enrollment(self):
        self.abs.set(25); self.bo.set(35)
        self.accept(); result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.load('abs-book')['baseline'], self.initial_state['baseline'])

    def test_pending_operation_blocks_enrollment_and_retains_journal_baseline(self):
        self.bridge.write_record('pending', 'abs-book', {'goal': {'position': 25, 'status': 'in_progress'}})
        result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.count('native_readalong_observations'), 0)
        self.assertEqual(self.count('operations'), 1)
        self.assertEqual(self.bridge.load('abs-book')['baseline'], self.initial_state['baseline'])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_unknown_baseline_is_not_replaced(self):
        self.initial_state['baseline']['bo']['fingerprint'] = 'b' * 64
        self.bridge.save('abs-book', self.initial_state)
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertEqual(self.count('native_readalong_observations'), 0)

    def test_dry_run_cannot_start_timer_or_enroll_or_change_baseline(self):
        self.assertEqual(self.cycle(apply=False)['waiting'], 1)
        self.clock += 30
        self.assertEqual(self.cycle(apply=False)['waiting'], 1)
        self.assertEqual(self.count('native_readalong_observations'), 0)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.bridge.load('abs-book'), self.initial_state)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_failed_observation_resets_stability(self):
        self.cycle(); self.clock += 30
        self.job['status'] = 'failed'; self.cycle()
        self.assertEqual(self.count('native_readalong_observations'), 0)
        self.job['status'] = 'completed'
        self.assertEqual(self.cycle()['waiting'], 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)

    def test_metadata_version_change_requires_preparation_before_direct_apply(self):
        self.accept()
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T01:00:30.000Z'
        with self.assertRaises(native_discovery.NativeMediaVerificationRequired):
            self.bridge.apply(self.mapping,pair,raw,'abs_to_bo',self.initial_state['baseline'])
        self.bridge.prepare_native_readalong(self.mapping,True)
        result = self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', self.initial_state['baseline'])
        self.assertEqual(result['bo']['position'], 25)
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        self.assertEqual(proof['overlay']['content_version'], '2026-10-31T01:00:00.000Z')
        self.assertEqual(proof['overlay']['sha256'], 'a' * 64)

    def test_metadata_after_completion_rehashes_once_and_keeps_origin(self):
        self.accept()
        original = self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0]
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.hash_reads.clear()
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(len(self.hash_reads), 2)
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(len(self.hash_reads), 2)
        self.assertEqual(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0],original)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_same_partial_md5_size_but_changed_full_bytes_is_blocked(self):
        for role, index, file_id in [('overlay',1,19),('source',2,20)]:
            with self.subTest(role=role):
                self.setUp();self.accept()
                self.bo.bd['files'][index]['contentVersion']='2026-10-31T02:00:00.000Z'
                self.hashes[file_id]='c'*64
                self.abs.set(25)
                result=self.cycle()
                self.assertEqual(len(result['blocked']),1)
                self.assertIn('SHA256 changed',result['blocked'][0]['reason'])
                self.assertFalse(self.abs.writes or self.bo.writes)

    def test_pending_strict_witness_survives_late_metadata_and_restart(self):
        self.assertEqual(self.cycle()['waiting'],1)
        self.assertEqual(self.count('native_readalong_witnesses'),1)
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T02:00:00.000Z'
        self.bridge.db.close()
        with patch('sync.ABSClient',return_value=self.abs),patch('sync.BOClient',return_value=self.bo):
            self.bridge=sync.Bridge(self.root,self.config,dict(abs_token='dummy',bo_username='dummy',bo_password='dummy'),clock=lambda:self.clock)
        self.addCleanup(self.bridge.db.close)
        self.clock+=30
        self.assertEqual(self.cycle()['waiting'],1)
        self.assertEqual(self.count('native_readalong_enrollments'),1)
        self.assertEqual(self.count('native_readalong_witnesses'),0)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_legacy_proof_cannot_adopt_late_bytes_without_review(self):
        self.accept()
        proof=json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        proof['schema']=1;proof.pop('sha256_evidence')
        for role in ('overlay','source'):proof[role].pop('sha256')
        with self.bridge.db:self.bridge.db.execute('UPDATE native_readalong_enrollments SET proof=?',(json.dumps(proof),))
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T02:00:00.000Z'
        self.hash_reads.clear()
        self.assertEqual(len(self.cycle()['blocked']),1)
        self.assertFalse(self.hash_reads or self.abs.writes or self.bo.writes)

    def test_reviewed_existing_provenance_preserves_origin_and_requires_both_full_hashes(self):
        self.accept()
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        proof['sha256_evidence'] = {'kind': 'reviewed-existing', 'review_sha256': 'e' * 64}
        with self.bridge.db:
            self.bridge.db.execute('UPDATE native_readalong_enrollments SET proof=?', (sync.dump(proof),))
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0]), proof)
        self.bo.bd['files'][2]['contentVersion'] = '2026-10-31T02:01:00.000Z'
        self.hashes[20] = 'c' * 64
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_missing_review_provenance_does_not_authorize_late_metadata(self):
        self.accept()
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        proof['sha256_evidence'] = {'kind': 'reviewed-existing'}
        with self.bridge.db:
            self.bridge.db.execute('UPDATE native_readalong_enrollments SET proof=?', (sync.dump(proof),))
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.hash_reads.clear()
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertFalse(self.hash_reads or self.abs.writes or self.bo.writes)

    def test_changed_pending_bytes_never_enroll_or_reset_progress(self):
        self.assertEqual(self.cycle()['waiting'], 1)
        self.clock += 30
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.hashes[19] = 'c' * 64
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.count('native_readalong_observations'), 0)
        self.assertEqual(self.bridge.load('abs-book')['baseline'], self.initial_state['baseline'])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_download_bracket_detects_concurrent_version_edit(self):
        old=self.bo.file_sha256
        def changed(file_id,size):
            result=old(file_id,size)
            if file_id==19:self.bo.bd['files'][1]['contentVersion']='2026-10-31T01:00:30.000Z'
            return result
        self.bo.file_sha256=changed
        result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('during full-file hashing',result['blocked'][0]['reason'])
        self.assertEqual(self.count('native_readalong_witnesses'),0)

    def test_backward_timestamp_does_not_reuse_cache(self):
        self.accept()
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T00:59:59.000Z'
        self.assertEqual(len(self.cycle()['blocked']),1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_changed_output_cannot_reenroll_under_old_job_even_after_waiting(self):
        self.accept()
        self.bo.bd['files'][1]['fileHash'] = 'b' * 32
        for _ in range(2):
            self.clock += 30
            self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.load('abs-book')['baseline'], self.initial_state['baseline'])

    def test_new_completed_replacement_job_can_enroll_without_resetting_audio(self):
        self.accept()
        new_job = '12345678-1234-4234-8234-123456789abd'
        self.cap['existingReadAlongs'][0]['generatedByJobId'] = new_job
        self.job.update(id=new_job, completedAt='2026-10-31T02:01:00.000Z')
        self.bo.bd['files'][1].update(fileHash='b' * 32, contentVersion='2026-10-31T02:00:00.000Z')
        self.accept()
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        self.assertEqual(proof['job_id'], new_job)
        self.assertEqual(self.bridge.load('abs-book'), self.initial_state)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_restart_preserves_candidate_and_acceptance_without_progress(self):
        self.cycle()
        self.bridge.db.close()
        with patch('sync.ABSClient', return_value=self.abs), patch('sync.BOClient', return_value=self.bo):
            self.bridge = sync.Bridge(self.root, self.config,
                                      dict(abs_token='dummy', bo_username='dummy', bo_password='dummy'),
                                      clock=lambda: self.clock)
        self.addCleanup(self.bridge.db.close)
        self.clock += 30
        self.assertEqual(self.cycle()['waiting'], 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 1)
        self.assertEqual(self.bridge.load('abs-book'), self.initial_state)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_identity_change_during_final_reread_cannot_commit(self):
        self.cycle(); self.clock += 30
        original = self.bo._request
        count = [0]
        def changing_request(method, path, label, **kwargs):
            count[0] += 1
            if count[0] == 2:
                self.bo.bd['files'][1]['fileHash'] = 'b' * 32
            return original(method, path, label, **kwargs)
        self.bo._request = changing_request
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.count('native_readalong_observations'), 0)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_changed_source_after_prepare_is_caught_before_progress_write(self):
        self.accept(); self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.bo.bd['files'][-1]['fileHash'] = 'b' * 32
        with self.assertRaises(ValueError):
            self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', self.initial_state['baseline'])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_disabled_default_and_explicit_pin_are_not_reinterpreted(self):
        self.config.pop('auto_native_readalong')
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertFalse(self.requests)
        self.config['auto_native_readalong'] = {'enabled': True}
        self.config['native_readalong'] = {'abs-book': dict(bo_id=23, overlay_file_id=19,
            overlay_path='/audiobooks/Example/CD 0/generated.readaloud.epub', overlay_size=4321)}
        # The original manual pin still rejects an additional plain EPUB.
        self.assertEqual(len(self.cycle()['blocked']), 1)
        self.assertFalse(self.requests)

    def test_paused_mapping_does_not_observe_or_enroll(self):
        self.config['paused_mappings'] = {'abs-book': 'User pause'}
        self.assertEqual(len(self.cycle()['paused']), 1)
        self.assertFalse(self.requests)
        self.assertEqual(self.bridge.load('abs-book'), self.initial_state)

    def test_revision_only_reconciliation_keeps_progress_after_enrollment(self):
        self.accept(); self.bo.bm['revision'] = 'b' * 64
        result = self.cycle()
        self.assertEqual(result['revisions_updated'], 1, result)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.abs.raw['currentTime'], 10)
        self.assertEqual(self.bo.raw['positionMs'], 10000)

    def test_invalid_settings_fail_before_any_api_read(self):
        for settings in (None, [], {'enabled': 1}, {'enabled': True, 'unknown': 1}, {'stable_seconds': 0}):
            self.config['auto_native_readalong'] = settings
            with self.subTest(settings=settings), patch.object(self.abs, 'version') as version:
                with self.assertRaises(ValueError): self.cycle()
                version.assert_not_called()

    def test_native_title_arriving_before_pairing_registers_audio_and_proof_atomically(self):
        self.config['mappings'] = []
        self.config['auto_discovery'] = dict(enabled=True, abs_library_id=ABS_LIBRARY,
                                            bo_library_ids=[1], stable_seconds=300, interval_seconds=300)
        with self.bridge.db:
            self.bridge.db.execute('DELETE FROM state WHERE id=?', ('abs-book',))
        self.abs.ai.update(libraryId=ABS_LIBRARY, mediaType='book', isMissing=False, isInvalid=False)
        self.bo.bd['status'] = 'present'
        self.abs.libraries = lambda: [dict(id=ABS_LIBRARY, mediaType='book')]
        self.abs.inventory = lambda _: [copy.deepcopy(self.abs.ai)]
        self.bo.query = lambda: [dict(id=23, libraryId=1, status='present')]
        first = self.cycle()
        self.assertEqual(first['mapped'], 0)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.clock += 300
        second = self.cycle()
        self.assertEqual(second['mapped'], 1, second)
        self.assertEqual(second['waiting'], 1)
        self.assertEqual(self.count('auto_mappings'), 1)
        self.assertEqual(self.count('native_readalong_enrollments'), 1)
        self.assertEqual(self.bridge.load('abs-book'), {})
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(native_discovery.accepted_files(self.bridge.db), {23: {19, 20}})
        self.clock += 1
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(self.bridge.load('abs-book')['baseline']['abs']['position'], 10)
        self.assertFalse(self.abs.writes or self.bo.writes)


class NativeNewPairDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.expected, ai, bd, bm, self.cap, self.job = evidence(multi_fixture)
        ai.update(libraryId=ABS_LIBRARY, mediaType='book', isMissing=False, isInvalid=False)
        bd['status'] = 'present'
        self.abs, self.bo = DiscoveryABS([(ai, bd, bm)]), DiscoveryBO([(ai, bd, bm)])
        self.bo._request = lambda method, path, label: copy.deepcopy(self.cap if path.endswith('/read-aloud') else self.job)
        self.bo.file_sha256 = lambda file_id,size: ('a' if file_id==19 else 'b')*64

    def scan(self, previous=None, now=1000, enabled=True):
        return discover(self.abs, self.bo, [], ABS_LIBRARY, [7], previous or {}, now,
                        stable_seconds=300, native_readalong_enabled=enabled)

    def test_new_mixed_title_waits_for_full_stable_job_and_audio_proof(self):
        first = self.scan()
        self.assertTrue(first['scan_ok']); self.assertEqual(first['summary']['waiting'], 1)
        self.assertEqual(first['new_mappings'], [])
        second = self.scan(first['candidates'], now=1300)
        self.assertEqual(second['new_mappings'], [self.expected])
        self.assertEqual(set(second['native_readalongs']), {'abs-book'})
        self.assertEqual(second['native_readalongs']['abs-book']['source']['pin']['overlay_file_id'], 20)

    def test_new_mixed_title_stays_blocked_without_opt_in(self):
        result = self.scan(enabled=False)
        self.assertEqual(result['new_mappings'], [])
        self.assertEqual(result['summary']['blocked'], 1)
        self.assertEqual(result['native_readalongs'], {})

    def test_changed_published_identity_does_not_reuse_pending_hash_witness(self):
        for field in ('overlay', 'source', 'audio'):
            self.setUp(); first = self.scan()
            if field == 'overlay': self.bo.details[23]['files'][2]['fileHash'] = 'b' * 32
            if field == 'source': self.bo.details[23]['files'][3]['fileHash'] = 'c' * 32
            if field == 'audio': self.bo.manifests[23]['assets'][0]['etag'] = 'd' * 64
            with self.subTest(field=field):
                changed = self.scan(first['candidates'], now=1300)
                self.assertEqual(changed['new_mappings'], [])
                self.assertEqual(changed['summary']['blocked'], 1)

    def test_new_pair_pending_witness_accepts_only_identical_late_metadata(self):
        first=self.scan()
        self.bo.details[23]['files'][2]['contentVersion']='2026-10-31T02:00:00.000Z'
        second=self.scan(first['candidates'],now=1300)
        self.assertEqual(second['new_mappings'],[self.expected])
        self.assertEqual(second['native_readalongs']['abs-book'], next(iter(first['native_witnesses'].values())))

    def test_later_inventory_failure_retains_hash_witness_but_resets_timer(self):
        a, b, m = discovery_book(aid='later', bid=24, folder='Z later', asset_suffix='later')
        self.abs.items[a['id']] = a; self.abs.cards.append(a)
        self.bo.details[b['id']] = b; self.bo.cards.append(b); self.bo.manifests[b['id']] = m
        self.bo.fail = ('manifest', 24)
        failed = self.scan()
        self.assertFalse(failed['scan_ok'])
        self.assertEqual(failed['candidates'], {})
        self.assertEqual(failed['new_mappings'], [])
        self.assertEqual(len(failed['native_witnesses']), 1)
        self.bo.fail = None
        self.bo.details[23]['files'][2]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        fresh = discover(self.abs, self.bo, [], ABS_LIBRARY, [7], {}, 1300,
                         native_readalong_enabled=True, native_witnesses=failed['native_witnesses'])
        self.assertTrue(fresh['scan_ok'])
        self.assertEqual(fresh['new_mappings'], [])
        self.assertEqual(fresh['summary']['waiting'], 2)
        ready = discover(self.abs, self.bo, [], ABS_LIBRARY, [7], fresh['candidates'], 1600,
                         native_readalong_enabled=True, native_witnesses=fresh['native_witnesses'])
        self.assertEqual(len(ready['new_mappings']), 2)

    def test_partial_job_source_audio_and_missing_native_provenance_cannot_map_new_title(self):
        for change in ('partial', 'import', 'unpublished', 'post_publish_change'):
            self.setUp()
            if change == 'partial': self.job['input']['audioFileIds'] = [11]
            if change == 'import': self.cap['existingReadAlongs'][0]['generatedByJobId'] = None
            if change == 'unpublished': self.job['status'] = 'awaiting_review'
            if change == 'post_publish_change':
                self.bo.details[23]['files'][2]['contentVersion'] = '2026-10-31T02:00:00.000Z'
            with self.subTest(change=change):
                result = self.scan()
                self.assertEqual(result['new_mappings'], [])
                self.assertEqual(result['candidates'], {})
                self.assertEqual(result['native_readalongs'], {})


if __name__ == '__main__':
    unittest.main()
