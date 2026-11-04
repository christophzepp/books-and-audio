import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import manual_review as review
import native_discovery as native
import sync
from test_native_discovery import evidence
from test_sync import FakeABS, FakeBO


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mapping, ai, bd, bm, self.cap, self.job = evidence()
        bd['files'][1]['contentVersion'] = '2026-10-31T02:00:00.000Z'
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.bo._request = lambda method, path, label: copy.deepcopy(self.cap if path.endswith('/read-aloud') else self.job)
        self.hashes = {19: 'a' * 64, 20: 'b' * 64}; self.hash_reads = []
        def file_hash(file_id, size):
            self.hash_reads.append(file_id)
            return self.hashes[file_id]
        self.bo.file_sha256 = file_hash
        self.clock = dt.datetime(2026, 10, 31, 3, tzinfo=dt.timezone.utc).timestamp()
        config = dict(abs_url='http://abs', bo_url='http://bo', abs_version='2.37.1', mappings=[self.mapping],
                      auto_native_readalong={'enabled': True, 'stable_seconds': 30})
        with patch('sync.ABSClient', return_value=self.abs), patch('sync.BOClient', return_value=self.bo):
            self.bridge = sync.Bridge(self.root, config, dict(abs_token='dummy', bo_username='dummy', bo_password='dummy'), clock=lambda: self.clock)
        self.addCleanup(self.bridge.db.close)
        self.abs.set(10); self.bo.set(10)
        self.baseline = {'baseline': {'abs': sync.normalize_abs(self.abs.raw, self.mapping),
            'bo': sync.normalize_bo(self.bo.raw, self.bo.bd, self.mapping)}, 'candidate': {'signature': 'keep', 'since': self.clock-5}}
        self.bridge.save('abs-book', self.baseline)
        self.bridge.save('unrelated-title', {'keep': True})
        self.gate()

    def gate(self):
        (self.root / 'deployment.json').write_text(json.dumps({'ok': True, 'checked_at': self.clock}))

    def preview(self):
        return review.preview(self.bridge, 'abs-book')

    def approve(self, record):
        return review.approve(self.bridge, record, record['review_digest'])

    def count(self, table):
        return self.bridge.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def assert_untouched(self):
        self.assertEqual(self.bridge.load('abs-book'), self.baseline)
        self.assertEqual(self.bridge.load('unrelated-title'), {'keep': True})
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_preview_is_current_hashed_and_read_only(self):
        record = self.preview()
        self.assertTrue(record['can_approve'], record)
        self.assertEqual(record['files']['source']['sha256'], 'b' * 64)
        self.assertEqual(record['files']['overlay']['file_id'], 19)
        self.assertEqual(record['proof']['schema'], 1)
        self.assertEqual(self.hash_reads, [20, 19])
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertFalse((self.root / 'manual-review-backups').exists())
        self.assert_untouched()

    def test_old_unwitnessed_file_approval_preserves_progress_and_creates_backup(self):
        record = self.preview(); result = self.approve(record)
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(self.hash_reads, [20, 19, 20, 19])
        self.assert_untouched()
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        self.assertEqual(proof['sha256_evidence']['kind'], 'user-review')
        self.assertEqual(proof['overlay']['content_version'], '2026-10-31T02:00:00.000Z')
        self.assertEqual(proof['sha256_evidence']['review_sha256'], record['review_digest'])
        with sqlite3.connect(result['backup']) as backup:
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM native_readalong_enrollments').fetchone()[0], 0)
            self.assertEqual(json.loads(backup.execute('SELECT value FROM state WHERE id=?', ('abs-book',)).fetchone()[0]), self.baseline)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o077, 0)
        current, _ = native.collect(self.bo, self.mapping, self.abs.ai, self.bo.bd, self.bo.bm, trusted=proof)
        self.assertEqual(current, proof)
        with self.assertRaises(review.ReviewRefused): self.approve(record)

    def test_explicit_review_can_adopt_changed_bytes_same_finished_job(self):
        first = self.preview(); self.approve(first)
        self.hashes[19] = 'c' * 64
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:30:00.000Z'
        second = self.preview()
        self.assertTrue(second['can_approve'])
        self.assertTrue(any(d['field'] == 'overlay.sha256' and d['after'] == 'c' * 64 for d in second['differences']))
        self.approve(second)
        proof = json.loads(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0])
        self.assertEqual(proof['overlay']['sha256'], 'c' * 64)
        self.assertEqual(self.count('events'), 2)
        self.assert_untouched()

    def test_forged_or_wrong_digest_or_wrong_target_records_are_refused(self):
        record = self.preview()
        for change in ('digest', 'target', 'hash', 'timestamp'):
            forged = copy.deepcopy(record)
            if change == 'target': forged['abs_id'] = 'other'
            if change == 'hash': forged['files']['overlay']['sha256'] = 'd' * 64
            if change == 'timestamp': forged['captured_at'] = '2026-10-31T02:00:00Z'
            if change == 'digest': forged['review_digest'] = 'e' * 64
            else: forged['review_digest'] = review.digest({k: v for k, v in forged.items() if k != 'review_digest'})
            with self.subTest(change=change), self.assertRaises(review.ReviewRefused): self.approve(forged)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assert_untouched()

    def test_browser_integral_number_roundtrip_keeps_review_digest(self):
        record = self.preview()
        def browser_numbers(value):
            if type(value) is float and value.is_integer(): return int(value)
            if isinstance(value, list): return [browser_numbers(v) for v in value]
            if isinstance(value, dict): return {k: browser_numbers(v) for k, v in value.items()}
            return value
        result = self.approve(browser_numbers(record))
        self.assertEqual(result['status'], 'approved')
        self.assert_untouched()

    def test_expired_or_restarted_worker_requires_refresh(self):
        record = self.preview(); self.clock += review.TTL + 1; self.gate()
        with self.assertRaisesRegex(review.ReviewRefused, 'expired'): self.approve(record)
        self.clock -= review.TTL + 1; self.gate()
        self.bridge._manual_reviews.clear()
        with self.assertRaisesRegex(review.ReviewRefused, 'not issued'): self.approve(record)
        self.assert_untouched()

    def test_changed_bytes_or_api_version_after_display_requires_new_review(self):
        record = self.preview(); self.hashes[19] = 'c' * 64
        with self.assertRaisesRegex(review.ReviewRefused, 'changed'): self.approve(record)
        self.hashes[19] = 'a' * 64
        self.bo.bd['files'][1]['contentVersion'] = '2026-10-31T02:30:00.000Z'
        with self.assertRaisesRegex(review.ReviewRefused, 'changed'): self.approve(record)
        self.assert_untouched()

    def test_pending_operation_or_coordinator_anywhere_blocks(self):
        record = self.preview()
        with self.bridge.db:
            self.bridge.db.execute('INSERT INTO operations VALUES (?,?,?,?)', ('uncertain', 'other', '{}', 'pending'))
        self.assertFalse(self.preview()['can_approve'])
        with self.assertRaises(review.ReviewRefused): self.approve(record)
        with self.bridge.db: self.bridge.db.execute('DELETE FROM operations')
        self.bridge.save('_jellyfin_audio:other', {'pending': {'goal': 'do not change'}})
        self.assertFalse(self.preview()['can_approve'])
        self.assert_untouched()

    def test_changed_baseline_or_accepted_proof_blocks_cas(self):
        record = self.preview()
        self.bridge.save('abs-book', dict(self.baseline, new='user activity'))
        with self.assertRaises(review.ReviewRefused): self.approve(record)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_failed_deployment_and_connection_change_cannot_be_approved(self):
        record = self.preview()
        (self.root / 'deployment.json').write_text(json.dumps({'ok': False, 'checked_at': self.clock}))
        self.assertFalse(self.preview()['can_approve'])
        with self.assertRaises(review.ReviewRefused): self.approve(record)
        self.gate(); self.bridge.config['bo_url'] = 'http://different-server'
        with self.assertRaisesRegex(review.ReviewRefused, 'connections'): self.approve(record)
        self.assert_untouched()

    def test_wrong_audio_lineage_unfinished_job_and_ambiguity_are_unapprovable(self):
        for change in ('audio', 'job', 'multiple'):
            with self.subTest(change=change):
                old = copy.deepcopy((self.bo.bm, self.job, self.cap))
                if change == 'audio': self.bo.bm['assets'][0]['etag'] = 'c' * 64
                if change == 'job': self.job['status'] = 'awaiting_review'
                if change == 'multiple': self.cap['existingReadAlongs'].append(dict(self.cap['existingReadAlongs'][0], fileId=21))
                record = self.preview()
                self.assertFalse(record['can_approve'])
                with self.assertRaises(review.ReviewRefused): self.approve(record)
                self.bo.bm, self.job, self.cap = old
        self.assert_untouched()

    def test_unknown_mapping_and_pause_remain_unapprovable(self):
        self.assertFalse(review.preview(self.bridge, 'unknown')['can_approve'])
        self.bridge.config['paused_mappings'] = {'abs-book': 'User pause'}
        self.assertFalse(self.preview()['can_approve'])
        self.assert_untouched()

    def test_exact_legacy_pin_is_superseded_only_by_explicit_user_review(self):
        self.bridge.config['native_readalong'] = {'abs-book': {'bo_id':23, 'overlay_file_id':19,
            'overlay_path':'/audiobooks/Example/CD 0/generated.readaloud.epub', 'overlay_size':4321}}
        original_config = copy.deepcopy(self.bridge.config)
        self.assertIn('abs-book', self.bridge.native_readalong())
        record = self.preview()
        self.assertTrue(record['can_approve'], record)
        self.assertEqual(record['legacy_pin'], original_config['native_readalong']['abs-book'])
        self.approve(record)
        self.assertEqual(self.bridge.native_readalong(), {})
        self.assertEqual(self.bridge.config, original_config)
        self.assertEqual(self.bridge._read_mapping(self.mapping)[0], self.mapping)
        self.bridge.config['native_readalong']['abs-book']['overlay_size'] += 1
        self.assertIn('abs-book', self.bridge.native_readalong(), 'A later config edit must not be silently superseded')
        self.assert_untouched()

    def test_legacy_import_without_official_job_stays_unapprovable(self):
        self.bridge.config['native_readalong'] = {'abs-book': {'bo_id':23, 'overlay_file_id':19,
            'overlay_path':'/audiobooks/Example/CD 0/generated.readaloud.epub', 'overlay_size':4321}}
        self.cap['existingReadAlongs'][0]['generatedByJobId'] = None
        self.assertFalse(self.preview()['can_approve'])
        self.assertIn('abs-book', self.bridge.native_readalong())
        self.assert_untouched()

    def test_malformed_accepted_proof_cannot_be_silently_repaired(self):
        with self.bridge.db:
            self.bridge.db.execute('INSERT INTO native_readalong_enrollments VALUES (?,?,?)', ('abs-book', 23, '{}'))
        self.assertFalse(self.preview()['can_approve'])
        self.assertEqual(self.bridge.db.execute('SELECT proof FROM native_readalong_enrollments').fetchone()[0], '{}')
        self.assert_untouched()

    def test_approval_does_not_override_unknown_baseline(self):
        self.baseline['baseline']['bo']['fingerprint'] = 'c' * 64
        self.bridge.save('abs-book', self.baseline)
        self.assertFalse(self.preview()['can_approve'])
        self.assert_untouched()

    def test_backup_failure_never_changes_approval_or_progress(self):
        record = self.preview()
        with patch('manual_review._backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.approve(record)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.count('events'), 0)
        self.assert_untouched()

    def test_pending_work_appearing_after_backup_is_rechecked_in_transaction(self):
        record = self.preview()
        original = review._backup
        def late_pending(bridge, shown):
            path = original(bridge, shown)
            with bridge.db:
                bridge.db.execute('INSERT INTO operations VALUES (?,?,?,?)', ('late', 'other', '{}', 'pending'))
            return path
        with patch('manual_review._backup', side_effect=late_pending):
            with self.assertRaisesRegex(review.ReviewRefused, 'state changed'): self.approve(record)
        self.assertEqual(self.count('native_readalong_enrollments'), 0)
        self.assertEqual(self.count('events'), 0)
        self.assertEqual(self.count('operations'), 1)
        self.assert_untouched()


if __name__ == '__main__': unittest.main()
