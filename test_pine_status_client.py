"""Own-version status markers only; all service calls are simulated."""
import copy
import json
import math
import unittest
from unittest.mock import Mock

from pinepods_client import PinepodsClient,RequestError,EpisodeUnavailable,action_epoch

FEED='https://feed.example/private?key=NEVER_LOG'
EPISODE='https://media.example/cut.mp3'
STAMP='2026-11-02T00:10:00'
DISABLED=dict(sync_type='gpodder',gpodder_enabled=True,external_enabled=False,external_url=None)

def response(value):
    r=Mock(status_code=200,content=json.dumps(value).encode());r.json.return_value=value;return r

def action(position=120,total=600,**changes):
    result=dict(podcast=FEED,episode=EPISODE,device='phone',timestamp=STAMP,
                action='play',started=0,position=position,total=total)
    result.update(changes);return result

def native(position=120,completed=False,duration=480):
    return dict(episode_id=7,user_id=2,duration=duration,position=position,completed=completed,
        downloaded=False,podcast_id=11,expected_duration=600,
        metadata=dict(episodeid=7,podcastid=11,feedurl=FEED,episodeurl=EPISODE,
            is_youtube=False,is_downloaded=False,completed=completed,
            episodeduration=duration,listenduration=position),account={'native_user_id':2})

