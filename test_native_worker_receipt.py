"""The worker receipt permits late discovery, never retroactive trust replacement."""
import copy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_discovery as native
from test_native_discovery import evidence, JOB_ID
from test_sync import FakeBO, FakeABS
import sync


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


class WorkerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'BOOK_PROGRESS_WORKER_WITNESS_DIR': str(self.root)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.expected, self.ai, self.detail, self.manifest, self.cap, self.job = evidence()
        for asset, file in zip(self.manifest['assets'], self.expected['files']):
            asset['etag'] = hashlib.sha256(f"{asset['assetId'][4:]}:{file['size']}:{int(file['mtimeMs'])}".encode()).hexdigest()
        self.expected['bo_assets'][0]['etag'] = self.manifest['assets'][0]['etag']
        self.detail['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.job['createdAt'] = '2026-10-31T00:50:00.000Z'
        self.job['input'].update(language='en', granularity='sentence')
        self.job['decisions'] = {'chapters': {}, 'audio': []}
        self.bo = FakeBO(self.detail, self.manifest)
        self.bo._request = lambda method, path, label: copy.deepcopy(self.cap if path.endswith('/read-aloud') else self.job)
        self.hashes = {19: 'a' * 64, 20: 'b' * 64}
        self.reads = []
        def file_hash(file_id, size):
            self.reads.append(file_id)
            return self.hashes[file_id]
        self.bo.file_sha256 = file_hash
        self.receipt = self.fixture()
        self.write_receipt()

    def fixture(self):
        def stat(path, size, mtime=1793408400000000000, digest=None):
            row = dict(path=path, size=size, mtime_ns=mtime, ctime_ns=mtime, dev=100, ino=200)
            if digest: row['sha256'] = digest
            return row
        source = stat(self.detail['files'][2]['absolutePath'], 123, digest='b' * 64)
        audio = [stat(file['path'], file['size'], int(file['mtimeMs'] * 1_000_000)) for file in self.expected['files']]
        directory = '/app/data/read-aloud/jobs/' + JOB_ID
        output = stat(directory + '/output.epub', 4321, digest='a' * 64)
        request = dict(jobId=JOB_ID, epubPath=source['path'], audioPaths=[x['path'] for x in audio],
            workDir=directory, language='en', granularity='sentence', model='quartznet15x5', sourceBytes=1123,
            audioDurationSeconds=100, decisions=copy.deepcopy(self.job['decisions']), resumeFrom=None, maxOutputBytes=1_000_000)
        markers = {stage: dict(stage=stage, inputs={}, fingerprint='e' * 64,
            finishedAt='2026-10-31T01:00:30.000Z', durationMs=20)
            for stage in ('aligning', 'preparing_audio', 'marking_up_ebook')}
        return dict(schema=1, kind='official-worker-completion', job_id=JOB_ID,
            generation_id='22345678-1234-4234-8234-123456789abc',
            request=request, request_sha256=hashlib.sha256(encoded(request)).hexdigest(),
            input_captured_at='2026-10-31T00:55:00.000Z', completed_witness_at='2026-10-31T01:00:40.000Z',
            source=source, audio=audio, output=output, report={'sha256': 'f' * 64, 'size': 900},
            markers=markers, worker={'job_id': JOB_ID, 'status': 'completed', 'output_bytes': 4321,
                'accepted': {'seq': 1, 'attempt': 1, 'at': '2026-10-31T00:55:01.000Z'}})

    def write_receipt(self):
        self.path = self.root / (JOB_ID + '.json')
        self.path.write_bytes(encoded(self.receipt) + b'\n')
        self.path.chmod(0o600)

    def collect(self, trusted=None):
        return native.collect(self.bo, self.expected, self.ai, self.bo.detail(23), self.bo.manifest(23), trusted=trusted)

    def test_late_first_observation_uses_early_receipt_and_keeps_actual_api_time(self):
        proof, current = self.collect()
        self.assertEqual(current, self.expected)
        self.assertEqual(proof['overlay']['content_version'], '2026-10-31T02:00:00.000Z')
        self.assertEqual(proof['sha256_evidence']['kind'], 'worker-artifact')
        self.assertEqual(proof['sha256_evidence']['receipt_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.reads, [20, 19])
        self.path.unlink()
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T03:00:00.000Z'
        self.assertEqual(self.collect(trusted=proof)[0], proof)

    def test_missing_receipt_preserves_strict_failure(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, 'after its completed'):
            self.collect()
        self.assertEqual(self.reads, [])

    def test_late_receipt_cannot_pretend_it_saw_publication(self):
        self.receipt['completed_witness_at'] = '2026-10-31T03:00:00.000Z'; self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'before publication'):
            self.collect()
        self.assertEqual(self.reads, [])

    def test_worker_receipt_never_rescues_changed_accepted_full_sha(self):
        proof, _ = self.collect()
        self.hashes[19] = 'c' * 64
        self.receipt['output']['sha256'] = 'c' * 64; self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'SHA256 changed'):
            self.collect(trusted=proof)

    def test_output_or_source_digest_mismatch_fails(self):
        for file_id in (19, 20):
            with self.subTest(file_id=file_id):
                old = self.hashes[file_id]; self.hashes[file_id] = 'c' * 64
                with self.assertRaisesRegex(ValueError, 'SHA256 changed'): self.collect()
                self.hashes[file_id] = old

    def test_input_and_job_identity_mismatches_fail_before_download(self):
        mutations = [lambda r: r.update(job_id='22345678-1234-4234-8234-123456789abc'),
            lambda r: r['source'].update(path='/audiobooks/Other/source.epub'),
            lambda r: r['audio'][0].update(size=1001),
            lambda r: r['audio'][0].update(mtime_ns=r['audio'][0]['mtime_ns']+2_000_000),
            lambda r: r['output'].update(path='/app/data/read-aloud/jobs/other/output.epub'),
            lambda r: r['worker'].update(status='running'),
            lambda r: r['worker']['accepted'].update(at='2026-10-31T00:54:00.000Z'),
            lambda r: r['worker']['accepted'].update(attempt=0),
            lambda r: r.update(generation_id=JOB_ID.upper()),
            lambda r: r['request'].update(decisions={'chapters': {'wrong': True}, 'audio': []}),
            lambda r: r.update(request_sha256='d' * 64)]
        original = copy.deepcopy(self.receipt)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.receipt = copy.deepcopy(original); mutate(self.receipt); self.write_receipt()
                with self.assertRaises((ValueError, KeyError)): self.collect()
        self.assertEqual(self.reads, [])

    def test_job_decisions_and_bookorbit_audio_etag_must_match(self):
        self.job['decisions'] = {'chapters': {'wrong': True}, 'audio': []}
        with self.assertRaisesRegex(ValueError, 'decisions'): self.collect()
        self.job['decisions'] = self.receipt['request']['decisions']
        self.bo.bm['assets'][0]['etag'] = 'c' * 64
        self.expected['bo_assets'][0]['etag'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'BookOrbit file evidence'): self.collect()

    def test_unsafe_or_ambiguous_receipt_files_fail(self):
        self.path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'Unsafe'): self.collect()
        self.write_receipt()
        self.path.write_bytes(b'{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'): self.collect()
        self.path.unlink(); self.path.symlink_to('/dev/null')
        with self.assertRaises(OSError): self.collect()

    def test_no_configured_directory_does_not_search_anywhere(self):
        with patch.dict(os.environ, {'BOOK_PROGRESS_WORKER_WITNESS_DIR': ''}):
            with self.assertRaisesRegex(ValueError, 'after its completed'): self.collect()

    def test_receipt_size_limit_is_checked_before_loading(self):
        with self.path.open('wb') as handle:
            handle.truncate(32 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'Unsafe'): self.collect()
        self.assertEqual(self.reads, [])

    def test_unicode_source_path_preserves_agreed_request_digest(self):
        path = '/audiobooks/Example/Quellbuch-ä.epub'
        self.bo.bd['files'][2]['absolutePath'] = path
        self.receipt['source']['path'] = self.receipt['request']['epubPath'] = path
        self.receipt['request_sha256'] = hashlib.sha256(encoded(self.receipt['request'])).hexdigest()
        self.write_receipt()
        self.assertEqual(self.collect()[0]['source']['pin']['overlay_path'], path)

    def test_real_enrollment_state_requires_second_observation_without_progress_writes(self):
        ab = FakeABS(self.ai); ab.set(10); self.bo.set(10)
        config = dict(abs_url='http://abs', bo_url='http://bo', abs_version='2.37.1',
            mappings=[self.expected], auto_native_readalong={'enabled': True, 'stable_seconds': 30})
        state = self.root / 'state'; state.mkdir()
        clock = [1000]
        with patch('sync.ABSClient', return_value=ab), patch('sync.BOClient', return_value=self.bo):
            bridge = sync.Bridge(state, config, dict(abs_token='dummy', bo_username='dummy', bo_password='dummy'),
                                 clock=lambda: clock[0])
        self.addCleanup(bridge.db.close)
        baseline = {'baseline': {'abs': sync.normalize_abs(ab.raw, self.expected),
                                'bo': sync.normalize_bo(self.bo.raw, self.bo.bd, self.expected)}}
        bridge.save(self.expected['abs_id'], baseline)
        def cycle():
            (state / 'deployment.json').write_text(json.dumps(dict(ok=True, checked_at=clock[0])))
            with contextlib.redirect_stdout(io.StringIO()):
                return bridge.cycle(apply=True, settle=0)
        self.assertEqual(cycle()['waiting'], 1)
        self.assertEqual(bridge.db.execute('SELECT COUNT(*) FROM native_readalong_enrollments').fetchone()[0], 0)
        self.path.unlink()  # Durable pending proof already retains the verified origin.
        clock[0] += 30
        self.assertEqual(cycle()['waiting'], 1)
        self.assertEqual(bridge.db.execute('SELECT COUNT(*) FROM native_readalong_enrollments').fetchone()[0], 1)
        self.assertEqual(bridge.load(self.expected['abs_id']), baseline)
        self.assertFalse(ab.writes or self.bo.writes)
        self.assertEqual(cycle()['unchanged'], 1)


if __name__ == '__main__':
    unittest.main()
