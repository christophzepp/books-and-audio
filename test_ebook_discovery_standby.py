"""Real EPUBs, durable folder witnesses, source changes and discovery retries."""
import copy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from ebook_discovery import refresh
from folder_manager import FolderManager
from test_ebook_discovery import DiscoveryTests


class DiscoveryStandbyTests(unittest.TestCase):
    def setUp(self):
        self.f=DiscoveryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.path=self.f.path.resolve();self.f.m['local_path']=str(self.f.path)
        self.media=self.f.path.parent;self.f.spec['local_root']=str(self.media)
        self.state=self.media/'nvme-state';self.state.mkdir(mode=0o700)
        self.now=1000;self.mount='mount-v1';self.available=True
        self.store=self.f.store;self.events=[];self.queries=0
        self.f.files[0]['updatedAt']='file-version-1'
        self.f.bo._username='test-user';self.f.bo.user={'id':7,'username':'test-user'}
        original_query=self.f.bo.query
        def query():self.queries+=1;return original_query()
        self.f.bo.query=query
        config=dict(bo_url='http://bookorbit.test',jellyfin=dict(auto_ebooks=self.f.spec,ebooks=[],
            url='http://jellyfin.test',user_id='user-1',username='test-user',version='10.11.0'),
            folder_mappings=[dict(id='text',kind='ebooks',name='E-Books',active=True,
                paths=dict(worker=str(self.media),bo='/books',jf='/data/ebooks'))])
        self.b=SimpleNamespace(root=self.state,config=config,bo=self.f.bo,jellyfin=self.f.discovery_jf,
            clock=lambda:self.now,guard=lambda:None,load=lambda key:copy.deepcopy(self.store.get(key,{})),
            save=lambda key,value:self.store.update({key:copy.deepcopy(value)}),
            event=lambda *args:self.events.append(args),alert=lambda *args:self.events.append(args))
        self.new_folders()
        self.b.media_guard=lambda kind:self.folders.require(kind)
        self.b.media_observation_guard=self.logical_guard
        self.b.media_observation_generation=lambda kind:self.folders.observation_generation(kind)

    def new_folders(self):
        self.folders=FolderManager(self.state,self.b.config,[dict(worker=str(self.media))],
            mount_checker=lambda path:self.available,generation_reader=lambda:self.mount,clock=lambda:self.now)
        self.folders.seed()

    def logical_guard(self,kind):
        if not self.available:raise ValueError('current mount receipt unavailable')
        return self.folders.require_observation(kind)

    def embedded_audio(self):
        with zipfile.ZipFile(self.f.path,'a') as archive:archive.writestr('overlay.smil','<smil/>')
        self.f.files[0]['sizeBytes']=self.f.path.stat().st_size

    def reject(self):
        self.embedded_audio();self.assertEqual(refresh(self.b,True)[1]['waiting'],1)
        self.now+=301;_,report=refresh(self.b,True)
        self.assertEqual(report['blocked'][0]['code'],'epub_content_rejected')
        return copy.deepcopy(self.store['_ebook_discovery']['observations']['1'])

    @contextmanager
    def no_media(self):
        real_stat=Path.stat;real_lstat=Path.lstat
        def denied(path,*args,**kwargs):
            if path==self.media or path==self.f.path:raise AssertionError('media stat')
            return real_stat(path,*args,**kwargs)
        def denied_lstat(path,*args,**kwargs):
            if path==self.media or path==self.f.path:raise AssertionError('media lstat')
            return real_lstat(path,*args,**kwargs)
        with patch.object(Path,'resolve',side_effect=AssertionError('media resolve')), \
             patch.object(Path,'stat',denied),patch.object(Path,'lstat',denied_lstat), \
             patch.object(self.folders,'check',side_effect=AssertionError('physical folder check')), \
             patch('ebook_discovery.inspect_epub',side_effect=AssertionError('EPUB content read')):
            yield

    def test_rejected_content_survives_year_and_recreated_folder_manager_without_media(self):
        proof=self.reject();self.new_folders();self.now+=365*86400
        self.f.bo.user=None;query=self.f.bo.query
        def authenticate():
            result=query();self.f.bo.user={'id':7,'username':'test-user'};return result
        self.f.bo.query=authenticate
        with self.no_media():
            additions,report=refresh(self.b,True)
        self.assertEqual(additions,[]);self.assertEqual(report['cached_rejections'],1)
        self.assertEqual(report['blocked'][0]['retry_mode'],'manual_or_source_change')
        self.assertEqual(self.store['_ebook_discovery']['observations']['1'],proof)

    def test_changed_api_file_version_revalidates_only_changed_candidate(self):
        self.reject()
        copy_path=self.media/'other.epub';copy_path.write_bytes(self.f.path.read_bytes())
        first=copy.deepcopy(self.f.files[0]);second=dict(first,id=2,absolutePath='/books/other.epub')
        self.f.cards.append(dict(id=2,libraryId=2))
        original=self.f.bo.detail
        self.f.bo.detail=lambda ident:dict(original(ident),files=[copy.deepcopy(first if ident==1 else second)])
        self.f.jitems.append(dict(Id='b'*32,Type='Book',Path='/data/ebooks/other.epub'))
        self.now+=301;refresh(self.b,True)
        self.now+=301;refresh(self.b,True)
        untouched=copy.deepcopy(self.store['_ebook_discovery']['observations']['1'])
        second['updatedAt']='file-version-2';self.now+=301
        from ebook_discovery import inspect_epub
        with patch('ebook_discovery.inspect_epub',wraps=inspect_epub) as inspected:
            _,report=refresh(self.b,True)
        self.assertEqual([call.args[0].name for call in inspected.call_args_list],['other.epub'])
        self.assertEqual(report['cached_rejections'],1)
        self.assertEqual(self.store['_ebook_discovery']['observations']['1'],untouched)

    def test_targeted_retry_bypasses_rejection_without_touching_other_candidate(self):
        self.reject();self.now+=1
        from ebook_discovery import inspect_epub
        with patch('ebook_discovery.inspect_epub',wraps=inspect_epub) as inspected:
            _,report=refresh(self.b,True,retry_file_ids=[1])
        self.assertEqual(inspected.call_count,1)
        self.assertEqual(report['cached_rejections'],0)
        self.assertEqual(report['unmatched_retry_file_ids'],[])

    def test_targeted_retry_keeps_other_open_rejections_visible_in_gui_report(self):
        self.reject()
        path=self.media/'other.epub';path.write_bytes(self.f.path.read_bytes())
        first=copy.deepcopy(self.f.files[0]);second=dict(first,id=2,absolutePath='/books/other.epub')
        self.f.cards.append(dict(id=2,libraryId=2));original=self.f.bo.detail
        self.f.bo.detail=lambda ident:dict(original(ident),files=[copy.deepcopy(first if ident==1 else second)])
        self.f.jitems.append(dict(Id='b'*32,Type='Book',Path='/data/ebooks/other.epub'))
        self.now+=301;refresh(self.b,True);self.now+=301;refresh(self.b,True)
        untouched=copy.deepcopy(self.store['_ebook_discovery']['observations']['1'])
        from ebook_discovery import inspect_epub
        with patch('ebook_discovery.inspect_epub',wraps=inspect_epub) as inspected:
            _,report=refresh(self.b,True,force=True,retry_file_ids=[2])
        self.assertEqual([call.args[0].name for call in inspected.call_args_list],['other.epub'])
        self.assertEqual({row['file_id'] for row in report['blocked']},{1,2})
        self.assertEqual(self.store['_ebook_discovery']['report']['blocked'],report['blocked'])
        self.assertEqual(self.store['_ebook_discovery']['observations']['1'],untouched)

    def test_missing_generation_never_reuses_or_creates_permanent_rejection(self):
        self.reject();self.b.media_observation_generation=lambda kind:None;self.now+=301
        _,report=refresh(self.b,True)
        self.assertEqual(report['cached_rejections'],0)
        self.assertEqual(self.store['_ebook_discovery']['observations']['1']['outcome'],'retry')

    def test_missing_fresh_mount_authority_blocks_before_cached_negative(self):
        self.reject();self.available=False;self.now+=301
        with self.no_media():
            additions,report=refresh(self.b,True)
        self.assertEqual(additions,[]);self.assertEqual(report['cached_rejections'],0)
        self.assertEqual(report['blocked'][0]['code'],'inventory_unavailable')

    def test_account_or_folder_change_invalidates_only_scoped_import_proof(self):
        self.reject();self.now+=301;self.f.bo.user['id']=8
        from ebook_discovery import inspect_epub
        with patch('ebook_discovery.inspect_epub',wraps=inspect_epub) as inspected:refresh(self.b,True)
        self.assertEqual(inspected.call_count,1)
        self.now+=301;self.b.config['folder_mappings'].append(dict(id='pod',kind='podcasts',active=False,paths={'worker':'/podcasts'}))
        with self.no_media():_,report=refresh(self.b,True)
        self.assertEqual(report['cached_rejections'],1)

    def test_personal_jellyfin_progress_and_etag_do_not_invalidate_source_rejection(self):
        self.reject();self.now+=301
        self.f.jitems[0].update(UserData={'Played':True,'PlayCount':2},Etag='new-user-state')
        with self.no_media():_,report=refresh(self.b,True)
        self.assertEqual(report['cached_rejections'],1)

    def test_outage_and_backoff_keep_last_known_file_errors_visible(self):
        self.reject();self.now+=301
        self.f.bo.query=lambda:(_ for _ in ()).throw(RuntimeError('network unavailable'))
        _,report=refresh(self.b,True)
        self.assertTrue(any(row['file_id']==1 for row in report['blocked']))
        self.now+=10;_,report=refresh(self.b,True)
        self.assertFalse(report['scanned'])
        self.assertTrue(any(row['file_id']==1 for row in report['blocked']))

    def test_backoff_is_capped_and_survives_recreated_refresh_bridge(self):
        self.f.path.rename(self.media/'unavailable.epub')
        delays=[]
        for _ in range(12):
            _,report=refresh(self.b,True)
            retry=report['blocked'][0]['retry_at'];delays.append(retry-self.now)
            self.b=SimpleNamespace(**vars(self.b));self.now=retry
        self.assertEqual(delays[:3],[300,600,1200])
        self.assertLessEqual(max(delays),86400);self.assertEqual(delays[-1],86400)

    def test_global_network_backoff_preserves_observations_and_existing_records(self):
        refresh(self.b,True);observed=copy.deepcopy(self.store['_ebook_discovery']['observations'])
        self.store['unrelated-progress']={'heard':True,'manual_approval':'keep'}
        original=self.f.bo.query
        self.f.bo.query=lambda:(_ for _ in ()).throw(RuntimeError('network unavailable'))
        self.now+=301;_,report=refresh(self.b,True)
        self.assertEqual(self.store['_ebook_discovery']['observations'],observed)
        retry=report['blocked'][0]['retry_at'];self.assertEqual(retry,self.now+300)
        self.now+=10
        self.f.bo.query=lambda:(_ for _ in ()).throw(AssertionError('too early network retry'))
        _,report=refresh(self.b,True);self.assertFalse(report['scanned'])
        self.f.bo.query=original;self.now=retry
        additions,_=refresh(self.b,True)
        self.assertEqual(len(additions),1)
        self.assertEqual(self.store['unrelated-progress'],{'heard':True,'manual_approval':'keep'})

    def test_transient_file_unavailability_backoff_doubles_and_force_bypasses_it(self):
        missing=self.media/'hidden.epub';self.f.path.rename(missing)
        _,report=refresh(self.b,True)
        self.assertEqual(report['blocked'][0]['retry_at'],1300)
        self.now=1300;_,report=refresh(self.b,True)
        self.assertEqual(report['blocked'][0]['retry_at'],1900)
        self.now=1601
        with self.no_media():_,report=refresh(self.b,True)
        self.assertEqual(report['retry_deferred'],1)
        self.f.path=missing;self.now=1602
        _,report=refresh(self.b,True,retry_file_ids=[1])
        self.assertEqual(report['blocked'][0]['attempts'],3)
        self.assertEqual(report['blocked'][0]['retry_at'],2802)

    def test_partial_book_detail_outage_preserves_other_stability_history(self):
        self.f.cards.append(dict(id=2,libraryId=2));original=self.f.bo.detail;unavailable=[False]
        def detail(ident):
            if ident==1:return original(ident)
            if unavailable[0]:raise RuntimeError('book2 unavailable')
            return dict(original(ident),files=[dict(id=2,format='pdf',absolutePath='/books/other.pdf',sizeBytes=100)])
        self.f.bo.detail=detail
        refresh(self.b,True);first=copy.deepcopy(self.store['_ebook_discovery']['observations']['1'])
        unavailable[0]=True;self.now+=301
        with self.no_media():additions,report=refresh(self.b,True)
        self.assertEqual(additions,[])
        self.assertEqual(self.store['_ebook_discovery']['observations']['1'],first)
        self.assertEqual(first['since'],1000)
        self.assertTrue(any(row['book_id']==2 for row in report['blocked']))
        self.now+=301;unavailable[0]=False
        additions,_=refresh(self.b,True)
        self.assertEqual(len(additions),1)

    def test_targeted_retry_cannot_hide_global_duplicate_path(self):
        first=copy.deepcopy(self.f.files[0]);second=dict(first,id=2)
        self.f.cards.append(dict(id=2,libraryId=2));original=self.f.bo.detail
        self.f.bo.detail=lambda ident:dict(original(ident),files=[copy.deepcopy(first if ident==1 else second)])
        with self.no_media():
            additions,report=refresh(self.b,True)
            self.assertEqual(additions,[]);self.assertEqual(len(report['blocked']),2)
            for _ in range(2):
                self.now+=301
                additions,report=refresh(self.b,True,force=True,retry_file_ids=[1])
                self.assertEqual(additions,[])
                self.assertTrue(any('Ambiguous BookOrbit' in row['reason'] for row in report['blocked']))
        self.assertEqual(self.store['_ebook_discovery']['mappings'],[])

    def test_targeted_retry_leaves_foreign_book_backoff_unchanged(self):
        self.f.cards.append(dict(id=2,libraryId=2));original=self.f.bo.detail;calls=[]
        def detail(ident):
            calls.append(ident)
            if ident==2:raise RuntimeError('book2 unavailable')
            return original(ident)
        self.f.bo.detail=detail;refresh(self.b,True)
        old=copy.deepcopy(self.store['_ebook_discovery']['observations']['book:2'])
        calls.clear();self.now+=1
        refresh(self.b,True,force=True,retry_file_ids=[1])
        self.assertNotIn(2,calls)
        self.assertEqual(self.store['_ebook_discovery']['observations']['book:2'],old)

    def test_mapped_ebooks_are_never_physically_scanned_or_retried(self):
        self.b.config['jellyfin']['ebooks']=[copy.deepcopy(self.f.m)]
        with self.no_media():
            additions,report=refresh(self.b,True,force=True,retry_file_ids=[1])
        self.assertEqual(additions,[self.f.m]);self.assertEqual(report['candidates'],0)
        self.assertEqual(report['unmatched_retry_file_ids'],[])

    def test_source_changed_during_content_validation_never_gets_permanent_verdict(self):
        self.embedded_audio();refresh(self.b,True);self.now+=301
        from ebook_discovery import inspect_epub
        def changed(path,**kwargs):
            self.f.files[0]['updatedAt']='new-source-version'
            return inspect_epub(path,**kwargs)
        with patch('ebook_discovery.inspect_epub',side_effect=changed):_,report=refresh(self.b,True)
        self.assertEqual(self.store['_ebook_discovery']['observations']['1']['outcome'],'retry')
        self.assertEqual(report['blocked'][0]['retry_mode'],'backoff')

    def test_jellyfin_source_changed_during_positive_admission_is_not_registered(self):
        refresh(self.b,True);self.now+=301
        from ebook_discovery import inspect_epub
        def changed(path,**kwargs):
            index=inspect_epub(path,**kwargs)
            self.f.jitems[0]['Path']='/data/ebooks/replaced.epub'
            return index
        with patch('ebook_discovery.inspect_epub',side_effect=changed):additions,report=refresh(self.b,True)
        self.assertEqual(additions,[])
        self.assertIn('Jellyfin ebook source changed',report['blocked'][0]['reason'])

    def test_invalid_target_ids_are_rejected_without_state_change(self):
        for ids in ([],[0],[True],['x'],{'id':1}):
            with self.subTest(ids=ids),self.assertRaises(ValueError):refresh(self.b,True,retry_file_ids=ids)
        self.assertEqual(self.store,{})


if __name__=='__main__':unittest.main()
