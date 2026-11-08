"""Idle podcast observations skip media stats; writes retain fresh source guards."""
import copy
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from episode_editor import route_proof
from podcast_review import reported_proof
from podcast_sync import PodcastCoordinator, build_mapping, fingerprint
from podcast_status_sync import StatusCoordinator, StatusConflict, state_key, version_receipt
from podcast_variant_proof import VariantProof, NAMESPACE, digest
from test_pine_metadata_repair import Bridge
from test_podcast_variant_proof import proof


class PositiveIdleIOTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.b=Bridge(Path(temp.name).resolve());self.addCleanup(self.b.db.close)
        self.mapping=build_mapping(self.b.item,self.b.item['media']['episodes'][0],self.b.config['podcasts'])
        self.mapping['jf']=dict(id='jf',size=1000,duration=600.25)
        self.account=dict(base_url='http://pinepods:8040',username='reader',device_id='book-progress-sync',
            protocol='gpodder-v2',server_version='0.9.0',native_user_id=2,completion_margin_seconds=60)
        self.b.pinepods.identity=copy.deepcopy(self.account)
        self.b.pinepods.value['account']=copy.deepcopy(self.account)
        self.b.pinepods.value['metadata']['episodetitle']=self.mapping['title']
        self.b.save('_podcast_accounts',dict(pinepods=self.account))
        self.generation='mount-generation-1'
        self.b.media_generation=lambda kind:self.generation
        self.monotonic=1000.
        clock=patch('podcast_variant_proof.time.monotonic',side_effect=lambda:self.monotonic)
        clock.start();self.addCleanup(clock.stop)
        self.probe=Mock(return_value=proof())
        source=VariantProof(self.b)._source(self.mapping)
        seed=proof(at=990)
        self.b.save('_pine_metadata:'+self.mapping['id'],dict(schema=1,stage='deferred',source=source,
            evidence=seed,evidence_sha256=digest(seed)))
        self.assertTrue(VariantProof(self.b,self.probe).inspect(self.mapping,refresh=True,apply=True)['fresh'])
        self.route=dict(feed_url=self.mapping['feed_url'],enclosure_url=self.mapping['enclosure_url'],
            native_episode_id=7,native_user_id=2,native_podcast_id=11)
        self.c=types.SimpleNamespace(b=self.b,pine=self.b.pinepods)

    def inspect(self,**kwargs):
        return VariantProof(self.b,self.probe).inspect(self.mapping,**kwargs)

    def warm(self):
        result=self.inspect();self.assertTrue(result['ready']);self.assertFalse(result['fresh'])
        self.probe.reset_mock();return result

    def snapshot(self):
        return self.b.db.execute('SELECT id,value FROM state ORDER BY id').fetchall()

    def test_one_hundred_new_helpers_observe_without_media_paths_or_durable_writes(self):
        initial=self.warm();before=self.snapshot();checked=copy.deepcopy(self.b._podcast_source_cache)
        hits=self.b._io_metrics.get('podcast_variant_source_cache_hits',0)
        with patch.object(Path,'resolve',side_effect=AssertionError('media resolve')), \
                patch.object(Path,'stat',side_effect=AssertionError('media stat')), \
                patch.object(Path,'open',side_effect=AssertionError('media open')):
            for i in range(100):
                self.b.time+=1;self.monotonic+=1
                result=self.inspect()
                self.assertEqual(result,initial)
        self.assertEqual(self.b._podcast_source_cache,checked)
        self.assertEqual(self.snapshot(),before);self.probe.assert_not_called()
        self.assertEqual(self.b._io_metrics['podcast_variant_source_cache_hits']-hits,100)

    def test_personal_and_reported_idle_proofs_reuse_source_but_are_never_fresh(self):
        for route in ('reported','personal'):
            with self.subTest(route=route):
                self.b._podcast_source_cache={}
                fn=lambda:reported_proof(self.c,self.mapping,refresh=False) if route=='reported' else route_proof(
                    self.c,self.mapping,self.route,refresh=False)
                self.assertFalse(fn()['fresh'])
                previous_reads=self.b.pinepods.reads
                with patch.object(Path,'resolve',side_effect=AssertionError('media resolve')), \
                        patch.object(Path,'stat',side_effect=AssertionError('media stat')):
                    for _ in range(20):self.assertFalse(fn()['fresh'])
                self.assertEqual(self.b.pinepods.reads-previous_reads,20)

    def test_historical_observations_do_not_expire_with_elapsed_time(self):
        self.warm()
        self.b.time+=90*86400;self.monotonic+=90*86400
        with patch.object(VariantProof,'_source',side_effect=AssertionError('age-driven media check')):
            result=self.inspect()
            self.assertTrue(result['ready']);self.assertFalse(result['fresh'])
        self.probe.assert_not_called()

    def test_clock_changes_do_not_make_a_historical_observation_fresh(self):
        self.warm();self.b.time-=1;self.monotonic-=1
        with patch.object(VariantProof,'_source',side_effect=AssertionError('clock-driven media check')):
            result=self.inspect()
            self.assertTrue(result['ready']);self.assertFalse(result['fresh'])
        self.probe.assert_not_called()

    def test_missing_or_failed_generation_disables_reuse(self):
        for failure in (None,'',ValueError('unavailable')):
            with self.subTest(failure=repr(failure)):
                self.warm()
                self.b.media_generation=Mock(side_effect=failure) if isinstance(failure,Exception) else Mock(return_value=failure)
                actual=VariantProof(self.b)._source
                with patch.object(VariantProof,'_source',side_effect=actual) as checked:
                    self.assertTrue(self.inspect()['ready']);self.assertTrue(self.inspect()['ready'])
                self.assertEqual(checked.call_count,2)
                self.b.media_generation=lambda kind:self.generation

    def test_generation_config_and_jellyfin_change_do_not_reuse_source(self):
        changes=(lambda:setattr(self,'generation','mount-generation-2'),
                 lambda:self.b.config.update(abs_url='http://replacement'),
                 lambda:self.mapping['jf'].update(id='replacement'))
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.warm();change()
                with patch.object(VariantProof,'_source',side_effect=ValueError('source changed')) as checked:
                    self.assertFalse(self.inspect()['ready'])
                checked.assert_called_once()

    def test_current_api_changed_without_new_mapping_is_refused_before_cached_use(self):
        self.warm();self.b.item['media']['episodes'][0]['audioFile']['metadata']['mtimeMs']+=10
        self.assertFalse(self.inspect()['ready'])
        self.probe.assert_not_called()

    def test_changed_guid_in_current_api_and_mapping_requires_new_binding(self):
        self.warm()
        self.b.item['media']['episodes'][0]['guid']='new-guid';self.mapping['abs_guid']='new-guid'
        with patch.object(VariantProof,'_source',side_effect=ValueError('guid changed')) as checked:
            self.assertFalse(self.inspect()['ready'])
        checked.assert_called_once()

    def test_native_change_cannot_use_a_cached_positive(self):
        self.warm();self.b.pinepods.value['episode_id']=8;self.b.pinepods.value['metadata']['episodeid']=8
        self.assertFalse(self.inspect()['ready']);self.probe.assert_not_called()

    def test_account_change_cannot_use_a_cached_positive(self):
        self.warm();self.b.pinepods.identity['username']='other'
        self.assertFalse(self.inspect()['ready']);self.probe.assert_not_called()

    def test_corrupt_or_expired_measurement_is_not_rescued_by_cached_source(self):
        self.warm();record=self.b.load(NAMESPACE+self.mapping['id']);record['proof_fingerprint']='corrupt'
        self.b.save(NAMESPACE+self.mapping['id'],record)
        self.assertFalse(self.inspect()['ready']);self.probe.assert_not_called()

    def replace_preserving_reported_size_and_mtime(self):
        target=Path(self.mapping['worker_path']);old=target.stat();replacement=target.with_suffix('.replacement')
        replacement.write_bytes(b'b'*old.st_size);os.utime(replacement,ns=(old.st_atime_ns,old.st_mtime_ns))
        replacement.replace(target)

    def test_hidden_replacement_may_wait_while_idle_but_is_refused_before_refresh_or_write(self):
        self.warm();self.replace_preserving_reported_size_and_mtime()
        self.assertTrue(self.inspect()['ready'])  # No API change; observation alone writes no progress.
        self.assertFalse(self.inspect(force_source=True)['ready'])
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.probe.assert_not_called();self.assertFalse(self.b.pinepods.posts)

    def test_failed_fresh_guard_evicts_old_positive_source(self):
        self.warm();self.replace_preserving_reported_size_and_mtime()
        self.assertTrue(self.inspect()['ready'])
        # The strict source still has the same API fields but a changed inode.
        # A failed refresh must not leave that old positive usable afterward.
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.assertFalse(self.inspect()['ready'])
        self.probe.assert_not_called()

    def test_source_that_changes_during_probe_cannot_reappear_as_idle_ready(self):
        self.warm()
        self.probe.side_effect=lambda *args,**kwargs:(self.replace_preserving_reported_size_and_mtime() or proof())
        result=self.inspect(refresh=True,apply=True)
        self.assertEqual(result['reason'],'variant_source_changed_during_probe')
        self.assertFalse(self.b._podcast_source_cache)
        self.assertFalse(self.inspect()['ready'])
        self.assertFalse(self.b.pinepods.posts)

    def test_candidate_classification_does_not_evict_native_idle_source(self):
        self.warm();self.assertTrue(VariantProof(self.b,self.probe).candidate(self.mapping))
        with patch.object(Path,'resolve',side_effect=AssertionError('media resolve')), \
                patch.object(Path,'stat',side_effect=AssertionError('media stat')):
            for _ in range(10):
                self.assertTrue(VariantProof(self.b,self.probe).candidate(self.mapping))
                self.assertTrue(self.inspect()['ready'])

    def test_manual_fresh_review_and_forced_pin_bypass_the_idle_cache(self):
        for mode in ('reported','personal'):
            with self.subTest(mode=mode):
                self.b._podcast_source_cache={}
                fn=lambda **kw:reported_proof(self.c,self.mapping,**kw) if mode=='reported' else route_proof(
                    self.c,self.mapping,self.route,**kw)
                self.assertFalse(fn(refresh=False)['fresh'])
                with patch.object(VariantProof,'_source',side_effect=ValueError('changed source')):
                    with self.assertRaises(ValueError):fn()
                    with self.assertRaises(ValueError):fn(refresh=False,force_source=True)


