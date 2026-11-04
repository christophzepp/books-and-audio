"""Explicit native-overlay exception; no EPUB reads or remote API calls."""
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from mapping import build_mapping
from test_sync import FakeABS, FakeBO, fixture


def native_fixture(audio_duration=100, overlay_duration=100):
    ai, bd, bm = fixture()
    ai['media']['duration'] = audio_duration
    ai['media']['tracks'][0]['duration'] = audio_duration
    bd['files'][0]['durationSeconds'] = audio_duration
    bm['assets'][0]['durationMs'] = round(audio_duration * 1000)
    bm['totalDurationMs'] = round(audio_duration * 1000)
    pin = {'bo_id': 23, 'overlay_file_id': 19,
           'overlay_path': '/audiobooks/Example/readalong.epub', 'overlay_size': 4321}
    bd['files'].append({'id': 19, 'absolutePath': pin['overlay_path'],
                        'sizeBytes': pin['overlay_size'], 'format': 'epub', 'role': 'ebook'})
    difference = abs(audio_duration - overlay_duration)
    bd['readAloudSync'] = {'mode': 'auto', 'state': 'enabled', 'unavailableReason': None,
                           'overlayFileId': 19, 'audioDurationSeconds': audio_duration,
                           'overlayDurationSeconds': overlay_duration,
                           'durationDifferenceSeconds': difference,
                           'durationDifferenceRatio': difference / overlay_duration,
                           'koreaderDownloadAvailable': True}
    return ai, bd, bm, pin


