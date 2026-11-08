"""Bounded, read-only cycle snapshots with real clients and synthetic APIs."""
import copy
from collections import Counter
import math
import types
import unittest
from unittest.mock import Mock, patch

from abs_client import ABSClient, RequestError as ABSError
from bo_client import BOClient
from jellyfin_client import JellyfinClient
from observation_snapshot import ABSObservation, PineObservation, ObservationBatch
from pinepods_client import PinepodsClient, RequestError, EpisodeUnavailable
from podcast_sync import EpisodeABS, raw_equal
import test_podcast_sync as core_fixture
import test_podcast_status_sync as status_fixture

FEED='https://feed.example/show.xml'
URL='https://media.example/episode-'


def catalog_row(i, **changes):
    row=dict(podcastname='Example',Episodetitle='Episode '+str(i),Episodepubdate='2026-11-01T00:00:00',
        Episodedescription='Description',Episodeartwork='https://images.example/episode.jpg',
        Episodeurl=URL+str(i),Episodeduration=600,Listenduration=17,Episodeid=i,
        Completed=False,saved=False,queued=False,downloaded=False,is_youtube=False,is_video=False)
    row.update(changes);return row


def pod():
    return dict(podcastid=11,podcastname='Example',artworkurl='https://images.example/show.jpg',
        feedurl=FEED,podcastindexid=39)


def metadata(row):
    out={k.lower():copy.deepcopy(v) for k,v in row.items() if k not in ('saved','queued','downloaded','is_video','podcastname')}
    out.update(pod())
    out.update(is_saved=row['saved'],is_queued=row['queued'],is_downloaded=row['downloaded'])
    return out