class StatusTests(unittest.TestCase):
    def setUp(self):
        self.c=PinepodsClient('https://pinepods.example','reader','PRIVATE_PASSWORD',api_key='PRIVATE_KEY',server_version='0.9.0')
        self.addCleanup(self.c.close)
        self.c.inspect_episode_metadata=Mock(return_value=native())
        self.c.actions=Mock(return_value={'actions':[action()], 'timestamp':action_epoch(STAMP)})
        self.c._request=Mock()

    def read(self):
        return self.c.read_variant_status(FEED,EPISODE,600,expected_episode_id=7,expected_user_id=2)

    def snapshot(self,position=120,completed=False,gstatus='in_progress'):
        self.c.inspect_episode_metadata.return_value=native(position,completed)
        rows=[] if gstatus=='not_started' else [action(600 if gstatus=='finished' else 75)]
        self.c.actions.return_value={'actions':rows,'timestamp':action_epoch(STAMP)}
        return self.read()

    def plan(self,status,previous):
        return self.c.prepare_status_actions(FEED,EPISODE,status,600,STAMP,previous=previous,
                        expected_episode_id=7,expected_user_id=2)

    def test_reader_keeps_own_seconds_despite_stale_rss_and_historical_cut_total(self):
        self.c.inspect_episode_metadata.return_value=native(700,True,480)
        self.c.actions.return_value={'actions':[action(700,700)],'timestamp':1}
        got=self.read()
        self.assertEqual(got['own_duration'],600);self.assertEqual(got['native_duration'],480)
        self.assertEqual(got['position'],700);self.assertEqual(got['status'],'finished')
        self.assertEqual(got['gpodder_raw_total'],700);self.assertEqual(got['gpodder_status'],'finished')
        self.assertNotIn('NEVER_LOG',json.dumps(got));self.assertNotIn('PRIVATE',json.dumps(got))
        self.c._request.assert_not_called()

    def test_observed_native_revision_excludes_only_virtual_expected_duration(self):
        row=native()
        def inspect(feed,enclosure,duration):return dict(copy.deepcopy(row),expected_duration=duration)
        self.c.inspect_episode_metadata.side_effect=inspect
        before=self.read()
        refreshed=self.c.read_variant_status(FEED,EPISODE,601,expected_episode_id=7,expected_user_id=2)
        self.assertNotEqual(before['native_revision'],refreshed['native_revision'])
        self.assertNotEqual(before['revision'],refreshed['revision'])
        self.assertEqual(before['native_observed_revision'],refreshed['native_observed_revision'])
        row['metadata']['listendate']='2026-11-02T10:00:00Z'
        changed=self.read()
        self.assertEqual(before['native_position'],changed['native_position'])
        self.assertNotEqual(before['native_observed_revision'],changed['native_observed_revision'])

    def test_wrong_person_episode_or_cached_media_remains_blocked(self):
        for key,value in [('episode_id',8),('user_id',3),('downloaded',True)]:
            self.c.inspect_episode_metadata.return_value=dict(native(),**{key:value})
            with self.subTest(key=key),self.assertRaises(RequestError):self.read()
        for ids in ((True,2),(7,1),(7,True)):
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                self.c.read_variant_status(FEED,EPISODE,600,expected_episode_id=ids[0],expected_user_id=ids[1])

    def test_same_second_conflicting_actions_block_even_when_status_matches(self):
        for rows in ([action(70),action(80)],[action(),action(device='other')],
                     [action(),dict(podcast=FEED,episode=EPISODE,action='new',timestamp=STAMP)]):
            self.c.actions.return_value={'actions':rows,'timestamp':1}
            with self.subTest(rows=rows),self.assertRaises(RequestError):self.read()

    def test_position_beyond_its_own_action_total_never_implies_completion(self):
        self.c.actions.return_value={'actions':[action(601,600)],'timestamp':1}
        with self.assertRaisesRegex(RequestError,'exceeds its own total'):self.read()

    def test_short_action_below_exact_end_does_not_imply_completion(self):
        for position in (0,1,39):
            self.c.actions.return_value={'actions':[action(position,40)],'timestamp':1}
            with self.subTest(position=position),self.assertRaisesRegex(RequestError,'too short'):self.read()
        self.c.actions.return_value={'actions':[action(40,40)],'timestamp':1}
        self.assertEqual(self.read()['gpodder_status'],'finished')

    def test_download_actions_do_not_hide_latest_listening_status(self):
        self.c.actions.return_value={'actions':[action(),dict(podcast=FEED,episode=EPISODE,action='download',timestamp='2026-11-02T00:11:00')],'timestamp':1}
        self.assertEqual(self.read()['gpodder_status'],'in_progress')

    def test_unheard_started_uses_only_one_second_of_own_cut(self):
        before=self.snapshot(None,False,'not_started');plan=self.plan('in_progress',before)
        self.assertEqual(len(plan),1);self.assertEqual(plan[0]['position'],1)
        self.assertEqual(plan[0]['total'],600);self.assertEqual(plan[0]['_expected_native_position'],1)
        self.assertEqual(before['native_position'],None)

    def test_started_retains_existing_local_intermediate(self):
        before=self.snapshot(137,False,'not_started');plan=self.plan('in_progress',before)
        self.assertEqual(len(plan),1);self.assertEqual(plan[0]['position'],137)
        # Native and gpodder can have different actual seconds; shared status
        # does not authorize aligning these positions.
        before=self.snapshot(137,False,'in_progress')
        self.assertEqual(before['gpodder_raw_position'],75)
        self.assertEqual(self.plan('in_progress',before),[])

    def test_reopen_resets_then_restores_own_intermediate_with_distinct_timestamps(self):
        before=self.snapshot(137,True,'finished');plan=self.plan('in_progress',before)
        self.assertEqual([p.get('_native_action',p['action']) for p in plan],['uncompleted','new','play'])
        self.assertEqual([p['_expected_native_position'] for p in plan],[0,0,137])
        self.assertEqual(plan[-1]['position'],137)
        self.assertEqual([action_epoch(p) for p in plan],[action_epoch(STAMP)+i for i in range(3)])

    def test_reopen_old_end_uses_marker_but_does_not_rewind_true_near_end_intermediate(self):
        before=self.snapshot(700,True,'finished');self.assertEqual(self.plan('in_progress',before)[-1]['position'],1)
        before=self.snapshot(575,False,'finished')
        with self.assertRaisesRegex(RequestError,'too near'):self.plan('in_progress',before)

    def test_completion_uses_own_end_then_bool_only_bulk_not_rss_end(self):
        before=self.snapshot(137,False,'in_progress');plan=self.plan('finished',before)
        self.assertEqual(len(plan),2);self.assertEqual(plan[0]['position'],600)
        self.assertEqual(plan[-1]['_native_action'],'completed_preserving_position')
        self.assertEqual(plan[-1]['_expected_native_position'],600)
        self.assertEqual(before['native_duration'],480)

    def test_already_finished_gpodder_allows_native_intermediate_to_be_preserved(self):
        before=self.snapshot(137,False,'finished');plan=self.plan('finished',before)
        self.assertEqual(len(plan),1);self.assertEqual(plan[0]['_expected_native_position'],137)
        self.assertEqual(plan[0]['_native_action'],'completed_preserving_position')
        self.assertEqual(self.plan('finished',self.snapshot(137,True,'finished')),[])

    def test_native_completion_edge_can_update_gpodder_with_own_end_only(self):
        before=self.snapshot(137,True,'in_progress');plan=self.plan('finished',before)
        self.assertEqual(before['status'],'finished');self.assertEqual(before['gpodder_status'],'in_progress')
        self.assertEqual(len(plan),1);self.assertNotIn('_native_action',plan[0])
        self.assertEqual(plan[0]['position'],600);self.assertEqual(plan[0]['total'],600)
        self.assertTrue(plan[0]['_expected_native_completed'])

    def test_missing_native_history_is_preserved_through_new_until_first_play(self):
        before=self.snapshot(None,False,'in_progress');plan=self.plan('not_started',before)
        self.assertEqual(len(plan),1);self.assertEqual(plan[0]['action'],'new')
        self.assertIsNone(plan[0]['_expected_native_position'])
        plan=self.plan('in_progress',self.snapshot(None,True,'finished'))
        self.assertIsNone(plan[0]['_expected_native_position']);self.assertIsNone(plan[1]['_expected_native_position'])
        self.assertEqual(plan[-1]['position'],1)

    def test_invalid_or_too_short_own_duration_never_gets_fabricated_marker(self):
        before=self.snapshot(0,False,'not_started')
        for duration in (float('nan'),0,172801):
            with self.subTest(duration=duration),self.assertRaises(ValueError):
                self.c.prepare_status_actions(FEED,EPISODE,'in_progress',duration,STAMP,previous=before,expected_episode_id=7,expected_user_id=2)
        before['own_duration']=40
        with self.assertRaisesRegex(RequestError,'no representable'):
            self.c.prepare_status_actions(FEED,EPISODE,'in_progress',40,STAMP,previous=before,expected_episode_id=7,expected_user_id=2)

    def test_writer_preread_detects_concurrent_position_change_before_any_post(self):
        before=self.snapshot(137,False,'finished');payload=self.plan('finished',before)[0]
        self.c.inspect_episode_metadata.return_value=native(138,False)
        with self.assertRaisesRegex(RequestError,'changed before'):self.c.write_status_action(payload,expected=before)
        self.c._request.assert_not_called()

    def test_bulk_writer_uses_single_own_id_and_never_individual_complete(self):
        before=self.snapshot(137,False,'finished');payload=self.plan('finished',before)[0]
        self.c._request.side_effect=[DISABLED,{'processed_count':1,'failed_count':None,'message':'ok'}]
        self.assertTrue(self.c.write_status_action(payload,expected=before)['accepted'])
        method,path=self.c._request.call_args.args
        self.assertEqual((method,path),('POST','/api/data/bulk_mark_episodes_completed'))
        self.assertEqual(self.c._request.call_args.kwargs['payload'],{'episode_ids':[7],'user_id':2,'is_youtube':False})

    def test_gpodder_writer_strips_private_journal_fields_and_uses_own_marker(self):
        before=self.snapshot(137,False,'in_progress');payload=self.plan('finished',before)[0]
        self.c._request.return_value=DISABLED;self.c._write_gpodder=Mock(return_value={'accepted':True,'timestamp':10})
        self.c.write_status_action(payload,expected=before)
        wire=self.c._write_gpodder.call_args.args[0]
        self.assertEqual(wire['position'],600);self.assertEqual(wire['total'],600)
        self.assertFalse(any(k.startswith('_') for k in wire))

    def test_bad_scope_foreign_duration_unknown_fields_and_repeated_receipt_are_rejected(self):
        before=self.snapshot(None,False,'not_started');payload=self.plan('in_progress',before)[0]
        for change in ({'_episode_id':True},{'_user_id':1},{'_variant_status':'unknown'},
                       {'total':999},{'position':600,'_expected_native_position':1},
                       {'_other':'secret'},{'_server_timestamp':action_epoch(STAMP)},
                       {'timestamp':'2026-11-02T01:10:00+01:00'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.c.write_status_action(dict(payload,**change),expected=before)
        self.c._request.assert_not_called()

    def test_native_confirmation_requires_raw_position_and_unchanged_gpodder_receipt(self):
        before=self.snapshot(137,False,'finished');payload=self.plan('finished',before)[0]
        after=dict(before,status='finished',native_completed=True)
        self.assertTrue(self.c.status_action_confirmed(after,payload,600,previous=before))
        for change in ({'native_position':480},{'gpodder_revision':'raced'},{'native_duration':481},{'native_user_id':3}):
            self.assertFalse(self.c.status_action_confirmed(dict(after,**change),payload,600,previous=before))

    def test_gpodder_echo_without_native_effect_or_wrong_server_time_is_not_confirmation(self):
        before=self.snapshot(137,False,'in_progress');payload=self.plan('finished',before)[0]
        payload['_server_timestamp']=action_epoch(STAMP)+5
        after=dict(before,native_position=600,position=600,gpodder_raw_position=600,gpodder_raw_total=600,
                   gpodder_status='finished',action='play',device=self.c.device_id,updated_at=action_epoch(STAMP)+5)
        self.assertTrue(self.c.status_action_confirmed(after,payload,600,previous=before))
        for change in ({'native_position':137},{'gpodder_raw_total':480},{'updated_at':action_epoch(STAMP)+6},
                       {'native_completed':True},{'device':'other'}):
            self.assertFalse(self.c.status_action_confirmed(dict(after,**change),payload,600,previous=before))

    def test_new_confirmation_preserves_native_position_while_resetting_gpodder_status(self):
        before=self.snapshot(137,False,'finished');payload=self.plan('in_progress',before)[0]
        self.assertEqual(payload['action'],'new');self.assertNotIn('_native_action',payload)
        after=dict(before,action='new',device=self.c.device_id,updated_at=action_epoch(STAMP),
                   gpodder_status='not_started',gpodder_raw_position=0,gpodder_raw_total=None)
        self.assertTrue(self.c.status_action_confirmed(after,payload,600,previous=before))
        self.assertFalse(self.c.status_action_confirmed(dict(after,native_position=0),payload,600,previous=before))

    def test_nullable_history_reset_can_be_confirmed_then_new_preserves_actual_zero(self):
        before=self.snapshot(None,True,'finished');plan=self.plan('in_progress',before)
        reset=dict(before,native_completed=False,status='not_started',native_position=0,position=0)
        self.assertTrue(self.c.status_action_confirmed(reset,plan[0],600,previous=before))
        self.c.read_variant_status=Mock(return_value=reset)
        self.c._request.return_value=DISABLED;self.c._write_gpodder=Mock(return_value={'accepted':True,'timestamp':10})
        self.c.write_status_action(plan[1],expected=reset)
        after=dict(reset,action='new',device=self.c.device_id,updated_at=action_epoch(plan[1]),
                   gpodder_status='not_started',gpodder_raw_position=0,gpodder_raw_total=None)
        self.assertTrue(self.c.status_action_confirmed(after,plan[1],600,previous=reset))
        self.assertFalse(self.c.status_action_confirmed(dict(after,native_position=None),plan[1],600,previous=reset))


class WireReadTests(unittest.TestCase):
    def test_real_reader_only_uses_existing_gets_and_verified_metadata_read_post(self):
        c=PinepodsClient('https://pinepods.example','reader','PRIVATE',api_key='KEY',server_version='0.9.0')
        self.addCleanup(c.close)
        row=native(120,False)['metadata']
        c.session.request=Mock(side_effect=[response({'status':'success','retrieved_id':2}),
            response({'UserID':2,'Username':'reader'}),response({'pods':[{'podcastid':11,'feedurl':FEED}]}),
            response({'total':1,'episodes':[{'Episodeid':7,'Episodeurl':EPISODE}]}),response({'episode':row}),
            response({'timestamp':1,'actions':[action(75,480)]})])
        got=c.read_variant_status(FEED,EPISODE,600,expected_episode_id=7,expected_user_id=2)
        self.assertEqual(got['native_position'],120);self.assertEqual(got['gpodder_raw_position'],75)
        self.assertEqual(got['gpodder_raw_total'],480)
        posts=[call for call in c.session.request.call_args_list if call.args[0]=='POST']
        self.assertEqual(len(posts),1);self.assertTrue(posts[0].args[1].endswith('/api/data/get_episode_metadata'))
        for call in c.session.request.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects']);self.assertTrue(call.kwargs['verify'])
        c.session.request.side_effect=[response({'episode':row})]
        with self.assertRaises(EpisodeUnavailable):c._native_episode(FEED,EPISODE,600)


if __name__=='__main__':unittest.main()
