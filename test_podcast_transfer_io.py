"""Progress/status transfers reuse confirmed media; current APIs remain strict.

The deliberate standby tradeoff is explicit: hidden byte replacements with
unchanged service metadata are discovered by a requested physical verification,
not by every status edge. This file checks both sides of that contract.
"""
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from podcast_status_sync import state_key, measurement_receipt, StatusCoordinator
from podcast_variant_proof import VariantProof, transfer_authorized, authorize_transfer
from podcast_sync import PodcastCoordinator
import test_podcast_positive_idle_io as positive
import test_podcast_durable_io as durable
import test_podcast_status_sync as status


class DurableTransferAuthorizationTests(unittest.TestCase):
    setUp=positive.PositiveIdleIOTests.setUp
    inspect=positive.PositiveIdleIOTests.inspect
    warm=positive.PositiveIdleIOTests.warm
    snapshot=positive.PositiveIdleIOTests.snapshot
    replace_preserving_reported_size_and_mtime=positive.PositiveIdleIOTests.replace_preserving_reported_size_and_mtime
    restart_bridge=durable.DurablePodcastTests.restart_bridge

    def no_media(self):
        from contextlib import ExitStack
        stack=ExitStack()
        for name in ('resolve','stat','open'):
            stack.enter_context(patch.object(Path,name,side_effect=AssertionError('media path operation')))
        stack.enter_context(patch.object(VariantProof,'_source',side_effect=AssertionError('physical source check')))
        stack.enter_context(patch.object(VariantProof,'_measure',side_effect=AssertionError('remote audio measurement')))
        return stack

    def test_restart_and_year_old_media_proof_authorize_status_edges_without_media_reads(self):
        original=self.warm();self.restart_bridge();self.b.time+=400*86400
        before=self.snapshot();self.b.media_observation_guard=Mock()
        reads=self.b.pinepods.reads
        with self.no_media():
            for position,completed in ((0,False),(200,False),(600,True),(0,False)):
                self.b.pinepods.value.update(position=position,completed=completed)
                self.b.pinepods.value['metadata'].update(completed=completed,listenduration=position)
                result=self.inspect(authorize=True)
                self.assertTrue(result['ready']);self.assertFalse(result['fresh'])
                self.assertEqual(measurement_receipt(result),measurement_receipt(original))
                self.assertTrue(transfer_authorized(result,self.b.time))
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.b.pinepods.reads-reads,4)
        self.assertEqual(self.b.media_observation_guard.call_args_list,[(("podcasts",),)]*4)
        self.assertEqual(self.b._io_metrics['podcast_variant_source_disk_hits'],1)
        self.probe.assert_not_called()

    def test_real_coordinator_forwards_live_authorization_without_physical_refresh(self):
        original=self.warm();self.b.time+=400*86400
        coordinator=PodcastCoordinator.__new__(PodcastCoordinator)
        coordinator.b=self.b;coordinator.pine=self.b.pinepods
        coordinator.accounts={'pinepods':copy.deepcopy(self.account)}
        coordinator.observation=None;coordinator.variant_checker=VariantProof(self.b,self.probe)
        with self.no_media():
            result=coordinator.variant_proof(self.mapping,authorize=True)
        self.assertTrue(transfer_authorized(result,self.b.clock()))
        self.assertEqual(measurement_receipt(result),measurement_receipt(original))
        self.assertFalse(result['fresh']);self.probe.assert_not_called()

    def test_real_pending_coordinator_has_no_refresh_fallback_when_cache_is_missing(self):
        self.warm();key=state_key(self.mapping['id'])
        self.b.save(key,dict(pending={'keep':'existing-journal'}))
        for path in (self.b.root/'media-observations').glob('*.json'):path.unlink()
        self.b._podcast_source_cache={}
        before=self.snapshot()
        coordinator=PodcastCoordinator.__new__(PodcastCoordinator)
        coordinator.b=self.b;coordinator.pine=self.b.pinepods
        coordinator.cfg={'pinepods_variant_status_only':True}
        coordinator.observation=None;coordinator.variant_checker=VariantProof(self.b,self.probe)
        coordinator.read=Mock(return_value=({'abs':{},'jf':{}},{}))
        with patch.object(VariantProof,'_source',side_effect=AssertionError('journal physical fallback')) as source, \
                patch.object(VariantProof,'_measure',side_effect=AssertionError('journal audio fallback')) as probe:
            action,_=coordinator.handle(self.mapping,True,0)
        self.assertEqual(action,'waiting');source.assert_not_called();probe.assert_not_called()
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.b.load(key),{'pending':{'keep':'existing-journal'}})

    def test_real_core_journal_recovery_never_rebuilds_missing_media_proof_at_any_age(self):
        self.warm();core_key=self.mapping['id'];status_key=state_key(core_key)
        for path in (self.b.root/'media-observations').glob('*.json'):path.unlink()
        for storage in ('state','operation'):
            for days in (15,400):
                with self.subTest(storage=storage,days=days):
                    self.b.time=1000+days*86400
                    self.b._podcast_source_cache={}
                    self.b.db.execute('DELETE FROM operations');self.b.db.commit()
                    core={'pending':{'keep':'original-local-journal'}} if storage=='state' else {'baseline':{'keep':'local'}}
                    self.b.save(core_key,core)
                    self.b.save(status_key,{'binding':'retained-status-binding'})
                    if storage=='operation':
                        self.b.db.execute('INSERT INTO operations VALUES(?,?,?,?)',
                            ('existing-core-operation',core_key,'{"keep":"original-operation"}','pending'))
                        self.b.db.commit()
                    before=self.snapshot()
                    operations=self.b.db.execute('SELECT * FROM operations').fetchall()
                    coordinator=PodcastCoordinator.__new__(PodcastCoordinator)
                    coordinator.b=self.b;coordinator.pine=self.b.pinepods
                    coordinator.cfg={'pinepods_variant_status_only':True}
                    coordinator.observation=None;coordinator.variant_checker=VariantProof(self.b,self.probe)
                    coordinator.read=Mock(return_value=({'abs':{},'jf':{}},{}))
                    coordinator.handle_position=Mock(side_effect=AssertionError('unverified journal advancement'))
                    with patch.object(VariantProof,'_source',side_effect=AssertionError('core journal media fallback')) as source, \
                            patch.object(VariantProof,'_measure',side_effect=AssertionError('core journal audio fallback')) as probe:
                        action,_=coordinator.handle(copy.deepcopy(self.mapping),True,0)
                    self.assertEqual(action,'waiting');source.assert_not_called();probe.assert_not_called()
                    coordinator.handle_position.assert_not_called()
                    self.assertEqual(self.snapshot(),before)
                    self.assertEqual(self.b.db.execute('SELECT * FROM operations').fetchall(),operations)

    def test_api_authorization_expires_without_expiring_physical_evidence(self):
        original=self.warm()
        with self.no_media():
            first=self.inspect(authorize=True);self.b.time+=151
            self.assertFalse(transfer_authorized(first,self.b.time))
            second=self.inspect(authorize=True)
        self.assertTrue(transfer_authorized(second,self.b.time));self.assertFalse(second['fresh'])
        self.assertEqual(measurement_receipt(second),measurement_receipt(original))
        changed=copy.deepcopy(second);changed['own_duration']+=1
        self.assertFalse(transfer_authorized(changed,self.b.time))

    def test_missing_changed_or_corrupt_cache_stops_without_physical_fallback(self):
        self.warm()
        for change in ('missing','corrupt','generation'):
            with self.subTest(change=change):
                self.b._podcast_source_cache={}
                if change=='missing':
                    for path in (self.b.root/'media-observations').glob('*.json'):path.unlink()
                elif change=='corrupt':
                    # A normal observation may target-rebuild outside a write.
                    self.inspect()
                    for path in (self.b.root/'media-observations').glob('*.json'):path.write_bytes(b'corrupt')
                    self.b._podcast_source_cache={}
                else:
                    self.inspect();self.generation+='-changed'
                with self.no_media():result=self.inspect(authorize=True)
                self.assertFalse(result['ready']);self.assertEqual(result['reason'],'variant_media_verification_required')
        self.probe.assert_not_called()

    def test_changed_source_guid_account_and_native_identity_never_authorize_old_proof(self):
        self.warm()
        item=copy.deepcopy(self.b.item);native=copy.deepcopy(self.b.pinepods.value)
        account=copy.deepcopy(self.b.pinepods.identity);mapping=copy.deepcopy(self.mapping)
        changes=(lambda:self.b.item['media']['episodes'][0]['audioFile']['metadata'].update(size=1001),
                 lambda:self.b.item['media']['episodes'][0].update(guid='changed-guid'),
                 lambda:self.b.pinepods.identity.update(username='another-user'),
                 lambda:self.b.pinepods.value.update(episode_id=999))
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.b.item=copy.deepcopy(item);self.b.pinepods.value=copy.deepcopy(native)
                self.b.pinepods.identity=copy.deepcopy(account);self.mapping=copy.deepcopy(mapping)
                change()
                with self.no_media():result=self.inspect(authorize=True)
                self.assertFalse(result['ready'])
        self.probe.assert_not_called()

    def test_kernel_mount_guard_failure_retains_history_but_refuses_transfer(self):
        self.warm();before=self.snapshot()
        self.b.media_observation_guard=Mock(side_effect=ValueError('mount unavailable'))
        with self.no_media():self.assertFalse(self.inspect(authorize=True)['ready'])
        self.b.media_observation_guard=Mock()
        with self.no_media():self.assertTrue(self.inspect(authorize=True)['ready'])
        self.assertEqual(self.snapshot(),before)

    def test_pending_journal_uses_old_measurement_without_receipt_timestamp_renewal(self):
        original=self.warm();key=state_key(self.mapping['id'])
        self.b.save(key,dict(pending={'media_receipt':measurement_receipt(original),'keep':'journal'}))
        before=self.snapshot();self.b.time+=400*86400
        with self.no_media():result=self.inspect(authorize=True)
        self.assertTrue(result['ready']);self.assertFalse(result['fresh'])
        self.assertEqual(measurement_receipt(result),measurement_receipt(original))
        self.assertEqual(self.snapshot(),before)

    def test_hidden_replacement_waits_for_explicit_physical_verification(self):
        original=self.warm();self.replace_preserving_reported_size_and_mtime()
        with self.no_media():authorized=self.inspect(authorize=True)
        self.assertTrue(transfer_authorized(authorized,self.b.clock()))
        self.assertEqual(measurement_receipt(authorized),measurement_receipt(original))
        # Explicit refresh retains the strict byte/source identity boundary.
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.assertFalse(self.inspect(authorize=True)['ready'])
        self.probe.assert_not_called()

    def test_manual_and_reported_routes_use_cached_media_and_current_catalog(self):
        from episode_editor import route_proof
        from podcast_review import reported_proof
        self.warm()
        for kind in ('personal','reported'):
            fn=(lambda **kw:route_proof(self.c,self.mapping,self.route,**kw)) if kind=='personal' else (
                lambda **kw:reported_proof(self.c,self.mapping,**kw))
            with self.no_media():result=fn(refresh=False,authorize=True)
            self.assertTrue(transfer_authorized(result,self.b.clock()));self.assertFalse(result['fresh'])
        self.probe.assert_not_called()