class BulkAPI(unittest.TestCase):
    def pine(self, n=1000):
        client=PinepodsClient('https://pinepods.example','reader','private',api_key='private',server_version='0.9.0')
        self.addCleanup(client.close)
        client._native_user_id=2
        rows=[catalog_row(i) for i in range(1,n+1)]
        by_id={row['Episodeid']:row for row in rows}
        calls=Counter()
        history=[dict(podcast=FEED,episode=URL+str(i),action='play',position=17,started=0,total=600,
                      timestamp='2026-11-01T00:00:00',device='phone') for i in range(1,n+1)]
        def request(method,path,**kwargs):
            calls[path]+=1
            if path=='/api/data/get_user':return dict(status='success',retrieved_id=2)
            if path=='/api/data/user_details_id/2':return dict(UserID=2,Username='reader')
            if path=='/api/data/return_pods/2':return dict(pods=[pod()])
            if path=='/api/data/podcast_episodes':
                q=kwargs['params'];start=q['offset'];return dict(total=len(rows),episodes=copy.deepcopy(rows[start:start+q['limit']]))
            if path=='/api/data/get_episode_metadata':return dict(episode=metadata(by_id[kwargs['payload']['episode_id']]))
            if path.startswith('/api/2/episodes/'):return dict(timestamp=1793491200,actions=copy.deepcopy(history))
            raise AssertionError('Unexpected API route')
        client._request=Mock(side_effect=request)
        return client,rows,calls,history

    def abs(self,n=1000):
        client=ABSClient('https://abs.example','private');self.addCleanup(client.close)
        item=dict(id='show',mediaType='podcast',libraryId='podcasts',media=dict(metadata={'title':'Example'},
            episodes=[dict(id='ep'+str(i),guid='guid'+str(i)) for i in range(n)]))
        progress=[dict(libraryItemId='show',episodeId='ep'+str(i),currentTime=17,isFinished=False) for i in range(n)]
        calls=Counter()
        def request(method,path,label,**kwargs):
            calls[path]+=1
            if path=='/api/items/show':return copy.deepcopy(item)
            if path=='/api/me/progress':return dict(mediaProgress=copy.deepcopy(progress))
            raise AssertionError('Unexpected ABS route')
        client._request=Mock(side_effect=request)
        return client,item,progress,calls

    def test_one_thousand_abs_episode_sources_and_progress_use_two_requests(self):
        client,item,progress,calls=self.abs()
        observed=ABSObservation(client)
        for i in range(1000):
            selected=observed.episode_item('show','ep'+str(i))
            self.assertEqual(selected['media']['episodes'],[item['media']['episodes'][i]])
            self.assertEqual(EpisodeABS(observed).read(dict(podcast_id='show',episode_id='ep'+str(i))),progress[i])
        self.assertEqual(calls,Counter({'/api/items/show':1,'/api/me/progress':1}))
        self.assertEqual(client._request.call_count,2)
        selected['media']['episodes'][0]['guid']='edited-copy'
        self.assertNotEqual(observed.episode_item('show','ep999')['media']['episodes'][0]['guid'],'edited-copy')

    def test_one_thousand_pine_episodes_share_catalog_owner_and_action_history(self):
        client,rows,calls,history=self.pine()
        observed=PineObservation(client)
        for i in range(1,1001):
            proof=observed.inspect_episode_metadata(FEED,URL+str(i),600)
            self.assertEqual(proof['metadata'],metadata(rows[i-1]))
            got=observed.read_variant_status(FEED,URL+str(i),600,expected_episode_id=i,expected_user_id=2)
            self.assertEqual(got['native_position'],17)
            self.assertEqual(got['gpodder_raw_position'],17)
        self.assertEqual(calls['/api/data/get_user'],1)
        self.assertEqual(calls['/api/data/user_details_id/2'],1)
        self.assertEqual(calls['/api/data/return_pods/2'],1)
        self.assertEqual(calls['/api/data/podcast_episodes'],5)
        self.assertEqual(calls['/api/data/get_episode_metadata'],1)
        self.assertEqual(calls['/api/2/episodes/reader.json'],1)
        self.assertEqual(sum(calls.values()),10)
        self.assertEqual(client._native_ids,{})  # Snapshot identity cache is isolated.

    def test_new_cycle_sees_reopened_old_episode_without_a_gpodder_change(self):
        client,rows,calls,history=self.pine(2)
        rows[1]['Completed']=True;rows[1]['Listenduration']=600
        first=PineObservation(client).read_variant_status(FEED,URL+'2',600,expected_episode_id=2,expected_user_id=2)
        rows[1]['Completed']=False;rows[1]['Listenduration']=0
        second=PineObservation(client).read_variant_status(FEED,URL+'2',600,expected_episode_id=2,expected_user_id=2)
        self.assertEqual(first['status'],'finished');self.assertEqual(second['status'],'not_started')
        self.assertEqual(first['gpodder_revision'],second['gpodder_revision'])
        self.assertNotEqual(first['native_observed_revision'],second['native_observed_revision'])

    def test_fresh_read_after_observation_detects_concurrent_edit_before_write(self):
        client,rows,calls,history=self.pine(2)
        observed=PineObservation(client).read_variant_status(FEED,URL+'2',600,expected_episode_id=2,expected_user_id=2)
        rows[1]['Listenduration']=30
        fresh=client.read_variant_status(FEED,URL+'2',600,expected_episode_id=2,expected_user_id=2)
        self.assertFalse(raw_equal({'pinepods':observed},{'pinepods':fresh}))
        self.assertEqual(fresh['native_position'],30)
        self.assertEqual(calls['/api/data/get_episode_metadata'],2)

    def test_metadata_shape_mismatch_falls_back_to_individual_checked_reads(self):
        client,rows,calls,history=self.pine(2)
        original=client._request.side_effect
        def request(method,path,**kwargs):
            value=original(method,path,**kwargs)
            if path=='/api/data/get_episode_metadata':value['episode']['extra']='upstream-field'
            return value
        client._request.side_effect=request
        observed=PineObservation(client)
        for i in (1,2):self.assertEqual(observed.inspect_episode_metadata(FEED,URL+str(i),600)['metadata']['extra'],'upstream-field')
        self.assertEqual(calls['/api/data/get_episode_metadata'],2)

    def test_incomplete_catalog_fields_fall_back_without_approving_synthetic_values(self):
        client,rows,calls,history=self.pine(2)
        rows[1].pop('Episodedescription')
        observed=PineObservation(client)
        self.assertIn('episodedescription',observed.inspect_episode_metadata(FEED,URL+'1',600)['metadata'])
        self.assertNotIn('episodedescription',observed.inspect_episode_metadata(FEED,URL+'2',600)['metadata'])
        self.assertEqual(calls['/api/data/get_episode_metadata'],2)

    def test_duplicate_or_changing_pine_catalog_is_never_accepted(self):
        for kind in ('duplicate','changed-total','truncated'):
            client,rows,calls,history=self.pine(201)
            original=client._request.side_effect
            def request(method,path,**kwargs):
                value=original(method,path,**kwargs)
                if path=='/api/data/podcast_episodes':
                    if kind=='duplicate' and kwargs['params']['offset']==200:value['episodes'][0]=copy.deepcopy(rows[0])
                    if kind=='changed-total' and kwargs['params']['offset']==200:value['total']+=1
                    if kind=='truncated':value['episodes']=[]
                return value
            client._request.side_effect=request
            with self.subTest(kind=kind),self.assertRaises(RequestError):PineObservation(client).inspect_episode_metadata(FEED,URL+'1',600)
            self.assertEqual(calls['/api/data/get_episode_metadata'],0)

    def test_ambiguous_pine_url_and_empty_url_absence_keep_exact_identity_failures(self):
        client,rows,calls,history=self.pine(2);rows[1]['Episodeurl']=rows[0]['Episodeurl']
        with self.assertRaises(EpisodeUnavailable) as exc:PineObservation(client).inspect_episode_metadata(FEED,URL+'1',600)
        self.assertEqual(exc.exception.reason,'episode_ambiguous')
        rows[1]['Episodeurl']=''
        with self.assertRaises(EpisodeUnavailable) as exc:PineObservation(client).inspect_episode_metadata(FEED,URL+'9',600)
        self.assertEqual(exc.exception.reason,'episode_identity_unresolved')

    def test_duplicate_abs_progress_or_episode_id_fails_closed(self):
        client,item,progress,calls=self.abs(2);item['media']['episodes'][1]['id']='ep0'
        with self.assertRaises(ABSError):ABSObservation(client).episode_item('show','ep0')
        progress.append(copy.deepcopy(progress[0]))
        with self.assertRaises(ABSError):ABSObservation(client).episode_progress('show','ep0')

    def test_missing_abs_progress_remains_missing(self):
        client,item,progress,calls=self.abs(2);progress.pop()
        self.assertIsNone(ABSObservation(client).episode_progress('show','ep1'))

    def test_observation_never_sends_mutations_and_scope_restores_after_exception(self):
        client,item,progress,calls=self.abs(2);holder=types.SimpleNamespace(abs=client)
        batch=ObservationBatch()
        with self.assertRaisesRegex(RuntimeError,'cannot send mutations'):
            with batch.scope((holder,'abs')):
                self.assertIsNot(holder.abs,client)
                holder.abs.write_position('show',4,600)
        self.assertIs(holder.abs,client);self.assertEqual(sum(calls.values()),0)
        pine,rows,pcalls,history=self.pine(2)
        with self.assertRaisesRegex(RuntimeError,'cannot send mutations'):
            PineObservation(pine)._request('POST','/api/data/bulk_mark_episodes_completed',payload={})
        self.assertEqual(sum(pcalls.values()),0)

    def test_scopes_reuse_client_only_within_one_cycle_and_keep_fakes_unchanged(self):
        client,item,progress,calls=self.abs(2);holder=types.SimpleNamespace(abs=client)
        batch=ObservationBatch()
        with batch.scope((holder,'abs')):before=holder.abs.episode_item('show','ep0')
        item['media']['episodes'][0]['guid']='changed'
        with batch.scope((holder,'abs')):self.assertEqual(holder.abs.episode_item('show','ep0'),before)
        with ObservationBatch().scope((holder,'abs')):self.assertEqual(holder.abs.episode_item('show','ep0')['media']['episodes'][0]['guid'],'changed')
        fake=Mock();self.assertIs(ObservationBatch().client(fake),fake)

    def test_one_thousand_jellyfin_item_reads_reuse_validated_parent_inventory(self):
        client=JellyfinClient('https://jf.example','private','a'*32,'reader','1.0')
        self.addCleanup(client.s.close)
        rows=[dict(Id=format(i,'032x'),Type='AudioBook',Path='/podcasts/'+str(i)+'.mp3',
                   UserData=dict(Played=False,PlayCount=0,PlaybackPositionTicks=0)) for i in range(1,1001)]
        calls=[]
        def request(method,path,params=None,body=None):
            calls.append((method,path))
            selected=rows
            if params.get('ids'):selected=[row for row in rows if row['Id'] in params['ids'].split(',')]
            return dict(Items=copy.deepcopy(selected),TotalRecordCount=len(selected))
        client.request=Mock(side_effect=request)
        observed=ObservationBatch().client(client)
        self.assertEqual(len(observed.items(parent='library',types='AudioBook')),1000)
        for row in rows:
            self.assertEqual(observed.items(ids=[row['Id']],types='AudioBook'),[row])
        self.assertEqual(len(calls),1)
        rows[-1]['UserData']['Played']=True
        self.assertFalse(observed.items(ids=[rows[-1]['Id']],types='AudioBook')[0]['UserData']['Played'])
        self.assertTrue(client.items(ids=[rows[-1]['Id']],types='AudioBook')[0]['UserData']['Played'])
        self.assertEqual(len(calls),2)
        with self.assertRaises(ValueError):observed.items(ids=['invalid'],types='AudioBook')

    def test_snapshot_metrics_count_actual_requests_and_index_reuse(self):
        client,item,progress,calls=self.abs(2)
        owner=types.SimpleNamespace();batch=ObservationBatch(owner)
        observed=batch.client(client)
        for i in range(2):
            observed.episode_item('show','ep'+str(i));observed.episode_progress('show','ep'+str(i))
        self.assertEqual(owner._io_metrics['api_observation_requests'],2)
        self.assertEqual(owner._io_metrics['api_observation_index_hits'],4)

    def test_generic_bookorbit_and_jellyfin_facades_are_explicit_read_only(self):
        bo=BOClient('https://bo.example','reader','private');self.addCleanup(bo.close)
        jf=JellyfinClient('https://jf.example','private','a'*32,'reader','1.0');self.addCleanup(jf.s.close)
        bo._request=Mock(return_value={'value':1});jf.request=Mock(return_value={'value':1})
        batch=ObservationBatch();observed_bo=batch.client(bo);observed_jf=batch.client(jf)
        for _ in range(2):
            self.assertEqual(observed_bo._request('GET','/api/v1/books/1','book'),{'value':1})
            self.assertEqual(observed_jf.request('GET','/Items',{}),{'value':1})
        self.assertEqual(bo._request.call_count,1);self.assertEqual(jf.request.call_count,1)
        with self.assertRaises(RuntimeError):observed_bo._request('POST','/api/v1/books/1/status','status',payload={})
        with self.assertRaises(RuntimeError):observed_jf.request('POST','/UserItems/x/UserData',{}, {})
        bo._request('GET','/api/v1/books/1','book');self.assertEqual(bo._request.call_count,2)


