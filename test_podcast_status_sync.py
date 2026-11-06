import copy
import unittest
from unittest.mock import patch
from pinepods_client import PinepodsClient, EpisodeUnavailable
from podcast_sync import PodcastCoordinator, fingerprint, run, TICKS
from podcast_status_sync import state_key, history_ids
import test_podcast_sync as existing

class VariantPine(PinepodsClient):
    def __init__(self,clock):
        super().__init__('http://pinepods','reader','private-test-value',device_id='sync',api_key='private-test-key')
        self._native_user_id=7;self.clock=clock;self.position=None;self.completed=False
        self.gpos=None;self.gtotal=None;self.gstatus='not_started';self.stamp=None;self.action=None;self.device=None
        self.writes=[];self.lost=False;self.after_write=None
    def probe(self):return dict(device_exists=True,progress_sync_supported=True)
    def read_episode(self,*a):raise EpisodeUnavailable('duration_mismatch')
    def read_variant_status(self,feed,enclosure,duration,*,expected_episode_id,expected_user_id):
        assert (expected_episode_id,expected_user_id)==(9,7) and 1<=duration<=172800
        native=dict(position=self.position,completed=self.completed,expected_duration=duration,observed_meta=getattr(self,'observed_meta',0))
        g=dict(position=self.gpos,total=self.gtotal,status=self.gstatus,stamp=self.stamp,action=self.action,device=self.device)
        return dict(position=self.position or 0,status='finished' if self.completed else 'in_progress' if self.position else 'not_started',
            missing=self.position is None and not self.completed and self.stamp is None,own_duration=duration,native_duration=450,
            native_position=self.position,native_completed=self.completed,native_episode_id=9,native_user_id=7,
            native_revision=fingerprint(native),native_observed_revision=fingerprint({k:v for k,v in native.items() if k!='expected_duration'}),gpodder_status=self.gstatus,gpodder_missing=self.stamp is None,
            gpodder_raw_position=self.gpos,gpodder_raw_total=self.gtotal,gpodder_revision=fingerprint(g),
            updated_at=self.stamp,action=self.action,device=self.device,revision=fingerprint([native,g]))
    def write_status_action(self,payload,*,expected):
        fresh=self.read_variant_status(payload['podcast'],payload['episode'],payload['_own_duration'],expected_episode_id=9,expected_user_id=7)
        if fresh!=expected:raise RuntimeError('Concurrent edit')
        self._status_payload(payload);self.writes.append(copy.deepcopy(payload));kind=payload.get('_native_action')
        if kind=='uncompleted':self.position=0;self.completed=False
        elif kind=='completed_preserving_position':self.completed=True
        else:
            self.stamp=int(self.clock());self.action=payload['action'];self.device='sync'
            if self.action=='new':self.gpos=0;self.gtotal=None;self.gstatus='not_started'
            else:
                self.position=self.gpos=payload['position'];self.gtotal=payload['total']
                self.gstatus='finished' if self.gpos==self.gtotal else 'in_progress'
        if self.after_write:self.after_write()
        if self.lost:raise RuntimeError('Lost acknowledgement')
        return dict(accepted=True) if kind else {'timestamp':self.stamp}
    def set(self,position,status):
        self.position=position;self.completed=status=='finished';self.gpos=position;self.gtotal=600;self.gstatus=status
        self.stamp=int(self.clock());self.action='new' if status=='not_started' else 'play';self.device='player'

