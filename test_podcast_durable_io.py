"""Durable observation evidence avoids media checks across age, growth and restarts."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from abs_client import RequestError
from media_observation import ObservationStore, MAX_RECORD_BYTES
from podcast_sync import build_mapping
from podcast_variant_proof import VariantProof, NAMESPACE
from test_pine_metadata_repair import Bridge
import test_podcast_positive_idle_io as fixtures


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.store=ObservationStore(self.root)

    def test_values_survive_new_store_without_any_media_path_operations(self):
        self.assertTrue(self.store.put('example','one',{'api':2},{'proof':'historical'}))
        with patch.object(Path,'resolve',side_effect=AssertionError('path resolution')), \
                patch.object(Path,'stat',side_effect=AssertionError('path stat')):
            self.assertEqual(ObservationStore(self.root).get('example','one',{'api':2}),{'proof':'historical'})
            self.assertIsNone(self.store.get('example','one',{'api':3}))
        self.assertEqual((self.root/'media-observations').stat().st_mode & 0o777,0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777==0o600 for p in (self.root/'media-observations').iterdir()))

    def test_checksum_corruption_and_changed_schema_are_misses(self):
        self.assertTrue(self.store.put('example','one','identity',{'kept':1}))
        p=next((self.root/'media-observations').glob('*.json'));original=p.read_bytes()
        bad=json.loads(original);bad['value']['kept']=2;p.write_text(json.dumps(bad))
        self.assertIsNone(self.store.get('example','one','identity'))
        p.write_bytes(b'{broken');self.assertIsNone(self.store.get('example','one','identity'))
        p.write_bytes(original);self.assertEqual(self.store.get('example','one','identity'),{'kept':1})

    def test_cache_directory_and_leaf_symlinks_do_not_read_or_overwrite_target(self):
        other=self.root/'elsewhere';other.mkdir(mode=0o700)
        (self.root/'media-observations').symlink_to(other,target_is_directory=True)
        self.assertFalse(self.store.put('example','one','identity',{'value':1}))
        self.assertIsNone(self.store.get('example','one','identity'));self.assertEqual(list(other.iterdir()),[])
        (self.root/'media-observations').unlink()
        self.assertTrue(self.store.put('example','one','identity',{'value':1}))
        p=next((self.root/'media-observations').glob('*.json'));p.unlink()
        target=other/'keep';target.write_bytes(b'keep');p.symlink_to(target)
        self.assertIsNone(self.store.get('example','one','identity'))
        self.assertTrue(self.store.put('example','one','identity',{'value':2}))
        self.assertEqual(target.read_bytes(),b'keep');self.assertFalse(p.is_symlink())

    def test_oversized_records_and_capacity_evict_only_generated_cache_leaves(self):
        self.assertFalse(self.store.put('example','too-big','identity',{'value':'x'*MAX_RECORD_BYTES}))
        bounded=ObservationStore(self.root,max_entries=2)
        self.assertTrue(bounded.put('example','one','identity',{'value':1}))
        folder=self.root/'media-observations';(folder/'unrelated.txt').write_text('keep')
        self.assertTrue(bounded.put('example','two','identity',{'value':2}))
        self.assertTrue(bounded.put('example','three','identity',{'value':3}))
        self.assertEqual(len(list(folder.glob('*.json'))),2)
        self.assertEqual((folder/'unrelated.txt').read_text(),'keep')
        self.assertEqual(bounded.get('example','three','identity'),{'value':3})

    def test_one_thousand_records_remain_independent_after_restart(self):
        for i in range(1000):
            self.assertTrue(self.store.put('podcast-source',str(i),{'id':i},{'source':i}))
        restarted=ObservationStore(self.root)
        with patch.object(restarted,'put',side_effect=AssertionError('unchanged cache rewrite')):
            for i in range(1000):self.assertEqual(restarted.get('podcast-source',str(i),{'id':i}),{'source':i})
        self.assertEqual(len(list((self.root/'media-observations').glob('*.json'))),1000)

    def test_folder_epoch_outlives_disposable_proof_eviction(self):
        bounded=ObservationStore(self.root,max_entries=2)
        epoch={'epoch':3,'nonce':'a'*32}
        self.assertTrue(bounded.put('folder-epoch','podcasts',{'schema':1},epoch))
        for i in range(4):self.assertTrue(bounded.put('example',str(i),'identity',{'value':i}))
        self.assertEqual(bounded.get('folder-epoch','podcasts',{'schema':1}),epoch)
        self.assertEqual(bounded.get('example','3','identity'),{'value':3})
        self.assertIsNone(bounded.get('example','0','identity'))


class DurablePodcastTests(unittest.TestCase):
    setUp=fixtures.PositiveIdleIOTests.setUp
    inspect=fixtures.PositiveIdleIOTests.inspect
    warm=fixtures.PositiveIdleIOTests.warm
    snapshot=fixtures.PositiveIdleIOTests.snapshot
    replace_preserving_reported_size_and_mtime=fixtures.PositiveIdleIOTests.replace_preserving_reported_size_and_mtime

    def restart_bridge(self):
        old=self.b;fresh=Bridge.__new__(Bridge)
        fresh.root=old.root;fresh.time=old.time;fresh.clock=lambda:fresh.time
        fresh.db=sqlite3.connect(old.root/'state.sqlite');self.addCleanup(fresh.db.close)
        fresh.config=copy.deepcopy(old.config);fresh.item=old.item;fresh.abs=old.abs
        fresh.pinepods=old.pinepods;fresh.external_write_guard=Mock()
        fresh.media_generation=lambda kind:self.generation
        self.b=fresh

    def test_restart_and_many_months_reuse_unchanged_source_without_media_checks(self):
        initial=self.warm();before=self.snapshot();self.restart_bridge();self.b.time+=400*86400
        with patch.object(VariantProof,'_source',side_effect=AssertionError('restart media check')):
            result=self.inspect()
        self.assertEqual(result,initial);self.assertFalse(result['fresh'])
        self.assertEqual(self.snapshot(),before);self.probe.assert_not_called()
        self.assertEqual(self.b._io_metrics['podcast_variant_source_disk_hits'],1)

    def test_new_unrelated_episode_checks_only_itself(self):
        self.warm();before=self.snapshot()
        target=Path(self.mapping['worker_path']).with_name('new-episode.mp3');target.write_bytes(b'b'*1000)
        episode=copy.deepcopy(self.b.item['media']['episodes'][0]);episode.update(id='new-episode',guid='guid-two')
        episode['enclosure']['url']='https://media.example/new-episode.mp3'
        episode['audioFile']['metadata'].update(path='/podcasts/show/new-episode.mp3',mtimeMs=target.stat().st_mtime_ns/1_000_000)
        self.b.item['media']['episodes'].append(episode)
        new=build_mapping(self.b.item,episode,self.b.config['podcasts']);new['jf']=dict(id='new-jf',size=1000,duration=600.25)
        checker=VariantProof(self.b,self.probe);actual=checker._inspect_source
        with patch.object(checker,'_inspect_source',wraps=actual) as checks:
            checker.source_snapshot(new,allow_cached=True,identity={'native':'new'})
            self.assertEqual(checks.call_count,1)
        with patch.object(VariantProof,'_source',side_effect=AssertionError('unrelated episode recheck')):
            self.assertTrue(self.inspect()['ready'])
        self.assertEqual(self.snapshot(),before)

    def test_unrelated_folder_or_policy_changes_preserve_episode_checkpoint(self):
        self.warm();self.restart_bridge()
        self.b.config['folder_mappings']=[dict(kind='ebooks',id='new-unrelated-folder')]
        self.b.config['podcasts']['primary_source']='pinepods'
        with patch.object(VariantProof,'_source',side_effect=AssertionError('unrelated config recheck')):
            self.assertTrue(self.inspect()['ready'])
        self.assertEqual(self.b._io_metrics['podcast_variant_source_disk_hits'],1)

    def test_transient_api_outage_does_not_destroy_checkpoint(self):
        self.warm();self.restart_bridge()
        original=self.b.abs.item.side_effect
        self.b.abs.item.side_effect=RequestError('GET /api/items/{item_id}: transport failure')
        with self.assertRaises(RequestError):self.inspect()
        self.b.abs.item.side_effect=original
        with patch.object(VariantProof,'_source',side_effect=AssertionError('outage caused recheck')):
            self.assertTrue(self.inspect()['ready'])
        self.assertEqual(self.b._io_metrics['podcast_variant_source_disk_hits'],1)

    def test_corrupt_checkpoint_rechecks_only_this_episode(self):
        self.warm();self.restart_bridge()
        directory=self.b.root/'media-observations'
        for p in directory.glob('*.json'):p.write_bytes(b'corrupt')
        actual=VariantProof(self.b)._source
        with patch.object(VariantProof,'_source',side_effect=actual) as checked:
            self.assertTrue(self.inspect()['ready'])
        checked.assert_called_once()

    def test_replacement_after_restart_requires_explicit_physical_check_to_detect(self):
        self.warm();self.restart_bridge();self.replace_preserving_reported_size_and_mtime()
        self.assertTrue(self.inspect()['ready'])
        self.assertFalse(self.inspect(force_source=True)['ready'])
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.assertFalse(self.b.pinepods.posts);self.probe.assert_not_called()
