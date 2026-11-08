"""Large cycle snapshots survive several writes without stale target reads."""
from collections import Counter
import copy
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

from jellyfin_client import JellyfinClient
from observation_snapshot import ObservationBatch
from podcast_sync import PodcastCoordinator, EpisodeABS
import test_podcast_bulk_io as fixtures


class ObservationScalingTests(unittest.TestCase):
    pine=fixtures.BulkAPI.pine
    abs=fixtures.BulkAPI.abs

    def setup_clients(self,n=1200):
        self.n=n;self.abs_client,self.item,self.progress,self.abs_calls=self.abs(n)
        base_request=self.abs_client._request.side_effect
        def abs_request(method,path,label,**kwargs):
            if path=='/status':
                self.abs_calls[path]+=1;return {'app':'audiobookshelf','serverVersion':'2.37.1'}
            if path.startswith('/api/me/progress/show/ep'):
                self.abs_calls[path]+=1;row=self.progress[int(path.rsplit('ep',1)[1])]
                if method=='PATCH':
                    row.update(kwargs['payload']);return None
                return copy.deepcopy(row)
            return base_request(method,path,label,**kwargs)
        self.abs_client._request=Mock(side_effect=abs_request)
        self.pine_client,self.pine_rows,self.pine_calls,self.history=self.pine(n)
        self.jf=JellyfinClient('https://jf.example','private','a'*32,'reader','1.0')
        self.addCleanup(self.jf.s.close)
        self.jf_rows=[dict(Id=format(i,'032x'),Type='AudioBook',Path='/podcasts/'+str(i)+'.mp3',
            UserData=dict(Played=False,PlayCount=1,PlaybackPositionTicks=17*10000000)) for i in range(1,n+1)]
        self.jf_by_id={row['Id']:row for row in self.jf_rows};self.jf_calls=Counter()
        self.jf_version='1.0';self.jf_username='reader'
        def jf_request(method,path,params=None,body=None):
            self.jf_calls[path]+=1
            if path=='/System/Info/Public':return {'Version':self.jf_version}
            if path=='/Users/'+'a'*32:return dict(Id='a'*32,Name=self.jf_username,Policy={'IsDisabled':False})
            if path=='/Items':
                selected=self.jf_rows
                if params.get('ids'):selected=[self.jf_by_id[ident] for ident in params['ids'].split(',')]
                return dict(Items=copy.deepcopy(selected),TotalRecordCount=len(selected))
            if path.startswith('/UserItems/'):
                ident=path.split('/')[2];self.jf_by_id[ident]['UserData'].update(body);return None
            raise AssertionError('unexpected JF route')
        self.jf.request=Mock(side_effect=jf_request)
        self.bridge=types.SimpleNamespace(abs=self.abs_client,jellyfin=self.jf,pinepods=self.pine_client)
        self.coordinator=PodcastCoordinator.__new__(PodcastCoordinator)
        c=self.coordinator;c.b=self.bridge;c.abs=EpisodeABS(self.abs_client);c.jf=self.jf;c.pine=self.pine_client
        c.feed_resolver=None;c.observation=ObservationBatch(types.SimpleNamespace())
        return c

    def mapping(self,i):
        return dict(id='_podcast:show:ep'+str(i),podcast_id='show',episode_id='ep'+str(i),duration=600,
            jf={'id':format(i+1,'032x')},feed_url=fixtures.FEED,enclosure_url=fixtures.URL+str(i+1))

    def read_one(self,i):
        m=self.mapping(i)
        with self.coordinator.observing():
            source=self.bridge.abs.episode_item('show','ep'+str(i))
            raw=self.coordinator.abs.read(m)
            jf=self.coordinator.jf.items(ids=[m['jf']['id']],types='AudioBook')[0]
            pine=self.coordinator.pine.read_variant_status(fixtures.FEED,m['enclosure_url'],600,
                expected_episode_id=i+1,expected_user_id=2)
        return source,raw,jf,pine

    def prime(self):
        with self.coordinator.observing():
            self.coordinator.jf.items(parent='library',types='AudioBook')
        for i in range(self.n):self.read_one(i)

    def no_disk(self):
        from contextlib import ExitStack
        stack=ExitStack()
        for name in ('resolve','stat','open'):
            stack.enter_context(patch.object(Path,name,side_effect=AssertionError('API observation touched disk')))
        return stack

    def test_1200_episode_cycle_with_five_changes_retains_catalogs_and_reads_changed_targets_live(self):
        c=self.setup_clients();changed=(5,204,603,907,1199)
        with self.no_disk():self.prime()
        snapshots=dict(c.observation._clients)
        self.assertEqual(self.abs_calls['/api/items/show'],1)
        self.assertEqual(self.abs_calls['/api/me/progress'],1)
        self.assertEqual(self.jf_calls['/Items'],1)
        for i in changed:
            m=self.mapping(i)
            with self.no_disk():
                c.mutate(c.abs.position,m,30+i%200,_mapping=m,_services=('abs',))
                c.mutate(self.jf.update,m['jf']['id'],{'PlaybackPositionTicks':(30+i%200)*10000000},
                    _mapping=m,_services=('jf',))
                def pine_write():
                    self.pine_rows[i]['Listenduration']=30+i%200
                    self.history[i]['position']=30+i%200
                c.mutate(pine_write,_mapping=m,_services=('pine',))
                # Original clients remain fresh even while snapshots retain
                # the old catalog. Never use an indexed projection for CAS.
                self.assertEqual(c.abs.read(m)['currentTime'],30+i%200)
                source,raw,jf,pine=self.read_one(i)
                self.assertEqual(raw['currentTime'],30+i%200)
                self.assertEqual(jf['UserData']['PlaybackPositionTicks'],(30+i%200)*10000000)
                self.assertEqual(pine['native_position'],30+i%200)
                self.assertEqual(pine['gpodder_raw_position'],30+i%200)
        with self.no_disk():
            for i in range(self.n):self.read_one(i)
        self.assertEqual(c.observation._clients,snapshots)
        self.assertEqual(self.abs_calls['/api/items/show'],1)
        self.assertEqual(self.abs_calls['/api/me/progress'],1)
        self.assertEqual(self.jf_calls['/Items'],1+len(changed))
        self.assertEqual(self.pine_calls['/api/data/get_episode_metadata'],1+len(changed))
        self.assertEqual(self.pine_calls['/api/2/episodes/reader.json'],1+len(changed))
        # Fresh changed-episode ownership checks deliberately retain the
        # original client's complete six-page identity validation.
        self.assertEqual(self.pine_calls['/api/data/podcast_episodes'],6*(1+len(changed)))
        self.assertEqual(sum(self.abs_calls[path] for path in self.abs_calls if '/progress/show/' in path),
                         3*len(changed))  # one PATCH, original readback, indexed-target refresh

    def test_live_authorization_unwraps_nested_scope_without_evicting_catalogs(self):
        c=self.setup_clients(1001)
        with self.no_disk():self.prime()
        snapshots=dict(c.observation._clients)
        checks=[]
        def live_proof(mapping,**kwargs):
            self.assertIs(c.b.abs,self.abs_client);self.assertIs(c.abs.client,self.abs_client)
            self.assertIs(c.pine,self.pine_client);self.assertIs(c.jf,self.jf)
            self.assertEqual(c.b.abs.version(),'2.37.1');c.jf.guard()
            native=c.pine.inspect_episode_metadata(mapping['feed_url'],mapping['enclosure_url'],600)
            checks.append(native['position']);return {'ready':True,'checked_position':native['position']}
        c._variant_proof=Mock(side_effect=live_proof)
        with self.no_disk():
            for i in (2,220,999):
                self.pine_rows[i]['Listenduration']=31+i%400
                with c.observing():
                    cached=c.pine
                    result=c.variant_proof(self.mapping(i),authorize=True)
                    self.assertIs(c.pine,cached)
                self.assertEqual(result['checked_position'],31+i%400)
        self.assertEqual(checks,[33,251,230])
        self.assertEqual(c.observation._clients,snapshots)
        self.assertEqual(self.abs_calls['/api/items/show'],1)
        self.assertEqual(self.abs_calls['/api/me/progress'],1)
        self.assertEqual(self.jf_calls['/Items'],1)
        self.assertEqual(self.abs_calls['/status'],3)
        self.assertEqual(self.jf_calls['/System/Info/Public'],3)

    def test_lost_acknowledgement_invalidates_only_affected_progress_and_never_reuses_old_projection(self):
        c=self.setup_clients(1001)
        with self.no_disk():self.prime()
        snapshots=dict(c.observation._clients);m=self.mapping(77)
        def lost():
            self.pine_rows[77]['Completed']=True
            self.pine_rows[77]['Listenduration']=600
            self.history[77]['position']=600
            raise RuntimeError('lost reply')
        with self.no_disk():
            with self.assertRaisesRegex(RuntimeError,'lost reply'):
                c.mutate(lost,_mapping=m,_services=('pine',))
            _,_,_,fresh=self.read_one(77)
            self.assertEqual(fresh['status'],'finished')
            self.assertEqual(fresh['gpodder_status'],'finished')
            for i in (0,1000):self.assertEqual(self.read_one(i)[3]['native_position'],17)
        self.assertEqual(c.observation._clients,snapshots)
        self.assertEqual(self.abs_calls['/api/me/progress'],1)
        self.assertEqual(self.pine_calls['/api/data/get_episode_metadata'],2)

    def test_live_authorization_checks_new_server_version_and_account_despite_cached_guards(self):
        c=self.setup_clients(1001)
        with c.observing():c.jf.guard()
        snapshots=dict(c.observation._clients)
        c._variant_proof=lambda mapping,**kwargs:c.jf.guard()
        for field,new in (('jf_version','2.0'),('jf_username','another-user')):
            with self.subTest(field=field):
                self.jf_version='1.0';self.jf_username='reader'
                setattr(self,field,new)
                with c.observing():
                    observed=c.jf
                    with self.assertRaisesRegex(RuntimeError,'Jellyfin .*changed'):
                        c.variant_proof(self.mapping(0),authorize=True)
                    self.assertIs(c.jf,observed)
        self.assertEqual(c.observation._clients,snapshots)
        self.assertEqual(self.jf_calls['/System/Info/Public'],3)

    def test_explicit_feed_action_reader_sees_own_write_without_losing_other_feed_indexes(self):
        c=self.setup_clients(1001)
        with self.no_disk():self.prime()
        m=self.mapping(1)
        with c.observing():self.assertEqual(c.pine.actions(fixtures.FEED)['actions'][1]['position'],17)
        c.mutate(lambda:self.history[1].update(position=600),_mapping=m,_services=('pine',))
        with c.observing():
            self.assertEqual(c.pine.actions(fixtures.FEED)['actions'][1]['position'],600)
        self.assertEqual(self.abs_calls['/api/me/progress'],1)
        self.assertEqual(self.pine_calls['/api/2/episodes/reader.json'],2)

    def test_indexed_items_copy_only_requested_episode_and_never_expose_mutable_cache(self):
        c=self.setup_clients(1200);full_copies=[];original=copy.deepcopy
        def counted(value,*args,**kwargs):
            if isinstance(value,dict) and len(value.get('media',{}).get('episodes',[]))==1200:full_copies.append(True)
            return original(value,*args,**kwargs)
        with patch('observation_snapshot.copy.deepcopy',side_effect=counted),self.no_disk():
            for i in range(1200):
                with c.observing():selected=c.b.abs.episode_item('show','ep'+str(i))
        self.assertEqual(len(full_copies),1)  # only the synthetic HTTP response
        selected['media']['episodes'][0]['guid']='caller edit'
        with c.observing():
            self.assertEqual(c.b.abs.episode_item('show','ep1199')['media']['episodes'][0]['guid'],'guid1199')
        self.assertEqual(self.abs_calls['/api/items/show'],1)


if __name__=='__main__':unittest.main()
