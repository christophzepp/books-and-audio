import copy
import json
import unittest
from unittest.mock import patch

import test_podcast_sync as harness
from pinepods_client import EpisodeUnavailable
from podcast_sync import PodcastCoordinator,preview,fingerprint,TICKS
import pine_identity_alias as alias

TARGET_FEED='https://example.org/feed'
TARGET_EP='https://example.org/episode.mp3?rss_browser=PINE-PRIVATE'
SOURCE_EP='https://example.org/episode.mp3?rss_browser=ABS-PRIVATE'


class AliasPine(harness.Pine):
    identity={'base_url':'http://pinepods','username':'reader','device_id':'sync','protocol':'gpodder-v2',
              'server_version':'0.9.0','native_user_id':2,'completion_margin_seconds':60}
    def __init__(self):
        super().__init__();self.source_actions=[];self.inspections=0;self.reads=[];self.prepared=[]
        self.native_id=7940;self.duration=1000;self.downloaded=False;self.unavailable=None
    def actions(self,feed):
        assert feed=='https://example.org/feed'
        return {'timestamp':2000000000,'actions':copy.deepcopy(self.source_actions)}
    def inspect_episode_metadata(self,feed,enclosure,duration):
        assert (feed,enclosure)==(TARGET_FEED,TARGET_EP);self.inspections+=1
        return {'episode_id':self.native_id,'user_id':2,'podcast_id':18,'duration':self.duration,
            'expected_duration':duration,'account':copy.deepcopy(self.identity),'downloaded':self.downloaded,
            'position':None,'completed':False,'metadata':{'episodeid':self.native_id,'podcastid':18,
                'feedurl':feed,'episodeurl':enclosure,'episodeduration':self.duration,'is_youtube':False,
                'is_downloaded':self.downloaded,'completed':False,'listenduration':None}}
    def read_episode(self,feed,enclosure,duration):
        self.reads.append((feed,enclosure))
        if (feed,enclosure)!=(TARGET_FEED,TARGET_EP):raise EpisodeUnavailable('episode_missing')
        if self.unavailable:raise EpisodeUnavailable(self.unavailable)
        return dict(super().read_episode(feed,enclosure,duration),native_episode_id=self.native_id,native_user_id=2)
    def prepare_actions(self,feed,enclosure,*args,**kwargs):
        assert (feed,enclosure)==(TARGET_FEED,TARGET_EP);self.prepared.append((feed,enclosure))
        return [dict(row,podcast=feed,episode=enclosure) for row in super().prepare_actions(feed,enclosure,*args,**kwargs)]


class AliasCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.e=harness.Tests();self.e.setUp();self.addCleanup(self.e.doCleanups)
        self.e.abs.data['media']['episodes'][0].update(guid='PRIVATE-GUID',enclosure={'url':SOURCE_EP})
        self.p=AliasPine();self.e.b.pinepods=self.p
        self.e.cfg['podcasts'].update(pinepods_enabled=True,pinepods_optional_enrollment=True)
        self.mapping=PodcastCoordinator(self.e.b).mappings()[0][0]
        source={k:self.mapping[k] for k in ('podcast_id','episode_id','abs_library','feed_url','enclosure_url','duration')}
        source.update(mapping_fingerprint=self.mapping['fingerprint'],jellyfin_fingerprint=self.mapping['jf']['fingerprint'],guid='PRIVATE-GUID')
        target={'user_id':2,'podcast_id':18,'episode_id':7940,'feed_url':TARGET_FEED,
                'enclosure_url':TARGET_EP,'duration':1000,'guid':'PRIVATE-GUID'}
        history=alias.source_history_evidence(source,[])
        evidence={'method':'personal_native_database_guid','observed_at':1900000000,
                  'source_report_sha256':'c'*64,'native_report_sha256':'d'*64,
                  'bindings_sha256':alias.binding_evidence_fingerprint(source,target,self.p.identity,history)}
        self.record={'schema':1,'kind':'pine_episode_alias','enabled':True,'revision':1,'mapping_id':self.mapping['id'],
            'source':source,'target':target,'account':copy.deepcopy(self.p.identity),'source_history':history,
            'evidence':evidence,'evidence_sha256':alias.fingerprint(evidence)}
        self.alias_key=alias.state_key(self.mapping['id']);self.e.save(self.alias_key,self.record)

    def cycle(self):return self.e.cycle()
    def okay(self,result):self.assertTrue(result['ok'],result)
    def no_writes(self):self.assertFalse(self.e.abs.writes or self.e.jf.writes or self.p.writes)

    def test_readonly_preview_resolves_alias_without_baseline_or_file_writes(self):
        self.e.abs.set(50)
        before=list(self.e.db.execute('SELECT id,value FROM state ORDER BY id'))
        result=preview(self.e.b);self.okay(result);self.assertEqual(result['proposed'],1)
        self.assertEqual(before,list(self.e.db.execute('SELECT id,value FROM state ORDER BY id')))
        self.no_writes();self.assertTrue(self.p.inspections)
        self.assertTrue(all(pair==(TARGET_FEED,TARGET_EP) for pair in self.p.reads))

    def test_positions_both_directions_and_steady_cycle_use_only_native_target_urls(self):
        self.e.abs.set(50);self.okay(self.cycle())
        self.assertEqual(self.p.state['position'],50)
        self.assertEqual(self.e.jf.row['UserData']['PlaybackPositionTicks'],50*TICKS)
        self.assertTrue(all(a['podcast']==TARGET_FEED and a['episode']==TARGET_EP for a in self.p.writes))
        self.assertEqual(self.e.abs.data['media']['episodes'][0]['enclosure']['url'],SOURCE_EP)
        self.p.state.update(position=120,status='in_progress',missing=False,revision='changed-by-native-player',
                            updated_at=self.e.clock+10,action='play',device='phone')
        self.e.clock+=11;self.e.write_gate();self.okay(self.cycle())
        self.assertEqual(self.e.abs.raw['currentTime'],120)
        self.assertEqual(self.e.jf.row['UserData']['PlaybackPositionTicks'],120*TICKS)
        counts=(len(self.p.writes),len(self.e.abs.writes),len(self.e.jf.writes))
        result=self.cycle();self.okay(result);self.assertEqual(result['unchanged'],1)
        self.assertEqual(counts,(len(self.p.writes),len(self.e.abs.writes),len(self.e.jf.writes)))
        stored=self.e.load(self.mapping['id'])
        self.assertEqual(stored['pine_alias_fingerprint'],alias.fingerprint(self.record))

    def test_completion_and_reopen_both_directions_preserve_alias_pin(self):
        self.e.abs.set(0,True);self.okay(self.cycle());self.assertEqual(self.p.state['status'],'finished')
        self.p.state.update(position=120,status='in_progress',missing=False,revision='phone-reopened',
                            updated_at=self.e.clock+2,action='play',device='phone')
        self.e.clock+=3;self.e.write_gate();self.okay(self.cycle())
        self.assertFalse(self.e.abs.raw['isFinished']);self.assertFalse(self.e.jf.row['UserData']['Played'])
        self.p.state.update(position=1000,status='finished',missing=False,revision='phone-finished',
                            updated_at=self.e.clock+2,action='play',device='phone')
        self.e.clock+=3;self.e.write_gate();self.okay(self.cycle())
        self.assertTrue(self.e.abs.raw['isFinished']);self.assertTrue(self.e.jf.row['UserData']['Played'])

    def test_old_source_history_new_action_blocks_before_inspection_or_writes(self):
        self.e.abs.set(50)
        self.p.source_actions=[{'podcast':self.mapping['feed_url'],'episode':SOURCE_EP,'action':'play',
                               'timestamp':'2026-11-01T12:00:00','position':90,'total':1000,'started':0}]
        result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(self.p.inspections,0)
        self.assertIn('original-URL history changed',result['blocked'][0]['reason']);self.no_writes()

    def test_unrelated_source_feed_actions_do_not_change_exact_old_pair_history(self):
        self.p.source_actions=[{'podcast':self.mapping['feed_url'],'episode':'https://other.example/episode',
                               'action':'play','timestamp':'2026-11-01T12:00:00','position':90,'total':1000,'started':0}]
        self.e.abs.set(50);self.okay(self.cycle())

    def test_invalid_disabled_or_missing_previously_used_alias_never_falls_back(self):
        self.okay(self.cycle())
        original=copy.deepcopy(self.record)
        for changed in ({},dict(original,enabled=False),dict(original,revision=2)):
            self.e.save(self.alias_key,changed)
            result=self.cycle();self.assertFalse(result['ok']);self.no_writes()
        self.e.db.execute('DELETE FROM state WHERE id=?',(self.alias_key,));self.e.db.commit()
        result=self.cycle();self.assertFalse(result['ok']);self.assertIn('alias is missing',result['blocked'][0]['reason'])

    def test_native_id_download_or_duration_change_does_not_trigger_metadata_repair(self):
        for field,value in [('native_id',7941),('downloaded',True),('duration',1001)]:
            old=getattr(self.p,field);setattr(self.p,field,value)
            with patch('pine_metadata_repair.PineMetadataRepair') as repair:
                result=self.cycle();self.assertFalse(result['ok']);repair.assert_not_called()
            self.no_writes();setattr(self.p,field,old)

    def test_race_failure_from_normal_client_cannot_drop_alias_or_repair_duration(self):
        self.p.unavailable='duration_mismatch';self.e.abs.set(50)
        with patch('pine_metadata_repair.PineMetadataRepair') as repair:
            result=self.cycle();self.assertFalse(result['ok']);repair.assert_not_called()
        self.no_writes();self.assertIn('normal media guard failed',result['blocked'][0]['reason'])

    def test_source_file_or_guid_change_blocks_without_fuzzy_matching(self):
        self.e.abs.data['media']['episodes'][0]['guid']='other'
        result=self.cycle();self.assertFalse(result['ok']);self.no_writes()

    def test_lost_write_response_recovery_keeps_exact_alias_and_never_retries(self):
        self.e.abs.set(50);self.p.fail=True
        result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(len(self.p.writes),1)

    def test_uncertain_success_cannot_confirm_tampered_journal_url_from_matching_progress(self):
        self.e.abs.set(50)
        def lost_ack():raise RuntimeError('Lost acknowledgement after write')
        self.p.after_write=lost_ack
        result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(self.p.state['position'],50)
        saved=self.e.load(self.mapping['id']);pending=saved['pending']
        self.assertTrue(pending['stage'].startswith('sending_pinepods_'))
        original_index=pending['pine_next']
        pending['pine_plan'][0]['episode']=SOURCE_EP
        self.e.save(self.mapping['id'],saved)
        self.e.write_record(pending['id'],self.mapping['id'],pending,'pending')
        self.p.after_write=None
        result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(len(self.p.writes),1)
        self.assertIn('alias action target changed',result['blocked'][0]['reason'])
        self.assertEqual(self.e.load(self.mapping['id'])['pending']['pine_next'],original_index)
        pending=self.e.load(self.mapping['id'])['pending']
        self.assertEqual(pending['pine_alias_fingerprint'],alias.fingerprint(self.record))
        self.p.fail=False;result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(len(self.p.writes),1)
        self.e.save(self.alias_key,dict(self.record,revision=2))
        result=self.cycle();self.assertFalse(result['ok']);self.assertEqual(len(self.p.writes),1)

    def test_alias_cannot_be_added_to_previously_enrolled_native_binding(self):
        self.e.save(self.mapping['id'],{'pine_enrolled':True,'baseline':{'pinepods':{'status':'not_started'}}})
        result=self.cycle();self.assertFalse(result['ok']);self.no_writes()
        self.assertIn('alias binding changed',result['blocked'][0]['reason'])

    def test_existing_core_baseline_can_admit_alias_after_unchanged_peer_check(self):
        self.e.cfg['podcasts']['pinepods_enabled']=False
        self.e.abs.set(50);self.okay(self.cycle())
        self.e.cfg['podcasts']['pinepods_enabled']=True
        self.okay(self.cycle());self.assertEqual(self.p.state['position'],50)


if __name__=='__main__':unittest.main()