class StatusTransferAuthorizationTests(unittest.TestCase):
    setUp=status.StatusTests.setUp
    write_gate=status.StatusTests.write_gate
    load=status.StatusTests.load
    save=status.StatusTests.save
    write_record=status.StatusTests.write_record
    setup_variant=status.StatusTests.setup_variant
    cycle=status.StatusTests.cycle
    okay=status.StatusTests.okay
    advance=status.StatusTests.advance
    settle_all=status.StatusTests.settle_all

    def test_aged_journal_finishes_with_current_api_checks_and_original_media_receipt(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.okay(self.cycle())
        key=state_key('_podcast:show:episode');saved=self.load(key)
        self.assertTrue(saved['pending']);original=copy.deepcopy(saved['pending']['media_receipt'])
        self.fixed_proof_time=original['validated_at'];self.proof['fresh']=False
        self.advance(400*86400)
        prior=PodcastCoordinator.variant_proof;calls=[]
        def current(c,mapping,**kwargs):
            self.assertFalse(kwargs.get('refresh'));self.assertFalse(kwargs.get('force_source'))
            calls.append(kwargs);return prior(c,mapping,**kwargs)
        with patch.object(PodcastCoordinator,'variant_proof',current):self.okay(self.cycle())
        self.assertTrue(any(row.get('authorize') for row in calls))
        self.assertFalse(self.load(key).get('pending'));self.assertTrue(self.pine.completed)
        operation=json.loads(self.db.execute('SELECT value FROM operations WHERE book=?',(key,)).fetchone()[0])
        self.assertEqual(operation['media_receipt'],original)

    def test_no_transfer_with_missing_current_api_authorization(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True)
        prior=PodcastCoordinator.variant_proof
        def unavailable(c,mapping,**kwargs):
            value=prior(c,mapping,**kwargs)
            if kwargs.get('authorize'):value.pop('transfer_authorization',None)
            return value
        with patch.object(PodcastCoordinator,'variant_proof',unavailable):result=self.cycle()
        self.assertEqual(result['waiting'],1);self.assertFalse(self.pine.writes)
        self.assertFalse(self.load(state_key('_podcast:show:episode')).get('pending'))


if __name__=='__main__':unittest.main()
