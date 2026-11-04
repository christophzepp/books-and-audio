import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from abs_client import ABSClient, RequestError
from pinepods_client import action_epoch, EpisodeUnavailable, RequestError as PineRequestError
from podcast_sync import (EpisodeABS, PodcastCoordinator, abs_state, bind, build_mapping,
                          fingerprint, guard, preview, run, TICKS)

STAMP=1793500000000


def fixture():
    return dict(id='show',libraryId='podcasts',mediaType='podcast',path='/podcasts/Show',media=dict(
        metadata=dict(title='Show',feedUrl='https://example.org/feed'),episodes=[dict(
            id='episode',libraryItemId='show',title='Episode',duration=1000,size=1000,
            enclosure={'url':'https://example.org/episode.mp3'},audioFile=dict(duration=1000,
                metadata=dict(path='/podcasts/Show/episode.mp3',size=1000,mtimeMs=STAMP)))]))


class ABS:
    _id=staticmethod(ABSClient._id)
    def __init__(self):
        self.data=fixture();self.raw=None;self.writes=[];self.after_write=None;self.fail=False
    def version(self):return '2.37.1'
    def libraries(self):return [dict(id='podcasts',mediaType='podcast')]
    def inventory(self,lib):return [dict(id='show')]
    def item(self,ident):return copy.deepcopy(self.data)
    def set(self,pos,finished=False):
        self.raw=dict(libraryItemId='show',episodeId='episode',mediaItemType='podcastEpisode',currentTime=pos,
            duration=1000,isFinished=finished,startedAt=STAMP,lastUpdate=STAMP,finishedAt=STAMP if finished else None)
    def _request(self,method,path,label,**kw):
        if path=='/api/me':return dict(username='reader',id='user')
        assert path=='/api/me/progress/show/episode',path
        if method=='GET':return copy.deepcopy(self.raw)
        assert method=='PATCH'; payload=kw['payload'];self.writes.append(copy.deepcopy(payload))
        if self.fail:raise RuntimeError('Lost ABS response')
        if self.raw is None:self.set(0)
        if 'isFinished' in payload:
            if not payload['isFinished'] and self.raw['isFinished']:
                self.raw['currentTime']=0;self.raw['finishedAt']=None
            self.raw['isFinished']=payload['isFinished']
            if payload['isFinished']:self.raw['finishedAt']=payload.get('finishedAt',STAMP)
        else:
            if self.raw['isFinished'] and self.raw['currentTime']!=payload['currentTime']:
                self.raw['isFinished']=False;self.raw['finishedAt']=None
            self.raw.update({k:payload[k] for k in ('currentTime','duration')})
        self.raw['lastUpdate']+=1
        if self.after_write:self.after_write(payload)


class JF:
    def __init__(self):
        self.row=dict(Id='a'*32,Type='AudioBook',Path='/data/podcasts/Show/episode.mp3',RunTimeTicks=1000*TICKS,
            MediaSources=[dict(Path='/data/podcasts/Show/episode.mp3',Size=1000)],
            UserData=dict(PlaybackPositionTicks=0,Played=False,PlayCount=0,LastPlayedDate=None))
        self.active=set();self.writes=[];self.fail=False;self.after_write=None;self.available=True
        self.policy=dict(EnableAllFolders=True,EnabledFolders=[])
    def guard(self):return {'Policy':copy.deepcopy(self.policy)}
    def items(self,**kw):return [copy.deepcopy(self.row)] if self.available else []
    def active_ids(self):return self.active
    def update(self,ident,values):
        assert ident==self.row['Id'];self.writes.append(copy.deepcopy(values))
        if self.fail:raise RuntimeError('Lost JF response')
        self.row['UserData'].update(copy.deepcopy(values))
        if self.after_write:self.after_write()
    def set(self,pos,finished=False):
        self.row['UserData'].update(PlaybackPositionTicks=round(pos*TICKS),Played=finished,PlayCount=1,
            LastPlayedDate='2026-11-01T12:00:00Z')