class StatusTests(unittest.TestCase):
    setUp=existing.Tests.setUp
    write_gate=existing.Tests.write_gate
    load=existing.Tests.load
    save=existing.Tests.save
    write_record=existing.Tests.write_record
    def setup_variant(self):
        self.cfg['podcasts'].update(pinepods_enabled=True,pinepods_optional_enrollment=True,pinepods_variant_status_only=True)
        self.pine=VariantPine(lambda:self.clock);self.b.pinepods=self.pine
        self.proof=dict(ready=True,fresh=True,validated_at=self.clock,own_duration=600,source_fingerprint='source',
            proof_fingerprint='proof',evidence_sha256='e'*64,feed_url='https://example.org/feed',enclosure_url='https://example.org/episode.mp3',
            native_episode_id=9,native_user_id=7)
        def proof(coordinator,mapping,refresh=False,apply=False):
            if refresh and getattr(self,'next_proof',None):
                self.proof.update(self.next_proof);self.next_proof=None
            value=copy.deepcopy(self.proof);value['validated_at']=getattr(self,'fixed_proof_time',self.clock)
            if refresh:value['fresh']=True
            if value['ready']:mapping['_variant_proof']=value
            return value
        self.addCleanup(patch.stopall)
        patch.object(PodcastCoordinator,'variant_proof',proof).start()
        patch.object(PodcastCoordinator,'repair_metadata',return_value=dict(outcome='waiting',reason='different_audio')).start()
        patch.object(PodcastCoordinator,'variant_candidate',return_value=False).start()
    def cycle(self,apply=True,settle=0):
        self.gate['checked_at']=self.clock;self.write_gate();return run(self.b,apply=apply,settle=settle)
    def okay(self,r):self.assertTrue(r['ok'],r)
    def advance(self,n=10):self.clock+=n
    def settle_all(self,limit=12):
        for _ in range(limit):
            r=self.cycle();self.okay(r)
            if not self.load(state_key('_podcast:show:episode')).get('pending'):return r
            self.advance()
        self.fail('journal did not finish')
    def test_empty_enrollment_preserves_all_positions(self):
        self.setup_variant();r=self.cycle();self.okay(r);self.assertEqual(r['pine_enrolled'],1)
        self.assertEqual(r['episodes'][0]['pine_mode'],'status_only');self.assertEqual(history_ids(self.b),{'_podcast:show:episode'})
        self.assertFalse(self.abs.writes or self.jf.writes or self.pine.writes);self.assertIsNone(self.pine.position)
    def test_abs_started_marker_not_cross_version_340(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(340);self.settle_all()
        self.assertEqual(self.pine.position,1);self.assertEqual(self.pine.gpos,1)
        self.assertEqual(self.abs.raw['currentTime'],340);self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],340*TICKS)
    def test_pine_started_marker_keeps_pine_250(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(250,'in_progress');self.settle_all()
        self.assertEqual(self.abs.raw['currentTime'],1);self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],TICKS)
        self.assertEqual(self.pine.position,250);before=(len(self.abs.writes),len(self.jf.writes),len(self.pine.writes))
        self.pine.set(310,'in_progress');self.okay(self.cycle());self.assertEqual(before,(len(self.abs.writes),len(self.jf.writes),len(self.pine.writes)))
    def test_finished_own_end_not_local1000_or_rss450(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(100,True);self.settle_all()
        self.assertEqual(self.pine.position,600);self.assertTrue(self.pine.completed)
        self.assertEqual(self.pine.gpos,600);self.assertEqual(self.pine.gtotal,600);self.assertEqual(self.abs.raw['currentTime'],100)
    def test_pine_completed_preserves_core_intermediate_and_reopen(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.advance();self.pine.set(80,'finished');self.settle_all()
        self.assertTrue(self.abs.raw['isFinished']);self.assertEqual(self.abs.raw['currentTime'],200)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],200*TICKS)
        self.advance();self.pine.set(100,'in_progress');self.settle_all();self.assertEqual(self.abs.raw['currentTime'],200)
        self.assertFalse(self.abs.raw['isFinished']);self.assertEqual(self.pine.position,100)
    def test_local_position_change_does_not_copy_to_variant(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(20,'in_progress');self.okay(self.cycle())
        self.abs.set(250);self.okay(self.cycle());self.assertEqual(self.pine.position,20);self.assertFalse(self.pine.writes)
    def test_initial_conflict(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(600,'finished');r=self.cycle()
        self.assertTrue(r['blocked']);self.assertFalse(self.pine.writes)
        self.assertEqual(r['episodes'][0]['pine_status_reason_code'],'variant_initial_status_conflict')

    def test_primary_pinepods_resolves_initial_variant_status_conflict(self):
        self.setup_variant();self.cfg['podcasts']['primary_source']='pinepods'
        self.abs.set(200);self.jf.set(200);self.pine.set(600,'finished');self.settle_all()
        self.assertTrue(self.abs.raw['isFinished'])
        self.assertEqual(self.abs.raw['currentTime'],200)
        self.assertTrue(self.jf.row['UserData']['Played'])
        self.assertEqual(self.pine.position,600)
    def test_concurrent_edges_conflict(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(20,'in_progress');self.okay(self.cycle())
        self.abs.set(1000,True);self.pine.set(0,'not_started');r=self.cycle();self.assertTrue(r['blocked']);self.assertFalse(self.pine.writes)
    def test_lost_pine_ack_readback_no_replay(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True
        self.assertTrue(self.cycle()['blocked']);count=len(self.pine.writes);self.pine.lost=False;self.advance();self.settle_all()
        self.assertEqual(len(self.pine.writes),count+1);self.assertTrue(self.pine.completed)
    def test_lost_abs_ack_readback_no_replay(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress');orig=self.abs._request
        def lost(method,*a,**kw):
            result=orig(method,*a,**kw)
            if method=='PATCH':raise RuntimeError('Lost ack')
            return result
        self.abs._request=lost;self.assertTrue(self.cycle()['blocked']);count=len(self.abs.writes)
        self.abs._request=orig;self.advance();self.settle_all();self.assertEqual(len(self.abs.writes),count)
    def test_failed_write_blocks_no_retry_or_core_writes(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(20,'in_progress');self.abs.fail=True
        self.assertTrue(self.cycle()['blocked']);n=len(self.abs.writes);self.abs.fail=False;self.advance();self.abs.set(55)
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.abs.writes),n);self.assertFalse(self.jf.writes)
    def test_preview_no_persist_or_write(self):
        self.setup_variant();self.abs.set(100);self.jf.set(100);before=list(self.db.execute('SELECT * FROM state'))
        r=self.cycle(apply=False);self.okay(r);self.assertEqual(r['proposed'],1)
        self.assertEqual(before,list(self.db.execute('SELECT * FROM state')));self.assertFalse(self.pine.writes or self.abs.writes or self.jf.writes)
    def test_disable_retains_history_and_core_continues(self):
        self.setup_variant();self.okay(self.cycle());saved=self.load(state_key('_podcast:show:episode'))
        self.cfg['podcasts']['pinepods_variant_status_only']=False;self.abs.set(70);self.okay(self.cycle())
        self.assertEqual(self.load(state_key('_podcast:show:episode')),saved);self.assertFalse(self.pine.writes)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],70*TICKS)
    def test_pending_disable_blocked(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True
        self.assertTrue(self.cycle()['blocked']);self.cfg['podcasts']['pinepods_variant_status_only']=False;self.assertTrue(self.cycle()['blocked'])
    def test_unknown_proof_waits(self):
        self.setup_variant();self.proof['ready']=False;r=self.cycle();self.okay(r);self.assertEqual(r['pine_waiting'],1);self.assertFalse(history_ids(self.b))
    def test_changed_source_proof_blocks(self):
        self.setup_variant();self.okay(self.cycle());self.proof['source_fingerprint']='changed';self.assertTrue(self.cycle()['blocked'])
    def test_rotation_pending_orphan_blocks(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True
        self.assertTrue(self.cycle()['blocked']);self.abs.data['media']['episodes']=[];self.assertTrue(self.cycle()['blocked'])
    def test_settle_active_wait(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress')
        self.assertEqual(self.cycle(settle=30)['waiting'],1);self.advance(31);self.jf.active={'a'*32}
        self.assertEqual(self.cycle(settle=30)['waiting'],1);self.assertFalse(self.abs.writes)
    def test_tampered_route_blocks_before_recovery(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True
        self.assertTrue(self.cycle()['blocked']);key=state_key('_podcast:show:episode');saved=self.load(key)
        saved['pending']['plan'][0]['value']['episode']='https://example.org/other.mp3';self.save(key,saved)
        self.pine.lost=False;self.advance();n=len(self.pine.writes);self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.pine.writes),n)

    def test_explicit_core_reset_resets_status_using_own_zero(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.abs.set(0);self.settle_all();self.assertEqual(self.pine.position,0);self.assertEqual(self.pine.gstatus,'not_started')
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],0)
    def test_explicit_pine_reset_resets_core_but_no_cross_seconds(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.advance();self.pine.set(0,'not_started');self.settle_all()
        self.assertEqual(self.abs.raw['currentTime'],0);self.assertEqual(self.jf.row['UserData']['PlayCount'],0)
    def test_native_history_disappearing_is_not_reset(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.pine.position=None;r=self.cycle();self.assertTrue(r['blocked']);self.assertFalse(self.abs.writes or self.pine.writes)
        self.assertEqual(r['episodes'][0]['pine_status_reason_code'],'variant_progress_missing')
    def test_abs_history_disappearing_is_not_reset(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.abs.raw=None;self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.pine.writes)
    def test_gpodder_history_disappearing_is_not_reset(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        self.pine.stamp=None;self.pine.gpos=None;self.pine.gstatus='not_started';r=self.cycle()
        self.assertTrue(r['blocked']);self.assertFalse(self.abs.writes or self.pine.writes)
    def test_lost_native_completion_ack_is_not_sent_again(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        # Native Pine is the only lagging participant; the API action is bulk completion.
        self.advance()
        self.abs.set(200,True);self.jf.set(200,True);self.pine.gstatus='finished';self.pine.gpos=600
        self.pine.lost=True;self.assertTrue(self.cycle()['blocked']);n=len(self.pine.writes)
        self.pine.lost=False;self.advance();self.settle_all();self.assertEqual(len(self.pine.writes),n)
    def test_concurrent_change_after_status_write_retains_journal(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200)
        self.pine.after_write=lambda:self.abs.set(250)
        self.assertTrue(self.cycle()['blocked']);self.assertTrue(self.load(state_key('_podcast:show:episode'))['pending'])
        n=len(self.pine.writes);self.pine.after_write=None;self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.pine.writes),n)
    def test_pending_core_and_status_never_progress_together(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True;self.assertTrue(self.cycle()['blocked'])
        saved=self.load('_podcast:show:episode');saved['pending']={'id':'other'};self.save('_podcast:show:episode',saved)
        n=len(self.pine.writes);self.pine.lost=False;self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.pine.writes),n)
    def test_changed_account_with_status_history_is_rejected(self):
        self.setup_variant();self.okay(self.cycle());self.pine.username='someone-else';r=self.cycle()
        self.assertFalse(r['ok']);self.assertFalse(self.pine.writes)
    def test_existing_full_enrollment_needs_explicit_migration(self):
        self.setup_variant();self.save('_podcast:show:episode',{'pine_enrolled':True})
        r=self.cycle();self.assertTrue(r['blocked']);self.assertFalse(history_ids(self.b))
        self.assertEqual(r['episodes'][0]['pine_status_reason_code'],'variant_full_enrollment_review')
    def test_status_pending_blocks_metadata_repair_before_probe(self):
        self.setup_variant();self.save(state_key('_podcast:show:episode'),{'pending':{'id':'unfinished'}})
        from pine_metadata_repair import PineMetadataRepair
        manager=PineMetadataRepair(self.b,probe=lambda *a,**k:self.fail('must not probe'))
        self.assertTrue(manager._pending_progress({'id':'_podcast:show:episode'}))
    def test_disabling_does_not_erase_account_binding(self):
        self.setup_variant();self.okay(self.cycle());old=self.load('_podcast_accounts')['pinepods']
        self.cfg['podcasts'].update(pinepods_enabled=False,pinepods_variant_status_only=False);self.okay(self.cycle())
        self.assertEqual(self.load('_podcast_accounts')['pinepods'],old)
        self.cfg['podcasts']['pinepods_enabled']=True;self.pine.username='another';self.assertFalse(self.cycle()['ok'])
    def test_journal_completes_atomically_with_core_baseline(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(80,'in_progress');self.settle_all()
        saved=self.load(state_key('_podcast:show:episode'));core=self.load('_podcast:show:episode')
        self.assertEqual({k:saved['baseline'][k] for k in ('abs','jf')},core['baseline'])
        self.assertFalse(self.db.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone())

    def test_invalid_abs_step_value_is_rejected_before_lost_ack_recovery(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress');orig=self.abs._request
        def lost(method,*a,**kw):
            result=orig(method,*a,**kw)
            if method=='PATCH':raise RuntimeError('Lost ack')
            return result
        self.abs._request=lost;self.assertTrue(self.cycle()['blocked']);self.abs._request=orig
        key=state_key('_podcast:show:episode');saved=self.load(key);original=copy.deepcopy(saved)
        from podcast_status_sync import plan_fingerprint
        for bad in (-1,False,float('nan')):
            value=copy.deepcopy(original);value['pending']['plan'][0]['value']=bad
            value['pending']['plan_fingerprint']=plan_fingerprint(value['pending']['plan'])
            self.save(key,value);self.assertTrue(self.cycle()['blocked'])
            self.assertEqual(self.load(key)['pending']['next'],0)
        self.assertFalse(self.jf.writes)
    def test_invalid_jf_step_kind_blocks_before_earlier_step_recovery(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress');orig=self.abs._request
        def lost(method,*a,**kw):
            result=orig(method,*a,**kw)
            if method=='PATCH':raise RuntimeError('Lost ack')
            return result
        self.abs._request=lost;self.assertTrue(self.cycle()['blocked']);self.abs._request=orig
        key=state_key('_podcast:show:episode');saved=self.load(key)
        from podcast_status_sync import plan_fingerprint
        saved['pending']['plan'][1]['kind']='delete';saved['pending']['plan_fingerprint']=plan_fingerprint(saved['pending']['plan'])
        self.save(key,saved);self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
    def test_proof_pin_rechecked_between_status_steps(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress')
        self.abs.after_write=lambda value:self.proof.update(source_fingerprint='different-file')
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
        self.assertTrue(self.load(state_key('_podcast:show:episode'))['pending'])

    def test_disable_flag_during_transfer_prevents_later_step(self):
        self.setup_variant();self.okay(self.cycle());self.advance();self.pine.set(200,'in_progress')
        self.abs.after_write=lambda value:self.cfg['podcasts'].update(pinepods_variant_status_only=False)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
        self.assertTrue(self.load(state_key('_podcast:show:episode'))['pending'])

    def test_new_duration_preserves_status_baseline_and_local_intermediate(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        before=self.load(state_key('_podcast:show:episode'));self.proof.update(fresh=False)
        self.next_proof={'own_duration':650,'proof_fingerprint':'new-measured-version'}
        self.abs.set(200,True);self.advance();self.settle_all()
        after=self.load(state_key('_podcast:show:episode'))
        self.assertEqual(after['binding'],before['binding']);self.assertEqual(after['version']['own_duration'],650)
        self.assertEqual(self.pine.position,650);self.assertTrue(self.pine.completed)
        self.assertEqual(self.abs.raw['currentTime'],200)
        self.assertEqual(self.pine.writes[0]['total'],650)
    def test_refresh_raw_change_never_disappears_behind_version_change(self):
        self.setup_variant();self.okay(self.cycle());self.proof['fresh']=False
        original=PodcastCoordinator.variant_proof
        def changed(c,mapping,refresh=False,apply=False):
            result=original(c,mapping,refresh=refresh,apply=apply)
            if refresh:self.pine.position=20
            return result
        self.next_proof={'own_duration':650,'proof_fingerprint':'new-version'};self.abs.set(200)
        with patch.object(PodcastCoordinator,'variant_proof',changed):r=self.cycle()
        self.assertTrue(r['blocked']);self.assertFalse(self.pine.writes)
    def test_pending_plan_never_changes_own_duration_or_replays(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.pine.lost=True
        self.assertTrue(self.cycle()['blocked']);key=state_key('_podcast:show:episode');old=self.load(key)['pending']
        self.proof.update(own_duration=650,proof_fingerprint='new-version');self.pine.lost=False;self.advance()
        n=len(self.pine.writes);self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.pine.writes),n)
        self.assertEqual(self.load(key)['pending'],old)
    def test_version_refresh_without_status_edge_does_not_write_positions(self):
        self.setup_variant();self.abs.set(200);self.jf.set(200);self.pine.set(80,'in_progress');self.okay(self.cycle())
        old=self.load(state_key('_podcast:show:episode'));self.proof.update(own_duration=650,proof_fingerprint='fresh-version')
        self.okay(self.cycle());new=self.load(state_key('_podcast:show:episode'))
        self.assertEqual(old['binding'],new['binding']);self.assertEqual(new['version']['own_duration'],650)
        self.assertEqual(self.pine.position,80);self.assertFalse(self.pine.writes or self.abs.writes or self.jf.writes)

    def test_actual_native_metadata_change_is_not_masked_by_duration_renewal(self):
        self.setup_variant();self.okay(self.cycle());self.proof['fresh']=False
        original=PodcastCoordinator.variant_proof
        def changed(c,mapping,refresh=False,apply=False):
            result=original(c,mapping,refresh=refresh,apply=apply)
            if refresh:self.pine.observed_meta=1
            return result
        self.next_proof={'own_duration':650,'proof_fingerprint':'new-version'};self.abs.set(200)
        with patch.object(PodcastCoordinator,'variant_proof',changed):r=self.cycle()
        self.assertTrue(r['blocked']);self.assertFalse(self.pine.writes)
    def test_renewal_requires_actual_native_receipt_not_just_virtual_hashes(self):
        from podcast_status_sync import same_user_progress
        left={'abs':None,'jf':{},'pinepods':{'own_duration':600,'native_revision':'old','revision':'old'}}
        right=copy.deepcopy(left);right['pinepods'].update(own_duration=650,native_revision='new',revision='new')
        self.assertFalse(same_user_progress(left,right))
        for value in (left,right):value['pinepods']['native_observed_revision']='a'*64
        self.assertTrue(same_user_progress(left,right))
        right['pinepods']['native_observed_revision']='b'*64;self.assertFalse(same_user_progress(left,right))

    def test_pending_reuses_original_measurement_across_sixty_second_cycle(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True)
        first=self.cycle();self.okay(first);key=state_key('_podcast:show:episode');saved=self.load(key)
        self.assertTrue(saved['pending']);original=copy.deepcopy(saved['pending']['media_receipt'])
        self.fixed_proof_time=original['validated_at'];self.proof['fresh']=False;self.advance(60)
        prior=PodcastCoordinator.variant_proof
        def no_refetch(c,mapping,refresh=False,apply=False):
            if refresh:self.fail('Unexpired original journal receipt must not trigger another audio fetch')
            return prior(c,mapping,refresh=refresh,apply=apply)
        with patch.object(PodcastCoordinator,'variant_proof',no_refetch):self.okay(self.cycle())
        self.assertTrue(self.pine.completed);self.assertFalse(self.load(key).get('pending'))
        import json
        operation=json.loads(self.db.execute('SELECT value FROM operations WHERE book=?',(key,)).fetchone()[0])
        self.assertEqual(operation['media_receipt'],original)
    def test_expired_receipt_cannot_adopt_new_version_or_extend_timestamp(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.okay(self.cycle())
        key=state_key('_podcast:show:episode');saved=self.load(key);original=copy.deepcopy(saved['pending'])
        self.fixed_proof_time=original['media_receipt']['validated_at'];self.proof['fresh']=False
        self.next_proof={'own_duration':650,'proof_fingerprint':'new-version'};self.advance(151)
        count=len(self.pine.writes);self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.pine.writes),count);self.assertEqual(self.load(key)['pending'],original)
    def test_changed_receipt_cannot_use_original_freshness_without_new_probe(self):
        self.setup_variant();self.okay(self.cycle());self.abs.set(200,True);self.okay(self.cycle())
        self.proof.update(fresh=False,evidence_sha256='f'*64);self.advance(60)
        prior=PodcastCoordinator.variant_proof;refreshes=[]
        def counted(c,mapping,refresh=False,apply=False):
            if refresh:refreshes.append(True)
            return prior(c,mapping,refresh=refresh,apply=apply)
        with patch.object(PodcastCoordinator,'variant_proof',counted):self.okay(self.cycle())
        self.assertEqual(len(refreshes),1)

    def test_proven_variant_precedes_matching_rss_full_read_and_metadata_repair(self):
        self.setup_variant()
        with patch.object(PodcastCoordinator,'variant_candidate',return_value=True), \
                patch.object(self.pine,'read_episode',side_effect=AssertionError('must not attempt full position admission')), \
                patch.object(PodcastCoordinator,'repair_metadata',side_effect=AssertionError('must not repair away variant classification')):
            result=self.cycle();self.okay(result)
        self.assertEqual(result['episodes'][0]['pine_mode'],'status_only')
        self.assertEqual(result['pine_enrolled'],1)
        self.assertFalse(self.load('_podcast:show:episode').get('pine_enrolled'))
    def test_proven_variant_without_fresh_proof_never_falls_back_to_full_sync(self):
        self.setup_variant();self.proof['ready']=False;self.abs.set(250)
        with patch.object(PodcastCoordinator,'variant_candidate',return_value=True), \
                patch.object(self.pine,'read_episode',side_effect=AssertionError('must remain waiting')), \
                patch.object(PodcastCoordinator,'repair_metadata',side_effect=AssertionError('must preserve variant classification')):
            result=self.cycle();self.okay(result)
        self.assertEqual(result['pine_waiting'],1);self.assertEqual(result['episodes'][0]['pine_mode'],'status_only')
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],250*TICKS)
        self.assertFalse(self.pine.writes);self.assertFalse(history_ids(self.b))
    def test_unproven_record_cannot_override_normal_position_guard(self):
        self.setup_variant()
        def same_audio(*args):
            value=self.pine.read_variant_status('https://example.org/feed','https://example.org/episode.mp3',1000,
                expected_episode_id=9,expected_user_id=7)
            return dict(value,gpodder_position=0)
        with patch.object(self.pine,'read_episode',side_effect=same_audio), \
                patch.object(PodcastCoordinator,'variant_proof',side_effect=AssertionError('unproven candidate is not admission')):
            result=self.cycle();self.okay(result)
        self.assertTrue(self.load('_podcast:show:episode')['pine_enrolled'])
        self.assertFalse(history_ids(self.b));self.assertEqual(result['episodes'][0]['pine_mode'],'position')
    def test_proven_candidate_never_migrates_an_existing_full_participant(self):
        self.setup_variant();self.save('_podcast:show:episode',{'pine_enrolled':True})
        with patch.object(PodcastCoordinator,'variant_candidate',side_effect=AssertionError('old participant must not reclassify')):
            result=self.cycle()
        self.assertTrue(result['blocked']);self.assertFalse(history_ids(self.b))

if __name__=='__main__':unittest.main()