class NativeMappingTests(unittest.TestCase):
    def test_explicit_single_overlay_retains_exact_audio_mapping_and_fingerprint(self):
        original = build_mapping(*fixture())
        ai, bd, bm, pin = native_fixture()
        self.assertEqual(build_mapping(ai, bd, bm, native_readalong=pin), original)

    def test_default_remains_strict_without_explicit_pin(self):
        ai, bd, bm, _ = native_fixture()
        with self.assertRaises(ValueError):
            build_mapping(ai, bd, bm)

    def test_unknown_missing_and_malformed_pin_fields_fail(self):
        ai, bd, bm, pin = native_fixture()
        invalid = [[], {}, dict(pin, extra=True), dict(pin, bo_id=24),
                   dict(pin, overlay_file_id=True), dict(pin, overlay_size=0),
                   dict(pin, overlay_size='4321'), dict(pin, overlay_path='/audiobooks/Other/readalong.epub'),
                   dict(pin, overlay_path='/audiobooks/Example/../readalong.epub'),
                   dict(pin, overlay_path='/audiobooks/Example/readalong.pdf')]
        missing = dict(pin); del missing['overlay_file_id']; invalid.append(missing)
        for bad in invalid:
            with self.subTest(pin=bad), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=bad)

    def test_each_pinned_file_identity_is_checked(self):
        for field, value in (('id', 20), ('sizeBytes', 4322),
                             ('absolutePath', '/audiobooks/Example/different.epub'), ('format', 'pdf')):
            ai, bd, bm, pin = native_fixture()
            bd['files'][-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_pin_does_not_allow_missing_or_additional_ebooks(self):
        ai, bd, bm, pin = native_fixture()
        bd['files'].pop()
        with self.assertRaises(ValueError):
            build_mapping(ai, bd, bm, native_readalong=pin)
        for extension in ('epub', 'pdf', 'mobi'):
            ai, bd, bm, pin = native_fixture()
            bd['files'].append({'id': 20, 'absolutePath': '/audiobooks/Example/extra.' + extension,
                                'format': extension, 'sizeBytes': 123})
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_duplicate_overlay_path_or_id_cannot_hide_as_sidecar(self):
        for extra in ({'id': 19, 'absolutePath': '/audiobooks/Example/cover.jpg', 'format': 'jpg'},
                      {'id': 20, 'absolutePath': '/audiobooks/Example/readalong.epub', 'format': 'epub', 'sizeBytes': 4321}):
            ai, bd, bm, pin = native_fixture()
            bd['files'].append(extra)
            with self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_enabled_mode_selection_and_failure_reason_are_checked(self):
        for field, value in (('state', 'unavailable'), ('state', 'disabled'), ('mode', 'disabled'),
                             ('mode', None), ('overlayFileId', 20), ('overlayFileId', '19'),
                             ('unavailableReason', 'duration_mismatch')):
            ai, bd, bm, pin = native_fixture()
            bd['readAloudSync'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_positive_finite_audio_and_overlay_duration_required(self):
        for field in ('audioDurationSeconds', 'overlayDurationSeconds'):
            for value in (None, False, 0, -1, math.nan, math.inf, '100'):
                ai, bd, bm, pin = native_fixture()
                bd['readAloudSync'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    build_mapping(ai, bd, bm, native_readalong=pin)

    def test_duration_evidence_must_match_manifest_and_its_own_arithmetic(self):
        for field, value in (('audioDurationSeconds', 101), ('durationDifferenceSeconds', 1),
                             ('durationDifferenceRatio', 0.1), ('durationDifferenceRatio', math.nan),
                             ('durationDifferenceSeconds', -1)):
            ai, bd, bm, pin = native_fixture()
            bd['readAloudSync'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_duration_threshold_uses_overlay_denominator_and_accepts_equality(self):
        for audio, overlay in ((105, 100), (95, 100), (6300, 6000), (9700, 10000), (10300, 10000)):
            ai, bd, bm, pin = native_fixture(audio, overlay)
            build_mapping(ai, bd, bm, native_readalong=pin)
        for audio, overlay in ((105.001, 100), (94.999, 100), (6300.001, 6000), (10300.001, 10000)):
            ai, bd, bm, pin = native_fixture(audio, overlay)
            with self.subTest(audio=audio, overlay=overlay), self.assertRaises(ValueError):
                build_mapping(ai, bd, bm, native_readalong=pin)

    def test_pin_does_not_weaken_audio_identity_or_duration_checks(self):
        ai, bd, bm, pin = native_fixture()
        ai['media']['tracks'][0]['metadata']['size'] += 1
        with self.assertRaises(ValueError):
            build_mapping(ai, bd, bm, native_readalong=pin)

    def test_inputs_are_not_mutated(self):
        ai, bd, bm, pin = native_fixture()
        before = copy.deepcopy((ai, bd, bm, pin))
        build_mapping(ai, bd, bm, native_readalong=pin)
        self.assertEqual((ai, bd, bm, pin), before)


class NativeSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mapping = build_mapping(*fixture())
        ai, bd, bm, self.pin = native_fixture()
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.id = self.mapping['abs_id']
        self.config = {'abs_url': 'http://abs', 'bo_url': 'http://bo', 'abs_version': '2.37.1',
                       'mappings': [self.mapping], 'native_readalong': {self.id: self.pin}}
        (self.root / 'deployment.json').write_text(json.dumps({'ok': True, 'checked_at': 1000}))
        with patch('sync.ABSClient', return_value=self.abs), patch('sync.BOClient', return_value=self.bo):
            self.bridge = sync.Bridge(self.root, self.config,
                                      {'abs_token': 'dummy', 'bo_username': 'dummy', 'bo_password': 'dummy'},
                                      clock=lambda: 1000)
        self.addCleanup(self.bridge.db.close)

    def cycle(self, apply=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=apply, settle=0)

    def test_abs_progress_passes_existing_transaction_with_pinned_overlay(self):
        self.abs.set(25)
        result = self.cycle()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(self.bo.raw['positionMs'], 25000)
        self.assertEqual(self.bridge.load(self.id)['baseline']['abs']['fingerprint'], self.mapping['fingerprint'])
        writes = copy.deepcopy(self.bo.writes)
        self.assertEqual(self.cycle()['unchanged'], 1)
        self.assertEqual(self.bo.writes, writes)

    def test_native_bo_audio_progress_flows_back_to_abs(self):
        # Simulates BO's native readalong having already updated its audio row.
        self.bo.set(42)
        result = self.cycle()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(self.abs.raw['currentTime'], 42)
        self.assertFalse(self.bo.writes)

    def test_without_pin_original_enabled_readalong_stays_blocked(self):
        self.config.pop('native_readalong')
        self.abs.set(25)
        result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_changed_overlay_blocks_without_replacing_baseline_or_mapping(self):
        self.abs.set(10); self.bo.set(10)
        self.cycle()
        baseline = copy.deepcopy(self.bridge.load(self.id)['baseline'])
        self.abs.set(25)
        self.bo.bd['files'][-1]['sizeBytes'] += 1
        result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(self.bridge.load(self.id)['baseline'], baseline)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.list_mappings(), [self.mapping])

    def test_overlay_change_after_prepare_is_caught_by_direct_apply(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.bo.bd['readAloudSync']['overlayFileId'] = 20
        with self.assertRaises(ValueError):
            self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', None)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_overlay_change_between_steps_stops_remaining_writes(self):
        self.abs.set(25)

        def replace_overlay(label):
            self.bo.bd['files'][-1]['sizeBytes'] += 1

        self.bo.after_write = replace_overlay
        result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(self.bo.writes, [('position', 25000)])
        self.assertEqual(self.bridge.db.execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0], 1)
        self.assertNotIn('baseline', self.bridge.load(self.id))

    def test_unknown_or_bad_native_config_fails_before_api_reads(self):
        for invalid in (None, [], {None: self.pin}, {'unknown': self.pin}, {self.id: None},
                        {self.id: dict(self.pin, bo_id=24)}, {self.id: dict(self.pin, extra=True)}):
            self.config['native_readalong'] = invalid
            with self.subTest(config=invalid), patch.object(self.abs, 'version') as version:
                with self.assertRaises(ValueError):
                    self.cycle()
                version.assert_not_called()
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_direct_apply_also_rejects_invalid_unrelated_pin(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.config['native_readalong']['unknown'] = self.pin
        with patch.object(self.bo, 'library') as library:
            with self.assertRaises(ValueError):
                self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', None)
            library.assert_not_called()
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_dry_run_reads_validated_pair_without_remote_or_baseline_writes(self):
        self.abs.set(25)
        result = self.cycle(apply=False)
        self.assertEqual(len(result['proposed']), 1)
        self.assertEqual(self.bridge.load(self.id), {})
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_revision_only_reconciliation_still_requires_valid_overlay(self):
        self.abs.set(10); self.bo.set(10)
        self.cycle()
        self.bo.bm['revision'] = 'b' * 64
        result = self.cycle()
        self.assertEqual(result['revisions_updated'], 1, result)
        self.assertEqual(result['unchanged'], 1)
        self.assertFalse(self.abs.writes or self.bo.writes)


if __name__ == '__main__':
    unittest.main()
