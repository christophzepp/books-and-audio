"""Read-only account-scoped inventory capture with simulated native APIs."""
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit
from tracking_source import NativeTrackingSource,TrackingSourceError
from pinepods_client import action_epoch

FEED='https://feed.test/private?key=PRIVATE_FEED'
URL='https://media.test/episode.mp3'
AUTH='Basic '+base64.b64encode(b'reader:PRIVATE_PASSWORD').decode()
STAMP='2026-11-07T09:00:00'

def episode(ident=7,**changes):
    row=dict(Episodeid=ident,Episodeurl=URL+str(ident),Episodepubdate='2020-01-01T00:00:00',
             Episodeduration=600,Listenduration=None,Completed=False,is_youtube=False,is_video=False)
    row.update(changes);return row

def history(row,stamp=STAMP):
    return dict(episodeid=row['Episodeid'],episodeurl=row['Episodeurl'],listendate=stamp,
                listenduration=row['Listenduration'],completed=row['Completed'],is_youtube=row['is_youtube'])

def response(value,status=200):
    return Mock(status_code=status,content=json.dumps(value).encode(),json=Mock(return_value=copy.deepcopy(value)))

class Upstream:
    def __init__(self):
        self.user_id=2;self.username='reader';self.pods=[{'podcastid':11,'feedurl':FEED}]
        self.rows={11:[episode()]};self.history=[];self.calls=[];self.hook=None
    def __call__(self,method,url,**kw):
        path=urlsplit(url).path;p=kw.get('params') or {};self.calls.append((method,path,copy.deepcopy(kw)))
        if self.hook:
            override=self.hook(method,path,kw)
            if override is not None:return override
        if path=='/api/data/get_user':data={'status':'success','retrieved_id':self.user_id}
        elif '/user_details_id/' in path:data={'UserID':self.user_id,'Username':self.username}
        elif '/return_pods/' in path:data={'pods':self.pods}
        elif path=='/api/data/podcast_episodes':
            rows=self.rows[p['podcast_id']];data={'episodes':rows[p['offset']:p['offset']+p['limit']],'total':len(rows)}
        elif '/user_history/' in path:data={'data':self.history[p['offset']:p['offset']+p['limit']],'total':len(self.history)}
        else:raise AssertionError('unexpected endpoint')
        return response(data)

class NativeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.key=Path(self.tmp.name)/'api-key';self.key.write_text('PRIVATE_KEY\n')
        self.cfg=dict(upstream='http://pinepods:8040',username='reader',api_key_file=str(self.key),server_version='0.9.0')
        self.up=Upstream();self.source=NativeTrackingSource(self.cfg,request=self.up)
    def capture(self):return self.source.capture(AUTH)
    def test_native_completed_without_history_or_gpodder_action_is_retained(self):
        self.up.rows[11]=[episode(Completed=True)]
        self.assertEqual(self.capture(),[dict(id='2:11:7',podcast=FEED,episode=URL+'7',completed=True,position=0,total=600,published_at=action_epoch('2020-01-01T00:00:00'))])
        self.assertTrue(self.source.last_status['ok'])
        self.assertFalse(any('get_episode_metadata' in path or '/api/2/' in path for _,path,_ in self.up.calls))
    def test_personal_key_gets_only_pinned_origin_no_redirects(self):
        self.capture()
        for method,path,kw in self.up.calls:
            self.assertEqual(method,'GET');self.assertTrue(path.startswith('/api/data/'))
            self.assertEqual(kw['headers']['Api-Key'],'PRIVATE_KEY');self.assertNotIn('Authorization',kw['headers'])
            self.assertEqual(kw['timeout'],(5,20));self.assertIs(kw['allow_redirects'],False);self.assertNotIn('json',kw)
        self.assertNotIn('PRIVATE',json.dumps(self.source.last_status))
    def test_personal_api_owner_required_before_inventory(self):
        for uid,name in ((1,'reader'),(True,'reader'),(2,'other')):
            self.up.user_id,self.up.username=uid,name;self.up.calls.clear()
            with self.subTest(uid=uid,name=name),self.assertRaisesRegex(TrackingSourceError,'^native_account_unverified$'):self.capture()
            self.assertFalse(any('/podcast_episodes' in path for _,path,_ in self.up.calls))
    def test_basic_user_must_match_without_request(self):
        for auth in (None,'Bearer secret','Basic ???','Basic '+base64.b64encode(b'other:PRIVATE_PASSWORD').decode()):
            with self.subTest(auth=auth),self.assertRaises(TrackingSourceError):self.source.capture(auth)
        self.assertEqual(self.up.calls,[])
    def test_account_pinned_after_success_and_explicit_across_restart(self):
        self.capture();self.up.user_id=3
        with self.assertRaisesRegex(TrackingSourceError,'^native_account_changed$'):self.capture()
        self.assertFalse(self.source.last_status['ok'])
        self.source=NativeTrackingSource(dict(self.cfg,user_id=2),request=self.up)
        with self.assertRaisesRegex(TrackingSourceError,'^native_account_changed$'):self.capture()
    def test_all_feeds_and_pages_descending(self):
        self.up.rows[11]=[episode(i) for i in range(1,202)]
        self.up.pods.append({'podcastid':12,'feedurl':'https://second.test/feed'});self.up.rows[12]=[episode(1000)]
        self.assertEqual(len(self.capture()),202)
        pages=[kw['params'] for _,path,kw in self.up.calls if path.endswith('/podcast_episodes')]
        self.assertEqual([(p['podcast_id'],p['offset']) for p in pages],[(11,0),(11,200),(12,0)])
        self.assertTrue(all(p['sort_order']=='desc' and p['sort_by']=='date' and p['filter']=='all' for p in pages))
    def test_last_page_outage_never_returns_partial_capture(self):
        self.up.rows[11]=[episode(i) for i in range(1,202)]
        def fail(method,path,kw):
            if path.endswith('/podcast_episodes') and kw['params']['offset']==200:raise OSError('PRIVATE_BODY')
        self.up.hook=fail
        with self.assertRaisesRegex(TrackingSourceError,'^native_transport_unavailable$'):self.capture()
        self.assertEqual(self.source.last_status,{'ok':False,'error':'native_transport_unavailable'})
    def test_changed_total_empty_midpage_or_duplicate_ids_fail(self):
        self.up.rows[11]=[episode(i) for i in range(1,202)]
        for result in ({'episodes':[episode(201)],'total':202},{'episodes':[],'total':201},{'episodes':[episode(1)],'total':201}):
            def alter(method,path,kw):
                if path.endswith('/podcast_episodes') and kw['params']['offset']==200:return response(result)
            self.up.hook=alter
            with self.subTest(result=result),self.assertRaises(TrackingSourceError):self.capture()
    def test_feed_scope_change_and_duplicate_feeds_fail(self):
        count=[0]
        def alter(method,path,kw):
            if '/return_pods/' in path:
                count[0]+=1
                if count[0]==2:return response({'pods':[{'podcastid':11,'feedurl':'https://changed.test/feed'}]})
        self.up.hook=alter
        with self.assertRaisesRegex(TrackingSourceError,'^native_feed_scope_changed$'):self.capture()
        self.up.hook=None;self.up.pods.append({'podcastid':12,'feedurl':FEED})
        with self.assertRaisesRegex(TrackingSourceError,'^native_feed_inventory_invalid$'):self.capture()
    def test_same_native_episode_under_two_feeds_fails(self):
        self.up.pods.append({'podcastid':12,'feedurl':'https://second.test/feed'});self.up.rows[12]=[episode()]
        with self.assertRaisesRegex(TrackingSourceError,'^native_episode_scope_ambiguous$'):self.capture()
    def test_duplicate_enclosures_do_not_become_two_listening_events(self):
        self.up.rows[11]=[episode(1),episode(2,Episodeurl=URL+'1')]
        with self.assertRaisesRegex(TrackingSourceError,'^native_episode_url_ambiguous$'):self.capture()
    def test_failed_input_after_success_clears_success_status(self):
        self.capture()
        with self.assertRaises(TrackingSourceError):self.source.capture('Basic ???')
        self.assertFalse(self.source.last_status['ok'])
        self.key.write_text('')
        with self.assertRaises(TrackingSourceError):self.capture()
        self.assertEqual(self.source.last_status,{'ok':False,'error':'native_key_unavailable'})
    def test_missing_url_duration_youtube_pending_do_not_block_other_rows(self):
        self.up.rows[11]=[episode(1),episode(2,Episodeurl=''),episode(3,Episodeduration=0),episode(4,Episodeduration=None),episode(5,is_youtube=True)]
        self.assertEqual([x['id'] for x in self.capture()],['2:11:1'])
        s=self.source.last_status
        self.assertEqual((s['inventory_rows'],s['pending_missing_url'],s['pending_missing_duration'],s['skipped_youtube']),(5,1,2,1))
    def test_stale_unfinished_position_is_pending_without_clipping_or_blocking_other_feeds(self):
        self.up.rows[11]=[episode(1,Listenduration=650,Episodeduration=600,Completed=False),episode(2,Completed=True,Listenduration=650,Episodeduration=600)]
        self.up.pods.append({'podcastid':12,'feedurl':'https://second.test/feed'});self.up.rows[12]=[episode(3,Listenduration=17)]
        result=self.capture()
        self.assertEqual([row['id'] for row in result],['2:11:2','2:12:3'])
        self.assertEqual(result[0]['position'],650)
        self.assertTrue(result[0]['completed'])
        self.assertEqual(result[1]['position'],17)
        self.assertEqual(self.source.last_status['pending_invalid_progress'],1)
        self.assertTrue(self.source.last_status['ok'])
        self.up.rows[11][0]['Episodeduration']=700
        self.assertEqual([row['id'] for row in self.capture()],['2:11:1','2:11:2','2:12:3'])
        self.assertEqual(self.source.last_status['pending_invalid_progress'],0)
    def test_invalid_native_values_cannot_fabricate_state(self):
        for change in ({'Listenduration':True},{'Listenduration':-1},{'Listenduration':float('nan')},{'Completed':1},{'Episodepubdate':'bad'},{'Episodeduration':False},{'Episodeduration':-1}):
            self.up.rows[11]=[episode(**change)]
            with self.subTest(change=change),self.assertRaises(TrackingSourceError):self.capture()
    def test_old_publication_heard_today_gets_true_native_time(self):
        self.up.rows[11]=[episode(Listenduration=137,Completed=True)];self.up.history=[history(self.up.rows[11][0])]
        self.source.history_since=action_epoch('2026-11-06T00:00:00');row=self.capture()[0]
        self.assertEqual(row['published_at'],action_epoch('2020-01-01T00:00:00'));self.assertEqual(row['updated_at'],action_epoch(STAMP));self.assertEqual(row['position'],137)
    def test_future_native_history_is_untrusted_without_time_shift_or_state_loss(self):
        self.up.rows[11]=[episode(1,Listenduration=50,Completed=True),episode(2,Listenduration=17)]
        self.up.history=[history(self.up.rows[11][0],'2026-11-07T10:00:00'),history(self.up.rows[11][1],'2026-11-07T08:00:00')]
        self.source=NativeTrackingSource(self.cfg,request=self.up,clock=lambda:action_epoch(STAMP))
        result=self.capture()
        self.assertNotIn('updated_at',result[0])
        self.assertTrue(result[0]['completed'])
        self.assertEqual(result[0]['position'],50)
        self.assertEqual(result[1]['updated_at'],action_epoch('2026-11-07T08:00:00'))
        self.assertEqual(self.source.last_status['history_untrusted_time'],1)
        self.assertTrue(self.source.last_status['ok'])
        # This is the real pilot failure boundary: a complete native capture
        # must remain admissible by the durable projector without future dates.
        import sqlite3
        from tracking_projection import NativeProjection
        with sqlite3.connect(':memory:') as db:
            emitted=NativeProjection.update(db,result,action_epoch(STAMP),recovery_ids=['2:11:1'])
        self.assertEqual(len(emitted),1)
        self.assertEqual(action_epoch(emitted[0]['timestamp']),action_epoch(STAMP))
    def test_native_history_equal_to_observation_time_is_allowed(self):
        self.up.rows[11]=[episode(Listenduration=17)]
        self.up.history=[history(self.up.rows[11][0],STAMP)]
        self.source=NativeTrackingSource(self.cfg,request=self.up,clock=lambda:action_epoch(STAMP))
        self.assertEqual(self.capture()[0]['updated_at'],action_epoch(STAMP))
        self.assertEqual(self.source.last_status['history_untrusted_time'],0)
    def test_history_timestamp_requires_matching_identity_and_state(self):
        row=episode(Listenduration=137,Completed=True);self.up.rows[11]=[row]
        for change in ({'episodeurl':'https://old.test/episode'},{'completed':False},{'listenduration':138}):
            self.up.history=[dict(history(row),**change)]
            with self.subTest(change=change):
                self.assertNotIn('updated_at',self.capture()[0]);self.assertEqual(self.source.last_status['history_changed'],1)
    def test_history_paging_boundary_never_filters_state_inventory(self):
        self.up.rows[11]=[episode(i,Listenduration=i) for i in range(1,202)]
        self.up.history=[history(row) for row in self.up.rows[11]]
        self.assertEqual(len(self.capture()),201)
        self.assertEqual([kw['params']['offset'] for _,path,kw in self.up.calls if '/user_history/' in path],[0,200])
        self.up.calls.clear();self.source.history_since=action_epoch('2026-11-08T00:00:00');rows=self.capture()
        self.assertEqual(len(rows),201);self.assertTrue(all('updated_at' not in row for row in rows));self.assertTrue(self.source.last_status['history_boundary_reached'])
        self.assertEqual([kw['params']['offset'] for _,path,kw in self.up.calls if '/user_history/' in path],[0])
    def test_history_outage_or_unstable_order_fails_whole_capture(self):
        def fail(method,path,kw):
            if '/user_history/' in path:return response({},503)
        self.up.hook=fail
        with self.assertRaisesRegex(TrackingSourceError,'^native_api_unavailable$'):self.capture()
        self.up.hook=None;self.up.rows[11]=[episode(1),episode(2)]
        self.up.history=[history(episode(1),'2026-11-06T00:00:00'),history(episode(2),STAMP)]
        with self.assertRaisesRegex(TrackingSourceError,'^native_history_order_changed$'):self.capture()
    def test_config_and_capture_limits_enforced(self):
        for cfg in ({'server_version':'0.9.1'},{'max_rows':True},{'max_feeds':10001},{'history_since':-1}):
            with self.subTest(cfg=cfg),self.assertRaises(TrackingSourceError):NativeTrackingSource(dict(self.cfg,**cfg),request=self.up)
        self.source=NativeTrackingSource(dict(self.cfg,max_pages=1),request=self.up)
        with self.assertRaisesRegex(TrackingSourceError,'^native_capture_page_limit$'):self.capture()
        self.source=NativeTrackingSource(dict(self.cfg,max_rows=1),request=self.up);self.up.rows[11]=[episode(1),episode(2)]
        with self.assertRaises(TrackingSourceError):self.capture()
    def test_bad_key_and_redirect_do_not_leak_response(self):
        self.key.write_text('')
        with self.assertRaisesRegex(TrackingSourceError,'^native_key_unavailable$'):self.capture()
        self.key.write_text('PRIVATE_KEY');self.up.hook=lambda *_:response({'secret':'PRIVATE_BODY'},302)
        with self.assertRaisesRegex(TrackingSourceError,'^native_api_unavailable$'):self.capture()

if __name__=='__main__':unittest.main()