class CoordinatorFixture(unittest.TestCase):
    setUp=core_fixture.Tests.setUp
    write_gate=core_fixture.Tests.write_gate
    load=core_fixture.Tests.load
    save=core_fixture.Tests.save
    write_record=core_fixture.Tests.write_record

    def coordinator(self):
        from podcast_sync import PodcastCoordinator
        c=PodcastCoordinator(self.b);c.observation=ObservationBatch()
        return c,c.mappings()[0][0]

    def prime(self,c):
        # An unrelated live-client observation represents the same shared
        # cycle's catalog/progress view. A subsequent scope must fetch anew.
        client=ABSClient('https://abs.example','private');self.addCleanup(client.close)
        client._request=Mock(return_value={'value':1})
        snapshot=c.observation.client(client)
        snapshot._request('GET','/example','example')
        return client,snapshot

    def assert_unrelated_preserved(self,c,client,old):
        fresh=c.observation.client(client)
        self.assertIs(fresh,old)
        fresh._request('GET','/example','example')
        self.assertEqual(client._request.call_count,1)


class MutationInvalidation(CoordinatorFixture):
    def test_new_admission_and_unchanged_enrollment_preserve_unrelated_snapshots(self):
        c,mapping=self.coordinator();client,old=self.prime(c)
        self.assertEqual(c.handle(mapping,True,0)[0],'unchanged')
        self.assert_unrelated_preserved(c,client,old)
        current=c.observation.client(client)
        for _ in range(10):self.assertEqual(c.handle(mapping,True,0)[0],'unchanged')
        self.assertIs(c.observation.client(client),current)
        self.assertEqual(client._request.call_count,1)

    def test_position_transfer_both_directions_preserves_unrelated_snapshot(self):
        c,mapping=self.coordinator();c.handle(mapping,True,0)
        for direction in ('jf','abs'):
            with self.subTest(direction=direction):
                if direction=='jf':self.abs.set(25)
                else:self.jf.set(70)
                client,old=self.prime(c)
                self.assertEqual(c.handle(mapping,True,0)[0],'synced')
                self.assert_unrelated_preserved(c,client,old)

    def test_lost_write_response_preserves_unrelated_snapshot_and_pending_journal(self):
        c,mapping=self.coordinator();c.handle(mapping,True,0)
        self.abs.set(25);self.jf.fail=True;client,old=self.prime(c)
        with self.assertRaisesRegex(RuntimeError,'Lost JF response'):c.handle(mapping,True,0)
        self.assert_unrelated_preserved(c,client,old)
        self.assertIn('pending',self.load(mapping['id']))

    def test_pending_core_initial_observation_uses_live_clients(self):
        c,mapping=self.coordinator();c.handle(mapping,True,0)
        stored=self.load(mapping['id']);stored['pending']={'stage':'prepared'};self.save(mapping['id'],stored)
        self.abs.set(35)
        with patch.object(c,'observing',side_effect=AssertionError('pending read entered snapshot')):
            states,raw=c.observe_read(mapping)
        self.assertEqual(states['abs']['position'],35)
        self.assertEqual(raw['abs']['currentTime'],35)

    def test_new_pine_admission_after_core_enrollment_uses_live_clients(self):
        c,mapping=self.coordinator();c.handle(mapping,True,0)
        self.cfg['podcasts']['pinepods_enabled']=True;self.b.pinepods=core_fixture.Pine()
        c,mapping=self.coordinator()
        with patch.object(c,'observing',side_effect=AssertionError('new Pine admission entered snapshot')):
            self.assertEqual(c.handle(mapping,True,0)[0],'unchanged')
        self.assertTrue(self.load(mapping['id'])['pine_enrolled'])

    def test_feed_route_resolver_cannot_retain_snapshot_into_live_guard(self):
        c,mapping=self.coordinator()
        pine=PinepodsClient('https://pine.example','reader','private',api_key='private',server_version='0.9.0')
        self.addCleanup(pine.close);c.pine=self.b.pinepods=pine
        resolvers=[]
        class Resolver:
            def __init__(self,b,client):self.pine=client;resolvers.append(self)
            def resolve(self,m):return m['feed_url']
        with patch('pine_feed_route.ReviewedFeeds',Resolver):
            with c.observing():
                c.pine_route(mapping);c.pine_route(mapping)
            self.assertEqual(len(resolvers),1)
            self.assertIs(resolvers[0].pine._observation_only,True)
            c.pine_route(mapping);c.pine_route(mapping)
        self.assertEqual(len(resolvers),3)
        self.assertIs(resolvers[1].pine,pine);self.assertIs(resolvers[2].pine,pine)


