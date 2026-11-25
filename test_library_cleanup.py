import copy
import json
import unittest
from unittest.mock import Mock, patch

from admin_runtime import Administration, close_bridge
from library_cleanup import overlay
from sync import Bridge
from test_admin_runtime import Fixture


class MissingLibraryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.static = {'abs_id':'old','bo_id':5,'title':'Altes Hörbuch','fingerprint':'old'}
        self.kept = {'abs_id':'still','bo_id':7,'title':'Im Bestand','fingerprint':'keep'}
        self.auto = {'abs_id':'auto','bo_id':9,'title':'Neue Aufnahme','fingerprint':'auto'}
        self.ebook = {'bo_id':11,'file_id':50,'jf_id':'b'*32,'title':'EPUB'}
        self.config.update(mappings=[self.static,self.kept],native_readalong={'old':{'pin':1}},paused_mappings={'old':'Pausiert'})
        self.config['jellyfin'].update(ebooks=[self.ebook],auto_ebooks={'bo_library_id':2,'jf_library_id':'ebooks'})
        self.admin=Administration(self.root,self.key,self.config,self.credentials,Bridge)
        self.admin.identities={k:{'user_id':v} for k,v in [('bookorbit','1'),('audiobookshelf','as-user'),('jellyfin','a'*32)]}
        cfg, creds, _=self.admin.manager.effective()
        self.b=Bridge(self.root,cfg,creds)
        self.addCleanup(close_bridge,self.b)
        self.b.guard=Mock()
        self.b.abs=Mock()
        self.b.abs._request.return_value={'id':'as-user','username':'A'}
        self.b.abs.libraries.return_value=[{'id':'library','mediaType':'book'}]
        self.b.abs.inventory.return_value=[{'id':'still','libraryId':'library'}]
        self.b.bo=Mock()
        self.b.bo._request.return_value={'id':1,'username':'B'}
        self.b.bo.library.side_effect=lambda i:{'id':i}
        self.b.bo.query.return_value=[{'id':7,'libraryId':1}]
        self.b.jellyfin=Mock()
        self.b.jellyfin.user='a'*32
        self.b.jellyfin.username='J'
        self.b.jellyfin.request.return_value={'Items':[{'Id':'ebooks','Name':'E-Books'}]}
        self.b.jellyfin.items.return_value=[]
        self.b.db.execute('INSERT INTO auto_mappings VALUES(?,?,?)',('auto',9,json.dumps(self.auto)))
        self.b.db.commit()
        self.b.save('old',{'baseline':{'position':90}})
        self.b.save('_podcast:x',{'baseline':{'position':123}})
        self.b.save('still',{'baseline':{'position':200}})

    def command(self,kind,payload):
        ident=self.admin.store.submit(kind,payload)['id']
        self.admin.process_one(self.b)
        return self.admin.store.command(ident)

    def preview(self,kind='audio'):
        return self.command('library.preview_missing',{'media':kind})

    def apply(self,review,ids=None):
        return self.command('library.retire_missing',{'review_id':review['review_id'],
            'selected':ids if ids is not None else [r['id'] for r in review['rows']],'confirm':True})

    def test_exact_selection_static_seed_pin_pause_durable_after_restart(self):
        review=self.preview()['result']
        self.assertEqual({r['title'] for r in review['rows']},{'Altes Hörbuch','Neue Aufnahme'})
        row=next(r for r in review['rows'] if r['local_id']=='old')
        self.assertEqual(set(row['approvals']),{'Titelsperre','Mitlese-Freigabe'})
        self.assertEqual(row['missing'],['Audiobookshelf','BookOrbit'])
        self.assertEqual(self.apply(review,[row['id']])['result'],{'retired':1})
        self.assertEqual(self.b.config['mappings'],[self.kept])
        self.assertEqual(self.b.config['native_readalong'],{})
        self.assertEqual(self.b.config['paused_mappings'],{})
        self.assertEqual(self.b.load('_podcast:x')['baseline']['position'],123)
        self.assertEqual(self.b.load('still')['baseline']['position'],200)
        cfg,creds,_=self.admin.manager.effective()
        self.assertEqual(len(cfg['mappings']),2)
        for raw_cfg in [cfg,self.config]:
            restarted=Bridge(self.root,copy.deepcopy(raw_cfg),creds)
            try:
                self.assertEqual(restarted._seed_mappings(),[self.kept,self.auto])
                self.assertEqual(restarted.config['native_readalong'],{})
            finally: close_bridge(restarted)
        replacement=dict(self.static,bo_id=62,fingerprint='rebuilt')
        self.assertEqual(overlay(dict(cfg,mappings=[replacement]),self.b.db)['mappings'],[replacement])
        self.assertEqual(self.apply(review,[row['id']])['status'],'failed')

    def test_pending_selected_transfer_and_change_in_remote_catalog(self):
        review=self.preview()['result']
        self.b.save('_jellyfin_audio:old',{'pending':{'id':'running'}})
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertTrue(self.b.db.execute('SELECT 1 FROM auto_mappings').fetchone())
        self.assertTrue(next(r for r in self.preview()['result']['rows'] if r['local_id']=='old')['pending'])
        self.b.save('_jellyfin_audio:old',{})
        self.b.bo.query.return_value=[{'id':7,'libraryId':1},{'id':5,'libraryId':1}]
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertEqual(self.b.load('old')['baseline']['position'],90)

    def test_pending_primary_operation_and_nonselected_transfer(self):
        self.b.db.execute("INSERT INTO operations VALUES('journal','old','{}','pending')")
        self.b.db.commit()
        review=self.preview()['result']
        auto=next(r['id'] for r in review['rows'] if r['local_id']=='auto')
        self.assertEqual(self.apply(review,[auto])['result'],{'retired':1})
        self.assertTrue(self.b.load('old'))
        self.assertFalse(self.b.db.execute('SELECT 1 FROM auto_mappings').fetchone())
        self.assertTrue(self.b.db.execute('SELECT 1 FROM operations').fetchone())

    def test_empty_wrong_account_missing_library_incomplete_catalog_and_stale_revision(self):
        review=self.preview()['result']
        self.b.abs.inventory.return_value=[]
        self.b.abs._request.return_value={'id':'other'}
        self.assertEqual(self.preview()['status'],'failed')
        self.b.abs._request.return_value={'id':'as-user'}
        self.b.abs.libraries.return_value=[]
        self.assertEqual(self.preview()['status'],'failed')
        self.b.abs.libraries.return_value=[{'id':'library','mediaType':'book'}]
        self.b.abs.inventory.side_effect=RuntimeError('private path')
        failed=self.preview()
        self.assertEqual(failed['status'],'failed')
        self.assertNotIn('private',failed['error'])
        with self.admin.manager._locked():
            state,raw=self.admin.manager._read()
            state['revision']+=1
            self.admin.manager._write(state,raw)
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertTrue(self.b.load('old'))

    def test_ebook_manual_and_automatic_missing_and_wrong_jellyfin_library(self):
        automatic=dict(self.ebook,bo_id=8,file_id=55,jf_id='d'*32,title='Anderes EPUB')
        self.b.save('_ebook_discovery',{'checked_at':101,'mappings':[automatic]})
        self.b.jellyfin.request.return_value={'Items':[]}
        self.assertEqual(self.preview('ebook')['status'],'failed')
        self.b.jellyfin.request.return_value={'Items':[{'Id':'ebooks'}]}
        review=self.preview('ebook')['result']
        self.assertEqual(len(review['rows']),2)
        self.b.save('_jellyfin_ebook:50',{'pending':{'id':'pending'}})
        self.assertEqual(self.apply(review)['status'],'failed')
        self.b.save('_jellyfin_ebook:50',{})
        self.assertEqual(self.apply(review)['result'],{'retired':2})
        self.assertEqual(self.b.config['jellyfin']['ebooks'],[])
        self.assertEqual(self.b.load('_ebook_discovery'),{'checked_at':101,'mappings':[]})
        self.assertEqual(self.b.config['jellyfin']['auto_ebooks']['bo_library_id'],2)
        self.assertEqual(self.b.load('old')['baseline']['position'],90)

    def test_failed_second_selected_item_rolls_back_all_local_changes(self):
        review=self.preview()['result']
        self.b.db.execute("CREATE TRIGGER refuse_auto BEFORE DELETE ON auto_mappings BEGIN SELECT RAISE(ABORT,'fail'); END")
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertEqual(self.b.config['mappings'],[self.static,self.kept])
        self.assertTrue(self.b.load('old'))
        self.assertEqual(self.b.db.execute('SELECT count(*) FROM retired_book_seeds').fetchone()[0],0)

    def test_ebook_file_disappears_with_book_still_present(self):
        self.b.jellyfin.items.return_value=[{'Id':self.ebook['jf_id']}]
        self.b.bo.query.return_value=[{'id':11,'libraryId':2}]
        self.b.bo.detail.return_value={'id':11,'libraryId':2,'files':[{'id':80}]}
        review=self.preview('ebook')['result']
        self.assertEqual(review['rows'][0]['missing'],['BookOrbit'])
        self.b.bo.detail.return_value={'id':11,'libraryId':2,'files':[{'id':50}]}
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertEqual(self.preview('ebook')['result']['rows'],[])

    def test_conflicting_manual_approval_change_invalidates_selection(self):
        review=self.preview()['result']
        self.b.db.execute('INSERT INTO native_readalong_enrollments VALUES(?,?,?)',('old',5,'{"proof":1}'))
        self.b.db.commit()
        self.assertEqual(self.apply(review)['status'],'failed')
        self.assertTrue(self.b.db.execute('SELECT 1 FROM native_readalong_enrollments').fetchone())
        review=self.preview()['result']
        self.assertIn('Bestätigte Mitlese-Zuordnung',next(r for r in review['rows'] if r['local_id']=='old')['approvals'])

    def test_retiring_bookbridge_pair_skips_only_that_import_and_preserves_bookorbit(self):
        from provider_runtime import BookBridgeCoordinator, pair_identity
        from library_cleanup import retire
        from bookorbit_scope import signature
        pair=dict(id=92,abs_id='old',audio_source='ABS',title='Alte Zuordnung')
        cfg=dict(self.b.config,mapping_provider='bookbridge')
        self.b.provider='bookbridge'
        self.b.config=cfg
        self.b.bookbridge=Mock()
        self.b.bookbridge.inventory.return_value=[pair]
        self.b.save('_bookbridge_mapping_inventory',{'mappings':[dict(abs_id='old',bb_id=92,pair=pair_identity(pair),title='Alt')]})
        self.b.save('_bookbridge_audio:old',{'baseline':{}})
        with self.b.db:
            retire(self.b,'audio',{'old'})
        self.b.retired_bookbridge_pairs=set(self.b.db.execute("SELECT local_id,seed FROM retired_book_seeds WHERE media='bookbridge'"))
        self.assertEqual(self.b.retired_bookbridge_pairs,{('old',signature(pair_identity(pair)))})
        self.assertEqual(BookBridgeCoordinator(self.b).mappings(),([],[]))
        self.b.abs.item.assert_not_called()
        self.assertEqual(self.b.load('old')['baseline']['position'],90)
        self.assertEqual(self.b.config['mappings'],[self.static,self.kept])