class ForcedSourceCallTests(unittest.TestCase):
    # Keep real coordinator/journal behavior and existing API fixtures; only
    # audio measurement is replaced by a deterministic proof in these tests.
    from test_podcast_status_sync import StatusTests as _fixtures
    setUp=_fixtures.setUp
    write_gate=_fixtures.write_gate
    load=_fixtures.load
    save=_fixtures.save
    write_record=_fixtures.write_record
    setup_variant=_fixtures.setup_variant
    cycle=_fixtures.cycle
    okay=_fixtures.okay
    advance=_fixtures.advance
    settle_all=_fixtures.settle_all

    def test_each_status_write_requests_an_uncached_local_pin(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True)
        original=PodcastCoordinator.variant_proof;calls=[]
        def watched(c,mapping,**kwargs):
            calls.append(dict(kwargs));return original(c,mapping,**kwargs)
        with patch.object(PodcastCoordinator,'variant_proof',watched):self.settle_all()
        pins=[call for call in calls if call.get('force_source') and call.get('apply') is False]
        writes=len(self.abs.writes)+len(self.jf.writes)+len(self.pine.writes)
        self.assertGreater(writes,0)
        row=self.db.execute('SELECT value FROM operations WHERE book=?',(state_key('_podcast:show:episode'),)).fetchone()
        self.assertIsNotNone(row);self.assertEqual(len(pins),len(json.loads(row[0])['plan']))
        self.assertTrue(all(call.get('refresh') is False for call in pins))

    def test_completed_plan_recovery_rechecks_source_without_replaying_writes(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True)
        original_persist=StatusCoordinator.persist;interrupted=[]
        def stop_before_final_commit(coordinator,key,stored,record,**kwargs):
            if kwargs.get('complete'):
                interrupted.append(True);raise RuntimeError('crash before final commit')
            return original_persist(coordinator,key,stored,record,**kwargs)
        with patch.object(StatusCoordinator,'persist',stop_before_final_commit):
            for _ in range(12):
                self.cycle()
                if interrupted:break
                self.advance()
        self.assertTrue(interrupted)
        key=state_key('_podcast:show:episode');pending=self.load(key)['pending']
        self.assertEqual(pending['next'],len(pending['plan']))
        writes=copy.deepcopy([self.abs.writes,self.jf.writes,self.pine.writes])
        original=PodcastCoordinator.variant_proof;calls=[]
        def watched(c,mapping,**kwargs):
            calls.append(dict(kwargs));return original(c,mapping,**kwargs)
        with patch.object(PodcastCoordinator,'variant_proof',watched):self.okay(self.cycle())
        self.assertTrue(calls[0]['force_source'])
        self.assertFalse(self.load(key).get('pending'))
        self.assertEqual([self.abs.writes,self.jf.writes,self.pine.writes],writes)


if __name__=='__main__':unittest.main()
