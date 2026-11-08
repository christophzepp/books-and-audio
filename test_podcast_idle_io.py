"""Negative retry deferrals never wake media or turn old proof into readiness."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pine_metadata_repair import PineMetadataRepair, NAMESPACE as METADATA
from podcast_variant_proof import VariantProof, NAMESPACE as VARIANT
from podcast_sync import build_mapping
from test_pine_metadata_repair import Bridge
from test_podcast_variant_proof import proof


class PodcastIdleIOTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.b = Bridge(Path(temp.name).resolve())
        self.addCleanup(self.b.db.close)
        self.mapping = build_mapping(self.b.item, self.b.item['media']['episodes'][0], self.b.config['podcasts'])
        self.mapping['jf'] = dict(id='jf', size=1000, duration=600.25)
        self.account = dict(base_url='http://pinepods:8040', username='reader', device_id='book-progress-sync',
                            protocol='gpodder-v2', server_version='0.9.0', native_user_id=2,
                            completion_margin_seconds=60)
        self.b.pinepods.identity = copy.deepcopy(self.account)
        self.b.pinepods.value['account'] = copy.deepcopy(self.account)
        self.b.save('_podcast_accounts', dict(pinepods=self.account))
        self.failed = Mock(return_value=dict(known=False, reason='probe_timeout'))

    def defer_metadata(self):
        manager = PineMetadataRepair(self.b, probe=self.failed)
        self.assertEqual(manager.repair_if_needed(self.mapping, True)['reason'], 'audio_header_unverified')
        self.failed.reset_mock()
        return METADATA + self.mapping['id']

    def defer_variant(self):
        source = PineMetadataRepair(self.b, probe=Mock())._source(self.mapping)
        evidence = proof(at=990)
        from podcast_variant_proof import digest
        self.b.save(METADATA + self.mapping['id'], dict(schema=1, stage='deferred', source=source,
                    evidence=evidence, evidence_sha256=digest(evidence)))
        self.assertTrue(VariantProof(self.b, Mock(return_value=proof())).inspect(
            self.mapping, refresh=True, apply=True)['fresh'])
        self.b.time += 5
        self.assertFalse(VariantProof(self.b, self.failed).inspect(
            self.mapping, refresh=True, apply=True)['ready'])
        self.failed.reset_mock()
        return VARIANT + self.mapping['id']

    def test_many_metadata_backoffs_after_new_instances_have_no_local_path_checks(self):
        key = self.defer_metadata()
        before = self.b.load(key)
        for _ in range(100):
            manager = PineMetadataRepair(self.b, probe=self.failed)
            with patch.object(manager, '_source', side_effect=AssertionError('local source check')) as source, \
                    patch.object(Path, 'resolve', side_effect=AssertionError('media resolve')) as resolve, \
                    patch.object(Path, 'stat', side_effect=AssertionError('media stat')) as stat:
                result = manager.repair_if_needed(self.mapping, True)
            self.assertEqual(result['reason'], 'metadata_probe_backoff')
            source.assert_not_called(); resolve.assert_not_called(); stat.assert_not_called()
            self.assertEqual(manager.remaining, 3)
        self.assertEqual(self.b.load(key), before)
        self.failed.assert_not_called(); self.assertEqual(self.b.pinepods.posts, [])

    def test_metadata_changed_jellyfin_binding_invalidates_negative_cache(self):
        self.defer_metadata()
        self.mapping['jf']['id'] = 'replacement'
        manager = PineMetadataRepair(self.b, probe=self.failed)
        with patch.object(manager, '_source', wraps=manager._source) as source:
            result = manager.repair_if_needed(self.mapping, True)
        source.assert_called_once()
        self.assertEqual(result['reason'], 'audio_header_unverified')
        self.failed.assert_called_once()

    def test_metadata_current_api_change_cannot_reuse_cooldown(self):
        key = self.defer_metadata()
        before = self.b.load(key)
        self.b.item['media']['episodes'][0]['audioFile']['metadata']['mtimeMs'] += 10
        result = PineMetadataRepair(self.b, probe=self.failed).repair_if_needed(self.mapping, True)
        self.assertEqual(result['reason'], 'metadata_repair_inspection_or_transport_failed')
        self.assertEqual(self.b.load(key), before)
        self.failed.assert_not_called()

    def test_due_metadata_probe_rechecks_actual_local_file_even_if_api_did_not_change(self):
        key = self.defer_metadata()
        self.b.time = self.b.load(key)['next_probe_at']
        Path(self.mapping['worker_path']).write_bytes(b'replaced')
        manager = PineMetadataRepair(self.b, probe=self.failed)
        with patch.object(manager, '_source', wraps=manager._source) as source:
            result = manager.repair_if_needed(self.mapping, True)
        source.assert_called_once()
        self.assertEqual(result['reason'], 'metadata_repair_inspection_or_transport_failed')
        self.failed.assert_not_called(); self.assertEqual(self.b.pinepods.posts, [])

    def test_many_variant_backoffs_never_read_local_path_or_renew_measurement(self):
        key = self.defer_variant()
        before = self.b.load(key)
        for _ in range(100):
            helper = VariantProof(self.b, self.failed)
            with patch.object(helper, '_source', side_effect=AssertionError('local source check')) as source, \
                    patch.object(Path, 'resolve', side_effect=AssertionError('media resolve')) as resolve, \
                    patch.object(Path, 'stat', side_effect=AssertionError('media stat')) as stat:
                result = helper.inspect(self.mapping, refresh=True, apply=True)
            self.assertEqual(result['reason'], 'variant_refresh_backoff')
            self.assertFalse(result['ready']); self.assertFalse(result['fresh'])
            source.assert_not_called(); resolve.assert_not_called(); stat.assert_not_called()
            self.assertEqual(helper.remaining, 2)
        self.assertEqual(self.b.load(key), before)
        self.failed.assert_not_called(); self.assertEqual(self.b.pinepods.posts, [])

    def test_variant_native_identity_change_cannot_reuse_negative_cache(self):
        self.defer_variant()
        self.b.pinepods.value['episode_id'] = 8
        self.b.pinepods.value['metadata']['episodeid'] = 8
        helper = VariantProof(self.b, self.failed)
        with patch.object(helper, '_source', wraps=helper._source) as source:
            result = helper.inspect(self.mapping, refresh=True, apply=True)
        source.assert_called_once()
        self.assertEqual(result['reason'], 'variant_native_identity_changed')
        self.assertFalse(result['ready']); self.failed.assert_not_called()

    def test_due_variant_probe_revalidates_file_before_external_measurement(self):
        key = self.defer_variant()
        self.b.time = self.b.load(key)['next_attempt_at']
        Path(self.mapping['worker_path']).write_bytes(b'replaced')
        helper = VariantProof(self.b, self.failed)
        with patch.object(helper, '_source', wraps=helper._source) as source:
            result = helper.inspect(self.mapping, refresh=True, apply=True)
        source.assert_called_once()
        self.assertFalse(result['ready']); self.assertFalse(result['fresh'])
        self.failed.assert_not_called(); self.assertEqual(self.b.pinepods.posts, [])

    def test_cached_positive_readiness_keeps_local_file_guard(self):
        self.defer_variant()
        Path(self.mapping['worker_path']).write_bytes(b'replaced')
        helper = VariantProof(self.b, self.failed)
        with patch.object(helper, '_source', wraps=helper._source) as source:
            result = helper.inspect(self.mapping, refresh=False, apply=False)
        source.assert_called_once()
        self.assertFalse(result['ready']); self.assertFalse(result['fresh'])
        self.failed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