class Pine:
    def __init__(self):
        self.state=dict(position=0,status='not_started',missing=True,revision='missing',updated_at=None,device=None,action=None)
        self.writes=[];self.fail=False;self.after_write=None
    identity={'base_url':'http://pinepods','username':'reader','device_id':'sync','protocol':'gpodder-v2'}
    def probe(self):return {'ok':True,'device_exists':True,'progress_sync_supported':True}
    def read_log(self):
        return dict(copy.deepcopy(self.state),raw_position=self.state['position'] if not self.state['missing'] else None)
    def read_episode(self,feed,enclosure,duration):
        row=copy.deepcopy(self.state)
        return dict(row,gpodder_position=row['position'],gpodder_status=row['status'],gpodder_missing=row['missing'],
            gpodder_revision=row['revision'],gpodder_raw_position=row['position'] if not row['missing'] else None,
            native_position=row['position'] if not row['missing'] else None,native_completed=row['status']=='finished')
    @staticmethod
    def action_epoch(a):return a['timestamp']
    def prepare_actions(self,feed,enclosure,goal,duration,timestamp,previous):
        stamp=int(timestamp);out=[]
        if goal['status']=='not_started':return [dict(action='new',timestamp=stamp,device='sync')]
        if previous['status']=='finished' and goal['status']=='in_progress':
            out.append(dict(action='new',timestamp=stamp,device='sync'));stamp+=1
        out.append(dict(action='play',timestamp=stamp,device='sync',position=round(goal['position']),total=1000))
        return out
    def write_action(self,a):
        self.writes.append(copy.deepcopy(a))
        if self.fail:raise RuntimeError('Lost PinePods response')
        pos=a.get('position',0)
        self.state=dict(position=pos,status='not_started' if a['action']=='new' else 'finished' if pos==1000 else 'in_progress',
            missing=False,revision=fingerprint(a),updated_at=action_epoch(a),device=a['device'],action=a['action'])
        if self.after_write:self.after_write()


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.abs=ABS();self.jf=JF();self.clock=2000000000
        self.cfg=dict(abs_version='2.37.1',podcasts=dict(enabled=True,abs_library_id='podcasts',
            jellyfin_library_id='jf-podcasts',abs_root='/podcasts',jellyfin_root='/data/podcasts',worker_root='/podcasts',
            expected_abs_username='reader',bookorbit_enabled=False,pinepods_enabled=False))
        self.gate=dict(podcasts_ok=True,pinepods_ok=True,checked_at=self.clock,podcast_mounts={
            k:dict(source='/srv/example-media/podcasts',destination=v,rw=k=='abs')
            for k,v in [('abs','/podcasts'),('jf','/data/podcasts'),('worker','/podcasts')]})
        self.write_gate();self.db=sqlite3.connect(self.root/'state.sqlite');self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE state(id TEXT PRIMARY KEY,value TEXT);CREATE TABLE operations(id TEXT PRIMARY KEY,book TEXT,value TEXT,status TEXT)')
        self.events=[];self.alerts=[]
        self.b=SimpleNamespace(root=self.root,config=self.cfg,abs=self.abs,jellyfin=self.jf,db=self.db,clock=lambda:self.clock,
            load=self.load,save=self.save,write_record=self.write_record,event=lambda *a:self.events.append(a),
            alert=lambda *a:self.alerts.append(a),bo=SimpleNamespace())
    def write_gate(self):self.root.joinpath('deployment.json').write_text(json.dumps(self.gate))
    def load(self,key):
        row=self.db.execute('SELECT value FROM state WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}
    def save(self,key,value):self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)));self.db.commit()
    def write_record(self,ident,key,record,status='pending'):
        self.db.execute('INSERT OR REPLACE INTO operations VALUES(?,?,?,?)',(ident,key,json.dumps(record),status));self.db.commit()
    def cycle(self,**kw):return run(self.b,apply=True,settle=0,**kw)
    def okay(self,result):self.assertTrue(result['ok'],result)
    def add_pine(self):self.cfg['podcasts']['pinepods_enabled']=True;self.b.pinepods=Pine();return self.b.pinepods
    def test_preview_has_no_mutations_and_independent_of_bo(self):
        self.abs.set(35);r=preview(self.b)
        self.assertEqual(r['mapped'],1);self.assertEqual(r['proposed'],1)
        self.assertFalse(self.abs.writes or self.jf.writes or self.root.joinpath('last-podcast-run.json').exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM state').fetchone()[0],0)
        self.assertTrue(r['bookorbit_prepared']);self.assertFalse(r['bookorbit_enabled'])
    def test_abs_position_to_jf_then_reverse(self):
        self.abs.set(25);self.okay(self.cycle());self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],25*TICKS)
        self.jf.set(70);self.okay(self.cycle());self.assertEqual(self.abs.raw['currentTime'],70)
    def test_completion_and_reopen_both_directions(self):
        self.abs.set(0,True);self.okay(self.cycle());self.assertTrue(self.jf.row['UserData']['Played'])
        self.jf.set(90);self.okay(self.cycle());self.assertFalse(self.abs.raw['isFinished']);self.assertEqual(self.abs.raw['currentTime'],90)
        self.jf.set(0,True);self.okay(self.cycle());self.assertTrue(self.abs.raw['isFinished']);self.assertEqual(self.abs.raw['currentTime'],1000)
        self.abs.set(20);self.okay(self.cycle());self.assertFalse(self.jf.row['UserData']['Played'])
    def test_no_loop_on_unchanged_or_overwrite_existing_book_state(self):
        self.save('book-old',{'baseline':{'untouched':True}});self.abs.set(25);self.okay(self.cycle())
        count=len(self.abs.writes)+len(self.jf.writes);self.okay(self.cycle())
        self.assertEqual(count,len(self.abs.writes)+len(self.jf.writes));self.assertEqual(self.load('book-old'),{'baseline':{'untouched':True}})
    def test_initial_conflict_blocks(self):
        self.abs.set(40);self.jf.set(50);self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.abs.writes or self.jf.writes)
    def test_concurrent_changes_block(self):
        self.abs.set(40);self.okay(self.cycle());self.abs.set(70);self.jf.set(80)
        n=len(self.jf.writes);self.assertTrue(self.cycle()['blocked']);self.assertEqual(n,len(self.jf.writes))
    def test_settle_wait_and_active_player(self):
        self.abs.set(40);r=run(self.b,apply=True,settle=30);self.assertEqual(r['waiting'],1)
        self.clock+=31;self.gate['checked_at']=self.clock;self.write_gate();self.jf.active={'a'*32}
        r=run(self.b,apply=True,settle=30);self.assertEqual(r['waiting'],1);self.assertFalse(self.jf.writes)
        self.jf.active.clear();self.okay(run(self.b,apply=True,settle=30));self.assertTrue(self.jf.writes)
    def test_uncertain_write_not_retried(self):
        self.abs.set(40);self.jf.fail=True;self.assertTrue(self.cycle()['blocked']);n=len(self.jf.writes)
        self.jf.fail=False;self.assertTrue(self.cycle()['blocked']);self.assertEqual(n,len(self.jf.writes))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0],1)
    def test_lost_response_reconciles_only_reached_goal(self):
        self.abs.set(40);orig=self.jf.update
        def arrived(*a):orig(*a);raise RuntimeError('Lost ack')
        self.jf.update=arrived;self.assertTrue(self.cycle()['blocked']);self.jf.update=orig;n=len(self.jf.writes)
        self.okay(self.cycle());self.assertEqual(n,len(self.jf.writes))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0],0)
    def test_concurrent_source_change_after_target_write_blocks(self):
        self.abs.set(40);self.jf.after_write=lambda:self.abs.set(80)
        self.assertTrue(self.cycle()['blocked']);n=len(self.jf.writes);self.jf.after_write=None
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(n,len(self.jf.writes))
    def test_missing_progress_never_resets(self):
        self.abs.set(40);self.okay(self.cycle());self.abs.raw=None
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],40*TICKS)
    def test_initial_all_empty_creates_baseline_without_writes(self):
        self.okay(self.cycle());self.assertFalse(self.abs.writes or self.jf.writes)
        self.assertIn('baseline',self.load('_podcast:show:episode'))
    def test_new_episode_auto_discovered_after_index(self):
        self.jf.available=False;r=self.cycle();self.assertEqual(r['mapped'],0);self.assertEqual(r['waiting'],1)
        self.jf.available=True;self.abs.set(40);self.okay(self.cycle());self.assertEqual(len(self.jf.writes),1)

    def test_new_episode_without_probe_waits_and_joins_only_after_valid_duration(self):
        pine=self.add_pine();self.abs.set(40)
        for ticks in (None,0,0.0):
            self.jf.row['RunTimeTicks']=ticks
            with patch.object(pine,'read_episode',wraps=pine.read_episode) as read:
                result=self.cycle()
            self.okay(result);read.assert_not_called()
            self.assertEqual((result['mapped'],result['waiting']),(0,1))
            self.assertIn('duration',result['episodes'][0]['reason'])
            self.assertEqual(result['episodes'][0]['pine_reason_code'],'jellyfin_not_indexed')
            self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)
            self.assertEqual(self.load('_podcast:show:episode'),{})
        self.jf.row['RunTimeTicks']=1000*TICKS
        self.okay(self.cycle())
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],40*TICKS)
        self.assertEqual(pine.state['position'],40)
        self.assertTrue(self.load('_podcast:show:episode')['pine_enrolled'])

    def test_unprobed_newcomer_does_not_stop_existing_episode_sync(self):
        self.abs.set(40);self.okay(self.cycle());self.abs.set(80)
        second=copy.deepcopy(self.abs.data['media']['episodes'][0])
        second['id']='new-episode';second['title']='New episode'
        second['audioFile']['metadata']['path']='/podcasts/Show/new.mp3'
        second['enclosure']['url']='https://example.org/new.mp3'
        self.abs.data['media']['episodes'].append(second)
        second_jf=copy.deepcopy(self.jf.row)
        second_jf.update(Id='b'*32,Path='/data/podcasts/Show/new.mp3',RunTimeTicks=None)
        second_jf['MediaSources'][0]['Path']=second_jf['Path']
        original=self.jf.items
        def items(**kw):
            return original(**kw) if kw.get('ids') else original(**kw)+[second_jf]
        with patch.object(self.jf,'items',side_effect=items):result=self.cycle()
        self.okay(result)
        self.assertEqual((result['mapped'],result['waiting'],result['synced']),(1,1,1))
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],80*TICKS)
        self.assertEqual(self.load('_podcast:show:new-episode'),{})
        self.assertFalse(self.db.execute("SELECT 1 FROM operations WHERE book='_podcast:show:new-episode'").fetchone())

    def test_known_episode_losing_probe_keeps_baseline_and_progress(self):
        self.abs.set(40);self.okay(self.cycle())
        saved=copy.deepcopy(self.load('_podcast:show:episode'));self.abs.set(80)
        n=len(self.abs.writes)+len(self.jf.writes)
        self.jf.row['RunTimeTicks']=None;result=self.cycle()
        self.assertTrue(result['blocked']);self.assertEqual(result['waiting'],0)
        self.assertIn('previously mapped',result['blocked'][0]['reason'])
        self.assertEqual(self.load('_podcast:show:episode'),saved)
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes),n)

    def test_unprobed_episode_never_hides_pending_or_completed_operation_history(self):
        self.abs.set(40);self.jf.fail=True;self.assertTrue(self.cycle()['blocked'])
        saved=copy.deepcopy(self.load('_podcast:show:episode'));n=len(self.jf.writes)
        self.jf.row['RunTimeTicks']=0;self.jf.fail=False
        self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(self.load('_podcast:show:episode'),saved)
        self.assertEqual(len(self.jf.writes),n)
        # A retained operation still proves previous involvement if state was lost.
        self.db.execute("DELETE FROM state WHERE id='_podcast:show:episode'")
        self.db.execute("UPDATE operations SET status='complete' WHERE book='_podcast:show:episode'");self.db.commit()
        self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.jf.writes),n)

    def test_unprobed_deferral_never_masks_invalid_metadata_or_wrong_file(self):
        self.abs.set(40)
        for ticks in (False,-1,float('nan'),float('inf'),'0',1001.01*TICKS):
            self.jf.row['RunTimeTicks']=ticks
            result=self.cycle();self.assertTrue(result['blocked']);self.assertEqual(result['waiting'],0)
        self.jf.row['RunTimeTicks']=None
        for field,value in (('Type','Audio'),('Id',None)):
            original=self.jf.row[field];self.jf.row[field]=value
            self.assertTrue(self.cycle()['blocked']);self.jf.row[field]=original
        self.jf.row['MediaSources'][0]['Size']=999
        self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.writes or self.jf.writes)

    def test_media_changed_blocks_existing_mapping(self):
        self.abs.set(40);self.okay(self.cycle());self.abs.data['media']['episodes'][0]['audioFile']['metadata']['mtimeMs']+=1
        self.abs.set(80);self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.jf.writes),1)
    def test_jellyfin_size_mismatch_blocks(self):
        self.jf.row['MediaSources'][0]['Size']=999;self.abs.set(40)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
    def test_other_episode_returned_blocks(self):
        self.abs.set(40);self.abs.raw['episodeId']='another'
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
    def test_duplicate_episode_identity_blocks(self):
        self.abs.data['media']['episodes'].append(copy.deepcopy(self.abs.data['media']['episodes'][0]))
        self.assertTrue(self.cycle()['blocked'])
    def test_remote_not_downloaded_episode_not_matched(self):
        self.abs.data['media']['episodes'][0]['audioFile']=None;self.assertEqual(self.cycle()['mapped'],0)
    def test_unsafe_and_outside_paths_block(self):
        for value in ['/etc/password','/podcasts/Show/../other.mp3','/podcasts/Show//episode.mp3']:
            self.abs.data=fixture();self.abs.data['media']['episodes'][0]['audioFile']['metadata']['path']=value
            self.assertTrue(self.cycle()['blocked'],value)
    def test_mount_and_account_guards(self):
        self.gate['podcast_mounts']['worker']['rw']=True;self.write_gate();self.assertTrue(self.cycle()['blocked'])
        self.gate['podcast_mounts']['worker']['rw']=False;self.gate['podcast_mounts']['jf']['source']='/other';self.write_gate()
        self.assertTrue(self.cycle()['blocked'])
        self.gate['podcast_mounts']['jf']['source']='/srv/example-media/podcasts';self.write_gate();self.cfg['podcasts']['expected_abs_username']='other'
        self.assertTrue(self.cycle()['blocked'])
    def test_stale_gate_and_unimplemented_bo_block(self):
        self.clock+=151;self.assertTrue(self.cycle()['blocked']);self.clock-=151
        self.cfg['podcasts']['bookorbit_enabled']=True
        with self.assertRaises(ValueError):run(self.b,apply=True)
    def test_missing_episode_cannot_hide_pending(self):
        self.abs.set(40);self.jf.fail=True;self.cycle();self.abs.data['media']['episodes']=[]
        self.assertTrue(self.cycle()['blocked'])
    def test_abs_finish_step_change_prevents_further_write(self):
        self.abs.set(40);self.okay(self.cycle());self.jf.set(1000,True)
        self.abs.after_write=lambda payload:self.abs.set(80) if 'currentTime'in payload else None
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.abs.raw['isFinished'])
    def test_pine_abs_progress_and_reverse(self):
        p=self.add_pine();self.abs.set(40);self.okay(self.cycle());self.assertEqual(p.state['position'],40)
        p.state.update(position=70,status='in_progress',revision='user-edit',updated_at=self.clock+1)
        self.clock+=2;self.okay(self.cycle());self.assertEqual(self.abs.raw['currentTime'],70)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],70*TICKS)
    def test_pine_reopen_waits_for_separate_timestamp(self):
        p=self.add_pine();self.abs.set(0,True);self.okay(self.cycle());self.abs.set(50);self.clock+=1
        r=self.cycle();self.assertEqual(r['waiting'],1);self.assertEqual(p.state['status'],'not_started')
        self.clock+=1;self.okay(self.cycle());self.assertEqual(p.state['position'],50);self.assertEqual(p.state['status'],'in_progress')
    def test_pine_uncertain_action_not_retried(self):
        p=self.add_pine();self.abs.set(40);p.fail=True;self.assertTrue(self.cycle()['blocked']);n=len(p.writes)
        p.fail=False;self.assertTrue(self.cycle()['blocked']);self.assertEqual(n,len(p.writes))
    def test_pine_lost_ack_verified_then_reconciled(self):
        p=self.add_pine();self.abs.set(40);orig=p.write_action
        def arrived(*a):orig(*a);raise RuntimeError('Lost ack')
        p.write_action=arrived;self.assertTrue(self.cycle()['blocked']);p.write_action=orig;n=len(p.writes)
        self.okay(self.cycle());self.assertEqual(n,len(p.writes))
    def test_pine_pending_cannot_be_disabled(self):
        p=self.add_pine();self.abs.set(40);p.fail=True;self.cycle();self.cfg['podcasts']['pinepods_enabled']=False
        self.assertTrue(self.cycle()['blocked'])
    def test_pine_newcomer_conflicting_progress_blocks(self):
        self.abs.set(40);self.okay(self.cycle());p=self.add_pine();p.state.update(position=80,status='in_progress',missing=False)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(p.writes)
    def test_pine_newcomer_blank_safely_seeded(self):
        self.abs.set(40);self.okay(self.cycle());p=self.add_pine();self.okay(self.cycle());self.assertEqual(p.state['position'],40)

    def actual_adapter(self):
        from pinepods_client import PinepodsClient,action_epoch
        fake=Pine(); client=PinepodsClient('http://pinepods','reader','dummy',device_id='sync',api_key='dummy',server_version='0.9.0')
        native=dict(episode_id=7,user_id=9,duration=1000,position=None,completed=False)
        client._native_user_id=9
        client.probe=fake.probe
        client._native_episode=lambda *a:copy.deepcopy(native)
        client._read_gpodder=lambda *a, **kw:fake.read_log()
        client._model_native=native;client._model_log=fake
        def write(action):
            kind=action.get('_native_action')
            if kind:
                fake.writes.append(copy.deepcopy(action))
                native['completed']=kind=='completed'
                if kind=='uncompleted':native['position']=0
            else:
                fake.write_action(dict(action,timestamp=int(self.clock)))
                if action['action']=='play':native['position']=action['position']
                return {'accepted':True,'timestamp':int(self.clock)}
        client.write_action=write
        self.cfg['podcasts']['pinepods_enabled']=True;self.b.pinepods=client
        self.addCleanup(client.close)
        return client,fake
    def test_real_pine_adapter_plan_interoperates_with_engine(self):
        client,fake=self.actual_adapter();self.abs.set(40);self.okay(self.cycle())
        self.assertEqual(fake.state['position'],40)
        self.clock+=2;self.abs.set(0,True);self.assertEqual(self.cycle()['waiting'],1)
        self.assertEqual(client.read_episode('https://example.org/feed','https://example.org/episode.mp3',1000)['status'],'in_progress')
        self.clock+=1;self.okay(self.cycle())
        self.assertEqual(client.read_episode('https://example.org/feed','https://example.org/episode.mp3',1000)['status'],'finished')
        self.clock+=2;self.abs.set(50);self.assertEqual(self.cycle()['waiting'],1)
        self.assertEqual(client.read_episode('https://example.org/feed','https://example.org/episode.mp3',1000)['status'],'not_started')
        self.clock+=1;self.assertEqual(self.cycle()['waiting'],1);self.clock+=1;self.okay(self.cycle())
        self.assertEqual(client.read_episode('https://example.org/feed','https://example.org/episode.mp3',1000)['position'],50)
    def test_unrepresentable_pine_position_preflight_preserves_abs(self):
        self.actual_adapter();self.jf.set(950)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.abs.writes or self.jf.writes)
        self.assertFalse(self.load('_podcast:show:episode').get('pending'))
    def test_pine_account_replacement_remains_blocked_after_disable(self):
        p=self.add_pine();self.abs.set(40);self.okay(self.cycle())
        self.cfg['podcasts']['pinepods_enabled']=False;self.assertTrue(self.cycle()['blocked'])
        self.cfg['podcasts']['pinepods_enabled']=True;p.identity=dict(p.identity,username='other')
        self.assertTrue(self.cycle()['blocked'])

    def optional_pine(self):
        self.cfg['podcasts']['pinepods_optional_enrollment']=True
        return self.add_pine()

    def test_optional_missing_pine_episode_keeps_core_running_and_visible(self):
        pine=self.optional_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            result=self.cycle()
        self.okay(result)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],40*TICKS)
        row=result['episodes'][0]
        self.assertEqual(row['participants'],['abs','jf'])
        self.assertEqual(row['pine_status'],'waiting')
        self.assertEqual(row['pine_reason_code'],'episode_missing')
        self.assertEqual(result['pine_waiting'],1)
        self.assertFalse(pine.writes)
        self.assertEqual(set(self.load('_podcast:show:episode')['baseline']),{'abs','jf'})

    def test_all_verified_media_incompatibilities_can_defer_never_enrolled_pine(self):
        pine=self.optional_pine()
        for reason in ('feed_missing','feed_ambiguous','episode_missing','episode_ambiguous','episode_identity_unresolved','duration_mismatch','media_identity_mismatch'):
            with self.subTest(reason=reason), patch.object(pine,'read_episode',side_effect=EpisodeUnavailable(reason)):
                result=self.cycle()
                self.okay(result)
                self.assertEqual(result['episodes'][0]['pine_reason_code'],reason)
                self.assertEqual(set(self.load('_podcast:show:episode')['baseline']),{'abs','jf'})
        self.assertFalse(pine.writes or self.abs.writes or self.jf.writes)

    def test_unresolved_optional_identity_keeps_core_and_reason_visible(self):
        pine=self.optional_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_identity_unresolved')):
            result=self.cycle()
        self.okay(result)
        row=result['episodes'][0]
        self.assertEqual(row['pine_status'],'waiting')
        self.assertEqual(row['pine_reason_code'],'episode_identity_unresolved')
        self.assertIn('identity is unresolved',row['pine_reason'])
        self.assertEqual(row['participants'],['abs','jf'])
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],40*TICKS)
        self.assertFalse(pine.writes)
        self.assertFalse(self.load('_podcast:show:episode').get('pine_enrolled'))
        self.okay(self.cycle())
        self.assertEqual(pine.state['position'],40)
        self.assertTrue(self.load('_podcast:show:episode')['pine_enrolled'])

    def test_unresolved_identity_never_drops_existing_pine_or_pending_journal(self):
        pine=self.optional_pine();self.abs.set(40);self.okay(self.cycle())
        original=copy.deepcopy(self.load('_podcast:show:episode'))
        self.abs.set(70);n=len(self.abs.writes)+len(self.jf.writes)+len(pine.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_identity_unresolved')):
            result=self.cycle()
        self.assertTrue(result['blocked']);self.assertEqual(result['pine_waiting'],0)
        self.assertEqual(result['episodes'][0]['pine_status'],'blocked')
        self.assertEqual(self.load('_podcast:show:episode')['baseline'],original['baseline'])
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes)+len(pine.writes),n)
        # Even a legacy baseline without Pine keys must honor its durable operation.
        stored=copy.deepcopy(original);stored.pop('pine_enrolled');stored.pop('pine_raw')
        stored['baseline']={k:v for k,v in stored['baseline'].items() if k in ('abs','jf')}
        self.save('_podcast:show:episode',stored)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_identity_unresolved')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes)+len(pine.writes),n)

    def test_unresolved_identity_keeps_partial_admission_journal_untouched(self):
        pine=self.optional_pine();self.abs.set(40);pine.fail=True
        self.assertTrue(self.cycle()['blocked'])
        pending=copy.deepcopy(self.load('_podcast:show:episode')['pending'])
        n=len(self.abs.writes)+len(self.jf.writes)+len(pine.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_identity_unresolved')):
            result=self.cycle()
        self.assertTrue(result['blocked']);self.assertEqual(result['pine_waiting'],0)
        self.assertEqual(self.load('_podcast:show:episode')['pending'],pending)
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes)+len(pine.writes),n)

    def test_unresolved_identity_does_not_bypass_required_pine_mode(self):
        pine=self.add_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_identity_unresolved')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(pine.writes or self.abs.writes or self.jf.writes)

    def test_optional_admission_requires_explicit_boolean_opt_in(self):
        pine=self.add_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)
        self.cfg['podcasts']['pinepods_optional_enrollment']='yes'
        with self.assertRaises(ValueError):self.cycle()

    def test_optional_mode_never_masks_auth_transport_or_incomplete_inventory(self):
        pine=self.optional_pine();self.abs.set(40)
        for error in (PineRequestError('PinePods: transport failure'),
                PineRequestError('PinePods: API request failed',401),
                PineRequestError('PinePods: API request failed',503),
                PineRequestError('PinePods: episode inventory is incomplete or changed'),
                PineRequestError('PinePods: invalid native episode progress')):
            with self.subTest(error=str(error)),patch.object(pine,'read_episode',side_effect=error):
                result=self.cycle()
                self.assertTrue(result['blocked'])
                self.assertEqual(result['pine_waiting'],0)
        self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)
        self.assertNotIn('baseline',self.load('_podcast:show:episode'))

    def test_optional_missing_source_urls_do_not_block_core_or_call_pine(self):
        pine=self.optional_pine();self.abs.set(40)
        self.abs.data['media']['metadata']['feedUrl']=None
        with patch.object(pine,'read_episode') as read:
            result=self.cycle()
        self.okay(result);read.assert_not_called()
        self.assertEqual(result['episodes'][0]['pine_reason_code'],'source_identity_missing')
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],40*TICKS)

    def test_optional_duplicate_source_urls_defer_both_files(self):
        self.optional_pine()
        second=copy.deepcopy(self.abs.data['media']['episodes'][0])
        second['id']='episode-two';second['audioFile']['metadata']['path']='/podcasts/Show/two.mp3'
        self.abs.data['media']['episodes'].append(second)
        second_jf=copy.deepcopy(self.jf.row)
        second_jf.update(Id='b'*32,Path='/data/podcasts/Show/two.mp3')
        second_jf['MediaSources'][0]['Path']=second_jf['Path']
        with patch.object(self.jf,'items',return_value=[self.jf.row,second_jf]):
            mappings,_=PodcastCoordinator(self.b).mappings()
        self.assertEqual(len(mappings),2)
        self.assertTrue(all(m['_pine_unavailable']=='source_identity_ambiguous' for m in mappings))

    def test_optional_episode_can_join_later_without_forced_reset(self):
        pine=self.optional_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            self.okay(self.cycle())
        before_abs=copy.deepcopy(self.abs.raw);before_jf=copy.deepcopy(self.jf.row['UserData'])
        result=self.cycle();self.okay(result)
        self.assertEqual(pine.state['position'],40)
        self.assertEqual(self.abs.raw,before_abs)
        self.assertEqual(self.jf.row['UserData'],before_jf)
        self.assertEqual(result['episodes'][0]['pine_status'],'enrolled')
        self.assertEqual(set(result['episodes'][0]['participants']),{'abs','jf','pinepods','gpodder'})
        self.assertTrue(self.load('_podcast:show:episode')['pine_enrolled'])

    def test_optional_newcomer_conflict_preserves_core_baseline_and_all_progress(self):
        pine=self.optional_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('feed_missing')):
            self.okay(self.cycle())
        baseline=copy.deepcopy(self.load('_podcast:show:episode')['baseline'])
        pine.state.update(position=80,status='in_progress',missing=False)
        n=len(self.abs.writes)+len(self.jf.writes)
        result=self.cycle()
        self.assertTrue(result['blocked']);self.assertEqual(result['pine_waiting'],0)
        self.assertEqual(self.load('_podcast:show:episode')['baseline'],baseline)
        self.assertFalse(pine.writes)
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes),n)

    def test_enrolled_pine_mismatch_cannot_silently_fall_back_to_core(self):
        pine=self.optional_pine();self.abs.set(40);self.okay(self.cycle())
        baseline=copy.deepcopy(self.load('_podcast:show:episode')['baseline'])
        self.abs.set(70);n=len(self.jf.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('duration_mismatch')):
            result=self.cycle()
        self.assertTrue(result['blocked'])
        self.assertEqual(result['episodes'][0]['pine_status'],'blocked')
        self.assertEqual(len(self.jf.writes),n)
        self.assertEqual(self.load('_podcast:show:episode')['baseline'],baseline)

    def test_completed_operation_history_prevents_loss_after_legacy_baseline_drop(self):
        pine=self.optional_pine();self.abs.set(40);self.okay(self.cycle())
        stored=self.load('_podcast:show:episode')
        stored.pop('pine_enrolled');stored.pop('pine_raw')
        stored['baseline']={k:v for k,v in stored['baseline'].items() if k in ('abs','jf')}
        self.save('_podcast:show:episode',stored)
        self.abs.set(70);n=len(self.jf.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.jf.writes),n)

    def test_pending_four_peer_journal_never_downgrades_on_media_mismatch(self):
        pine=self.optional_pine();self.abs.set(40);pine.fail=True
        self.assertTrue(self.cycle()['blocked'])
        journal=copy.deepcopy(self.load('_podcast:show:episode')['pending'])
        n=len(self.jf.writes)+len(self.abs.writes)+len(pine.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(self.load('_podcast:show:episode')['pending'],journal)
        self.assertEqual(len(self.jf.writes)+len(self.abs.writes)+len(pine.writes),n)

    def test_failed_admission_retains_existing_core_baseline_until_confirmed(self):
        self.abs.set(40);self.okay(self.cycle())
        baseline=copy.deepcopy(self.load('_podcast:show:episode')['baseline'])
        pine=self.optional_pine();pine.fail=True
        self.assertTrue(self.cycle()['blocked'])
        stored=self.load('_podcast:show:episode')
        self.assertEqual(stored['baseline'],baseline)
        self.assertEqual(set(stored['pending']['before']),{'abs','jf','pinepods','gpodder'})
        self.assertTrue(stored['pine_enrolled'])
        n=len(pine.writes)
        pine.fail=False
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('duration_mismatch')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(pine.writes),n)
        self.assertEqual(self.load('_podcast:show:episode')['baseline'],baseline)

    def test_media_incompatibility_after_first_read_cannot_change_participant_scope(self):
        pine=self.optional_pine();self.jf.set(40)
        original=pine.read_episode;reads=[]
        def changed(*args):
            reads.append(True)
            if len(reads)>=4:
                raise EpisodeUnavailable('episode_missing')
            return original(*args)
        with patch.object(pine,'read_episode',side_effect=changed):
            result=self.cycle()
        self.assertTrue(result['blocked'])
        stored=self.load('_podcast:show:episode')
        self.assertTrue(stored['pine_enrolled'])
        self.assertEqual(set(stored['pending']['before']),{'abs','jf','pinepods','gpodder'})
        self.assertEqual(result['pine_waiting'],0)
        n=len(self.abs.writes)+len(self.jf.writes)+len(pine.writes)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.abs.writes)+len(self.jf.writes)+len(pine.writes),n)

    def test_existing_core_pending_is_finished_before_new_pine_participant_joins(self):
        self.abs.set(40);original=self.jf.update
        def arrived(*args):original(*args);raise RuntimeError('Lost ack')
        self.jf.update=arrived;self.assertTrue(self.cycle()['blocked']);self.jf.update=original
        pine=self.optional_pine()
        result=self.cycle();self.okay(result)
        self.assertFalse(pine.writes)
        self.assertEqual(result['episodes'][0]['pine_reason_code'],'core_transfer_pending')
        self.assertEqual(set(self.load('_podcast:show:episode')['baseline']),{'abs','jf'})
        self.okay(self.cycle());self.assertEqual(pine.state['position'],40)

    def test_native_account_identity_can_be_enriched_only_before_enrollment(self):
        pine=self.optional_pine()
        old=dict(pine.identity,native_user_id=None)
        self.save('_podcast_accounts',{'pinepods':old})
        pine.identity=dict(old,native_user_id=9)
        self.okay(self.cycle())
        self.assertEqual(self.load('_podcast_accounts')['pinepods']['native_user_id'],9)
        # A stale account pin cannot justify replacing identities already used
        # in an episode baseline, even if its textual account name is the same.
        self.save('_podcast_accounts',{'pinepods':old})
        self.assertTrue(self.cycle()['blocked'])

    def test_optional_preview_never_admits_or_mutates_unmatched_episode(self):
        pine=self.optional_pine();self.abs.set(40)
        with patch.object(pine,'read_episode',side_effect=EpisodeUnavailable('episode_missing')):
            result=preview(self.b)
        self.okay(result)
        self.assertEqual(result['episodes'][0]['pine_status'],'waiting')
        self.assertEqual(result['proposed'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM state').fetchone()[0],0)
        self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)

    def test_first_multistep_pine_transfer_is_shown_as_joining_until_confirmed(self):
        self.actual_adapter();self.cfg['podcasts']['pinepods_optional_enrollment']=True
        self.abs.set(0,True)
        result=self.cycle();self.okay(result)
        self.assertEqual(result['waiting'],1)
        self.assertEqual(result['pine_joining'],1)
        self.assertEqual(result['pine_enrolled'],0)
        self.assertTrue(self.load('_podcast:show:episode')['pine_enrolled'])
        self.advance()
        result=self.cycle();self.okay(result)
        self.assertEqual(result['pine_joining'],0)
        self.assertEqual(result['pine_enrolled'],1)

    def test_acknowledged_play_without_stored_receipt_stays_pending_without_retry(self):
        # The reviewed local DB trigger could suppress a repeated play after
        # a reset while Go still applied its native position and returned 200.
        client,log=self.actual_adapter()
        log.write_action(dict(action='new',timestamp=self.clock-30,device='sync'))
        client._model_native['position']=0
        self.okay(self.cycle())
        baseline=copy.deepcopy(self.load('_podcast:show:episode')['baseline'])
        self.jf.set(0,True)
        calls=[]
        def suppressed_receipt(action):
            calls.append(copy.deepcopy(action))
            self.assertNotIn('_native_action',action)
            self.assertEqual(action['action'],'play')
            client._model_native['position']=action['position']
            # Deliberately retain the old new action; no log echo is present.
            return {'accepted':True,'timestamp':int(self.clock)}
        client.write_action=suppressed_receipt
        result=self.cycle()
        self.assertTrue(result['blocked'])
        state=self.load('_podcast:show:episode')
        self.assertEqual(state['pending']['stage'],'sending_pinepods_0')
        self.assertEqual(state['pending']['pine_next'],0)
        self.assertEqual(state['pending']['pine_plan'][0]['_server_timestamp'],self.clock)
        self.assertEqual(state['baseline'],baseline)
        self.assertEqual(client._model_native['position'],1000)
        self.assertFalse(client._model_native['completed'])
        self.assertEqual(log.state['action'],'new')
        self.assertTrue(self.abs.raw['isFinished'])
        self.assertTrue(self.jf.row['UserData']['Played'])
        self.advance()
        self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(calls),1)
        self.assertFalse(client._model_native['completed'])
        self.assertEqual(self.load('_podcast:show:episode')['pending'],state['pending'])
    def test_pine_same_second_new_change_waits_without_partial_write(self):
        p=self.add_pine();self.abs.set(40);self.okay(self.cycle());self.abs.set(50)
        count=len(self.jf.writes);self.assertEqual(self.cycle()['waiting'],1)
        self.assertEqual(len(self.jf.writes),count);self.clock+=1;self.okay(self.cycle())
    def test_pine_mid_reopen_concurrent_edit_blocks_second_step(self):
        p=self.add_pine();self.abs.set(0,True);self.okay(self.cycle());self.clock+=1;self.abs.set(50)
        self.assertEqual(self.cycle()['waiting'],1);count=len(p.writes)
        self.clock+=1;self.jf.set(70);self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(p.writes),count)
    def test_pine_requires_host_version_receipt(self):
        self.add_pine();self.gate['pinepods_ok']=False;self.write_gate();self.abs.set(40)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
    def test_invalid_pine_device_blocks_without_writes(self):
        p=self.add_pine();p.probe=lambda:{'device_exists':False};self.abs.set(40)
        self.assertTrue(self.cycle()['blocked']);self.assertFalse(self.jf.writes)
    def test_podcast_completed_normalizes_to_end_without_resume_conflict(self):
        self.abs.set(0,True);self.jf.set(10,True);self.okay(self.cycle())
        self.assertFalse(self.abs.writes or self.jf.writes)
    def test_abs_or_jf_endpoint_change_cannot_reuse_old_baseline(self):
        self.abs.set(40);self.okay(self.cycle());self.cfg['abs_url']='http://other-abs'
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(len(self.jf.writes),1)
    def test_remote_unrelated_feed_actions_do_not_mutate_subscriptions(self):
        p=self.add_pine();self.abs.set(40);self.okay(self.cycle())
        self.assertEqual(len(p.writes),1);self.assertEqual(p.writes[0]['action'],'play')

    def advance(self,seconds=60):
        self.clock+=seconds;self.gate['checked_at']=self.clock;self.write_gate()
    def finish_pending(self,limit=5):
        result=self.cycle()
        for _ in range(limit):
            self.okay(result)
            if not result['waiting']:return result
            self.advance();result=self.cycle()
        self.fail('Pending transfer did not finish: '+repr(result))
    def test_initial_native_pine_progress_is_imported_without_rewind(self):
        client,log=self.actual_adapter();client._model_native['position']=170
        self.okay(self.finish_pending())
        self.assertEqual(self.abs.raw['currentTime'],170)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],170*TICKS)
        self.assertEqual(log.state['position'],170)
        self.assertEqual(client._model_native['position'],170)
    def test_initial_native_and_old_gpodder_conflict_never_choose_furthest(self):
        client,log=self.actual_adapter();client._model_native['position']=170
        log.write_action(dict(action='play',position=35,total=1000,timestamp=self.clock-500,device='phone'))
        before=len(log.writes);self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.writes or self.jf.writes);self.assertEqual(len(log.writes),before)
        self.assertEqual(client._model_native['position'],170)
    def test_external_gpodder_completion_repairs_native_flag_and_other_apps(self):
        client,log=self.actual_adapter();self.abs.set(40);self.okay(self.cycle());self.advance()
        log.write_action(dict(action='play',position=1000,total=1000,timestamp=self.clock,device='phone'))
        client._model_native['position']=1000  # Go play side effect, completed stays false.
        self.assertFalse(client._model_native['completed'])
        self.finish_pending()
        self.assertTrue(client._model_native['completed']);self.assertTrue(self.abs.raw['isFinished'])
        self.assertTrue(self.jf.row['UserData']['Played'])
    def test_external_gpodder_new_resets_without_abs_record_deletion(self):
        client,log=self.actual_adapter();self.abs.set(0,True);self.finish_pending();self.advance()
        log.write_action(dict(action='new',timestamp=self.clock,device='phone'))
        self.assertTrue(client._model_native['completed']) # Go new does not touch native state.
        self.finish_pending()
        self.assertFalse(client._model_native['completed']);self.assertEqual(client._model_native['position'],0)
        self.assertIsNotNone(self.abs.raw);self.assertFalse(self.abs.raw['isFinished']);self.assertEqual(self.abs.raw['currentTime'],0)
        self.assertFalse(self.jf.row['UserData']['Played']);self.assertEqual(self.jf.row['UserData']['PlayCount'],0)
    def test_native_pine_reset_exports_new_action_to_gpodder(self):
        client,log=self.actual_adapter();self.abs.set(0,True);self.finish_pending();self.advance()
        client._model_native.update(position=0,completed=False)
        self.finish_pending()
        self.assertEqual(log.state['action'],'new');self.assertEqual(self.abs.raw['currentTime'],0)
        self.assertFalse(self.jf.row['UserData']['Played'])
    def test_abs_explicit_zero_reset_keeps_record_and_clears_jf_status(self):
        self.abs.set(0,True);self.okay(self.cycle());self.abs.set(0,False)
        self.okay(self.cycle());self.assertIsNotNone(self.abs.raw)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],0)
        self.assertFalse(self.jf.row['UserData']['Played']);self.assertEqual(self.jf.row['UserData']['PlayCount'],0)
    def test_gpodder_missing_record_is_not_reset_instruction(self):
        client,log=self.actual_adapter();self.abs.set(40);self.okay(self.cycle())
        log.state=Pine().state
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(self.abs.raw['currentTime'],40)
    def test_new_gpodder_action_with_independent_native_edit_conflicts(self):
        client,log=self.actual_adapter();self.abs.set(40);self.okay(self.cycle());self.advance()
        log.write_action(dict(action='play',position=80,total=1000,timestamp=self.clock,device='phone'))
        client._model_native['position']=70
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(self.abs.raw['currentTime'],40)
    def test_delayed_go_actions_never_share_server_generated_timestamp(self):
        client,log=self.actual_adapter();self.abs.set(0,True);self.finish_pending();self.advance()
        self.abs.set(50)
        self.assertEqual(self.cycle()['waiting'],1) # Native reset.
        self.advance(60)
        self.assertEqual(self.cycle()['waiting'],1) # New action server timestamp is NOW, not original plan.
        self.assertEqual(log.state['action'],'new')
        self.advance(60);self.okay(self.cycle());self.assertEqual(log.state['position'],50)
        wire=[x for x in log.writes if not x.get('_native_action')]
        stamps=[action_epoch(x) for x in wire]
        self.assertEqual(len(stamps),len(set(stamps)))
    def test_new_user_progress_during_reset_is_not_restored_from_saved_goal(self):
        client,log=self.actual_adapter();self.abs.set(0,True);self.finish_pending();self.advance()
        self.abs.set(50);self.assertEqual(self.cycle()['waiting'],1)
        client._model_native['position']=90;self.advance()
        writes=len(log.writes);self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(client._model_native['position'],90);self.assertEqual(len(log.writes),writes)
    def test_global_failure_alert_is_deduplicated_and_recovers(self):
        self.gate['podcasts_ok']=False;self.write_gate();self.cycle();self.cycle()
        self.assertEqual(len(self.alerts),1)
        self.gate['podcasts_ok']=True;self.write_gate();self.okay(self.cycle())
        self.assertEqual(self.load('_podcast_global_error'),{})
    def test_real_bridge_podcast_only_without_bookorbit_or_bookbridge_credentials(self):
        from sync import Bridge
        for provider in ('bookorbit','bookbridge'):
            with self.subTest(provider=provider):
                root=self.root/provider;root.mkdir()
                root.joinpath('deployment.json').write_text(json.dumps(self.gate))
                cfg=copy.deepcopy(self.cfg)
                cfg.update(abs_url='http://abs',mapping_provider=provider,
                    jellyfin=dict(enabled=True,url='http://jellyfin',user_id='b'*32,username='reader',version='10.11.0'))
                with patch('sync.ABSClient',return_value=self.abs), \
                     patch('jellyfin_client.JellyfinClient',return_value=self.jf), \
                     patch('sync.BOClient',side_effect=AssertionError('Podcast-only mode constructed BookOrbit')), \
                     patch('bookbridge_client.BookBridgeClient',side_effect=AssertionError('Podcast-only mode constructed BookBridge')):
                    bridge=Bridge(root,cfg,{'abs_token':'test-abs','jellyfin_token':'test-jf'},clock=lambda:self.clock)
                try:
                    self.assertIsNone(bridge.bo);self.assertIsNone(bridge.bookbridge)
                    self.okay(preview(bridge));self.okay(run(bridge,apply=True,settle=0))
                    self.assertEqual(bridge.load('_podcast_accounts')['abs']['username'],'reader')
                    self.assertFalse(self.abs.writes or self.jf.writes)
                finally:bridge.db.close()
    def test_extra_jellyfin_entries_do_not_join_podcast_sync(self):
        original=self.jf.items
        extra=copy.deepcopy(self.jf.row);extra.update(Id='f'*32,Path='/data/podcasts/Other/unmatched.mp3')
        def inventory(**kw):
            rows=original(**kw)
            return rows+[extra] if kw.get('parent') else rows
        self.jf.items=inventory;self.abs.set(40)
        result=self.cycle();self.okay(result)
        self.assertEqual(result['mapped'],1);self.assertEqual(len(self.jf.writes),1)
        self.assertEqual(extra['UserData']['PlaybackPositionTicks'],0)
    def test_same_username_with_replaced_abs_account_id_blocks(self):
        self.cfg['podcasts']['expected_abs_user_id']='replaced-user'
        self.abs.set(40);self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.writes or self.jf.writes)
    def test_service_drops_mid_cycle_defers_remaining_without_more_reads(self):
        coordinator=PodcastCoordinator(self.b);mappings,_=coordinator.mappings()
        mappings=[dict(mappings[0],id='_podcast:show:episode'+str(n)) for n in range(240)]
        for failure in (RequestError('GET progress: transport failure'),
                        RequestError('API request failed',429),RuntimeError('Jellyfin GET: HTTP 503')):
            with self.subTest(failure=str(failure)), \
                 patch.object(PodcastCoordinator,'mappings',return_value=(mappings,[])), \
                 patch.object(PodcastCoordinator,'read',side_effect=failure) as read:
                result=self.cycle()
            self.assertEqual(read.call_count,1)
            self.assertEqual(len(result['blocked']),1);self.assertEqual(result['waiting'],239)
            self.assertEqual(len(result['episodes']),240)
        self.assertFalse(self.abs.writes or self.jf.writes)
    def test_one_bad_episode_does_not_defer_unrelated_episodes(self):
        coordinator=PodcastCoordinator(self.b);mappings,_=coordinator.mappings()
        mappings=[dict(mappings[0],id='_podcast:show:episode'+str(n)) for n in range(3)]
        with patch.object(PodcastCoordinator,'mappings',return_value=(mappings,[])), \
             patch.object(PodcastCoordinator,'read',side_effect=ValueError('Episode identity mismatch')) as read:
            result=self.cycle()
        self.assertEqual(read.call_count,3);self.assertEqual(len(result['blocked']),3)
    def test_concurrent_gpodder_action_during_native_step_is_not_absorbed(self):
        client,log=self.actual_adapter();self.abs.set(0,True)
        self.assertEqual(self.cycle()['waiting'],1);self.advance()
        write=client.write_action
        def raced(action):
            result=write(action)
            if action.get('_native_action')=='completed':
                log.write_action(dict(action='play',position=80,total=1000,timestamp=self.clock,device='phone'))
                client._model_native['position']=80
            return result
        client.write_action=raced
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(log.state['position'],80)
        self.assertEqual(client._model_native['position'],80)
        writes=len(log.writes);self.advance();self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(log.writes),writes)
    def test_concurrent_completion_during_go_play_is_not_absorbed(self):
        client,log=self.actual_adapter();self.abs.set(40)
        write=client.write_action
        def raced(action):
            result=write(action)
            if not action.get('_native_action'):client._model_native['completed']=True
            return result
        client.write_action=raced
        self.assertTrue(self.cycle()['blocked']);self.assertTrue(client._model_native['completed'])
        writes=len(log.writes);self.advance();self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(log.writes),writes)
    def test_preview_rejects_indexed_episode_without_personal_library_access(self):
        self.abs.set(120);self.jf.policy=dict(EnableAllFolders=False,EnabledFolders=['another-library'])
        self.assertEqual(len(self.jf.items()),1) # Inventory visibility does not prove UserData access.
        with self.assertRaisesRegex(RuntimeError,'cannot access.*podcast library'):preview(self.b)
        self.assertFalse(self.abs.writes or self.jf.writes)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM state').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
    def test_specific_podcast_library_access_is_sufficient(self):
        self.jf.policy=dict(EnableAllFolders=False,EnabledFolders=['jf-podcasts'])
        self.abs.set(120);self.okay(self.cycle());self.assertEqual(len(self.jf.writes),1)
    def test_revoked_podcast_access_blocks_new_transfer_without_pending_journal(self):
        self.abs.set(40);self.okay(self.cycle());self.abs.set(80)
        self.jf.policy=dict(EnableAllFolders=False,EnabledFolders=[])
        result=self.cycle();self.assertTrue(result['blocked']);self.assertEqual(len(self.jf.writes),1)
        self.assertFalse(self.load('_podcast:show:episode').get('pending'))
    def test_missing_or_malformed_personal_library_policy_fails_closed(self):
        for policy in ({},{'EnableAllFolders':'true'},{'EnableAllFolders':False,'EnabledFolders':'jf-podcasts'}):
            with self.subTest(policy=policy):
                self.jf.policy=policy;self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.writes or self.jf.writes)


if __name__=='__main__':unittest.main()