class StatusMutationInvalidation(CoordinatorFixture):
    setup_variant=status_fixture.StatusTests.setup_variant

    def test_status_admission_and_unchanged_rows_preserve_unrelated_snapshots(self):
        self.setup_variant();c,mapping=self.coordinator();client,old=self.prime(c)
        self.assertEqual(c.handle(mapping,True,0)[0],'unchanged')
        self.assert_unrelated_preserved(c,client,old)
        current=c.observation.client(client)
        for _ in range(3):self.assertEqual(c.handle(mapping,True,0)[0],'unchanged')
        self.assertIs(c.observation.client(client),current)

    def test_pending_status_initial_read_does_not_use_observation_scope(self):
        from podcast_status_sync import StatusCoordinator,state_key
        self.setup_variant();c,mapping=self.coordinator();c.handle(mapping,True,0)
        self.abs.set(200,True);c.handle(mapping,True,0)
        self.assertTrue(self.load(state_key(mapping['id'])).get('pending'))
        # Recovery must not reuse the snapshot even when the episode is already
        # enrolled and its last successful observation exists in this cycle.
        with patch.object(c,'observing',side_effect=AssertionError('pending status read entered snapshot')):
            StatusCoordinator(c).handle(mapping,True,0)

    def test_status_write_preserves_unrelated_snapshot_on_success_and_lost_acknowledgement(self):
        from podcast_status_sync import state_key
        self.setup_variant();c,mapping=self.coordinator();c.handle(mapping,True,0)
        self.pine.set(80,'finished');client,old=self.prime(c)
        self.assertEqual(c.handle(mapping,True,0)[0],'synced')
        self.assert_unrelated_preserved(c,client,old)
        self.pine.set(0,'not_started');self.abs.fail=True;client,old=self.prime(c)
        with self.assertRaisesRegex(RuntimeError,'Lost ABS response'):c.handle(mapping,True,0)
        self.assert_unrelated_preserved(c,client,old)
        self.assertTrue(self.load(state_key(mapping['id'])).get('pending'))


if __name__=='__main__':unittest.main()
