"""Offline scheduling regressions; no services or network access."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from pine_metadata_repair import PineMetadataRepair,NAMESPACE,metadata_probe_order
from test_pine_metadata_repair import Bridge,URL
import test_podcast_sync as podcast_harness
import test_pine_metadata_repair as repair_harness
from podcast_sync import PodcastCoordinator


class OrderingTests(unittest.TestCase):
    def test_intents_then_unseen_then_oldest_check_with_stable_ties(self):
        names=['recent','unseen-a','old','intent','unseen-b','same-age']
        mappings=[{'id':name} for name in names]
        records={'recent':{'stage':'deferred','updated_at':200},'old':{'stage':'verified','updated_at':100},
                 'intent':{'stage':'intent','updated_at':300},'same-age':{'stage':'deferred','updated_at':100}}
        bridge=SimpleNamespace(load=lambda key:records.get(key.removeprefix(NAMESPACE),{}))
        before=copy.deepcopy((mappings,records));ordered=metadata_probe_order(bridge,mappings)
        self.assertEqual([m['id'] for m in ordered],['intent','unseen-a','unseen-b','old','same-age','recent'])
        self.assertEqual((mappings,records),before)
        self.assertTrue(all(any(item is original for original in mappings) for item in ordered))

    def test_unusable_journal_values_are_not_treated_as_authorization_or_global_failure(self):
        values={'invalid-json':ValueError('private detail'),'not-dict':[],
                'boolean-time':{'updated_at':True},'nan':{'updated_at':float('nan')},
                'negative':{'updated_at':-1},'valid':{'updated_at':100}}
        def load(key):
            value=values[key.removeprefix(NAMESPACE)]
            if isinstance(value,Exception):raise value
            return value
        mappings=[{'id':name} for name in values]
        self.assertEqual(metadata_probe_order(SimpleNamespace(load=load),mappings),mappings)

    def test_no_upfront_reservation_excludes_healthy_unknown_mappings(self):
        mappings=[{'id':'healthy'+str(i)} for i in range(150)]+[{'id':'needs-probe'}]
        bridge=SimpleNamespace(load=lambda key:{})
        self.assertEqual(metadata_probe_order(bridge,mappings),mappings)
        self.assertEqual(len(metadata_probe_order(bridge,mappings)),151)

    def test_prioritized_pending_intent_recovers_with_zero_budget_without_retry(self):
        env=repair_harness.RepairTests();env.setUp();self.addCleanup(env.doCleanups)
        def lose_response(_value):raise TimeoutError('private details')
        env.b.pinepods.after_write=lose_response
        self.assertEqual(env.run_repair()['outcome'],'waiting')
        self.assertEqual(env.b.load(env.key)['stage'],'intent')
        env.manager.remaining=0
        ordered=metadata_probe_order(env.b,[{'id':'never-checked'},env.mapping])
        self.assertIs(ordered[0],env.mapping)
        self.assertEqual(env.run_repair()['reason'],'interrupted_repair_verified')
        self.assertEqual(env.manager.remaining,0)
        self.assertEqual(env.b.pinepods.posts,[600]);self.assertEqual(env.probe.call_count,1)

    def test_coordinator_orders_only_productive_pine_cycles(self):
        env=podcast_harness.Tests();env.setUp();self.addCleanup(env.doCleanups)
        with patch('pine_metadata_repair.metadata_probe_order',side_effect=lambda bridge,rows:rows) as order:
            self.assertTrue(PodcastCoordinator(env.b).cycle(apply=True,settle=0)['ok']);order.assert_not_called()
            env.add_pine()
            self.assertTrue(PodcastCoordinator(env.b).cycle(apply=False)['ok']);order.assert_not_called()
            self.assertTrue(PodcastCoordinator(env.b).cycle(apply=True,settle=0)['ok']);self.assertEqual(order.call_count,1)


class FairProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.bridge=Bridge(Path(self.temp.name).resolve());self.addCleanup(self.bridge.db.close)
        self.mappings=[{'id':'episode'+str(i),'feed_url':'https://feed.example','enclosure_url':URL,'duration':600.25} for i in range(101)]
        self.probe=Mock(return_value={'known':False,'reason':'probe_timeout'})

    def cycle(self,full=False,ordered=True):
        manager=PineMetadataRepair(self.bridge,probe=self.probe,allow_full_probe=full)
        rows=metadata_probe_order(self.bridge,self.mappings) if ordered else self.mappings
        before=self.probe.call_count
        with patch.object(manager,'_source',side_effect=lambda m:{'mapping':{'id':m['id'],'enclosure_url':URL}}), \
             patch('pine_metadata_repair.time.monotonic',return_value=100):
            for row in rows:manager.repair_if_needed(row,True)
        self.bridge.time+=60
        self.assertGreaterEqual(manager.remaining,0)
        return self.probe.call_count-before

    def distinct(self):
        return self.bridge.db.execute("SELECT count(*) FROM state WHERE substr(id,1,?)=?",(len(NAMESPACE),NAMESPACE)).fetchone()[0]

    def test_101_transport_failures_all_get_checked_despite_short_retry_cooldown(self):
        # The old fixed order reached only45 different candidates forever:
        # by minute15 those first45 were eligible again. All101 now get a turn.
        samples={}
        for cycle in range(60):
            self.assertLessEqual(self.cycle(),3)
            if cycle in (14,15,33,59):samples[cycle+1]=self.distinct()
        self.assertEqual(samples,{15:45,16:48,34:101,60:101})
        self.assertFalse(self.bridge.pinepods.posts)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM operations WHERE status='pending'").fetchone()[0],0)

    def test_one_full_probe_slot_does_not_starve_unseen_episodes_or_expand_budget(self):
        def probe(url,**kwargs):
            return {'known':False,'reason':'probe_timeout' if kwargs.get('full_probe') else 'mpeg_audio_without_frame_count_tag'}
        self.probe.side_effect=probe
        for _ in range(101):
            start=self.probe.call_count
            self.assertLessEqual(self.cycle(full=True),4) # three headers, one full body maximum
            calls=self.probe.call_args_list[start:]
            self.assertEqual(sum(call.kwargs.get('full_probe') is True for call in calls),1)
        self.assertEqual(self.distinct(),101);self.assertFalse(self.bridge.pinepods.posts)

    def test_retry_order_never_overrides_existing_backoff(self):
        self.cycle();first=NAMESPACE+self.mappings[0]['id'];before=self.bridge.load(first)
        self.assertEqual(before['next_probe_at'],1900)
        self.cycle()
        self.assertEqual(self.bridge.load(first),before)
        self.assertEqual(self.distinct(),6);self.assertEqual(self.probe.call_count,6)


if __name__=='__main__':unittest.main()
