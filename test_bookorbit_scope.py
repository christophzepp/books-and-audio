import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import admin_runtime
import bookorbit_scope as scope
from sync import Bridge
from test_admin_runtime import Fixture
from test_bookorbit_instance import enrollment, runtime
from watch_deployment import bookorbit_runtime


class ScopeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config['bo_url'] = 'http://bookorbit.test'
        self.config['bookorbit_instance'] = dict(enrollment(), libraries=[{'id':1,'roots':['/audiobooks']}])
        proof = bookorbit_runtime(runtime(), self.config['bookorbit_instance'])
        self.gate = dict(ok=True, checked_at=time.time(), bookorbit_instance=proof)
        self.write_gate()
        self.admin = admin_runtime.Administration(self.root, self.key, self.config, self.credentials, Bridge)
        self.original = next(row['id'] for row in self.admin.manager.public()['profiles'] if row['kind']=='bookorbit')
        self.alternate = self.admin.manager.mutate('create', dict(expected_revision=0,
            name='Zweites Konto', kind='bookorbit', settings=dict(url=self.config['bo_url'],username='second',library_ids=[8]),
            secrets={'password':'OTHER-PRIVATE'}))['profile_id']
        self.calls = []
        self.roots = {1:'/audiobooks',8:'/audiobooks/andere',9:'/audiobooks/neu'}
        self.accounts = {'reader':1,'second':2}
        def client(url, username, password):
            ident = self.accounts[username]
            self.calls.append(('login',username))
            def read(method, endpoint, safe):
                self.assertEqual((method,endpoint),('GET','/api/v1/auth/me'))
                return {'id':ident}
            def library(number):
                self.calls.append(('library',number))
                return dict(id=number,folders=[dict(id=number,libraryId=number,path=self.roots[number])])
            return SimpleNamespace(user={'id':ident},_request=read,library=library,close=lambda:None)
        patcher = patch('bookorbit_scope.BOClient', side_effect=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_gate(self):
        (self.root/'deployment.json').write_text(json.dumps(self.gate))

    def request(self, kind, payload):
        ident = self.admin.store.submit(kind, payload)['id']
        self.assertTrue(self.admin.process_one(None))
        return self.admin.store.command(ident)

    def preview(self, profile=None, ids=None):
        return self.request('bookorbit.preview_scope', dict(profile_id=profile or self.alternate,
            library_ids=ids or [8], ebook_library_id=None, expected_revision=self.admin.manager.public()['revision']))

    def approve(self, preview, **kwargs):
        result = preview['result']
        return self.request('bookorbit.approve_scope', dict(review_id=result['review_id'],digest=result['digest'],
                            confirm=kwargs.get('confirm',True)))

    def test_both_directions_keep_books_and_podcasts_isolated_across_restart(self):
        self.put('_podcast:sample', {'baseline':'original podcast'})
        original_books = self.saved_state()
        preview = self.preview()
        self.assertEqual(preview['status'], 'success', preview)
        self.assertEqual(preview['result']['previous']['user_id'],'1')
        self.assertEqual(preview['result']['proposed']['libraries'],[dict(id=8,roots=['/audiobooks/andere'])])
        self.assertEqual(self.admin.manager.effective()[1]['bo_username'],'reader')
        self.assertEqual(self.approve(preview)['status'],'success')
        self.assertEqual(self.calls.count(('library',8)),2)
        self.assertEqual(self.saved_state(), original_books)
        cfg, credentials, disabled = self.admin.effective()
        self.assertIsNone(disabled)
        self.assertEqual(credentials['bo_username'],'second')
        self.assertEqual(cfg['mappings'],[])
        self.assertEqual(cfg['auto_discovery']['bo_library_ids'],[8])
        alternate_path = scope.database(self.root,cfg)
        self.assertNotEqual(alternate_path,self.root/'state.sqlite')
        def open_bridge(cfg, credentials):
            bridge = Bridge(self.root,cfg,credentials)
            self.addCleanup(admin_runtime.close_bridge,bridge)
            return bridge
        alternate = open_bridge(cfg,credentials)
        self.assertEqual(alternate.load('book'),{})
        alternate.save('book',{'baseline':'other account'})
        with scope.podcast_database(alternate):
            self.assertEqual(alternate.load('_podcast:sample'),{'baseline':'original podcast'})
            with scope.book_database(alternate):
                self.assertEqual(alternate.load('book'),{'baseline':'other account'})
            alternate.save('_podcast:sample',{'baseline':'new podcast progress'})
        # Reconstruct the worker from the unchanged host config and the encrypted state.
        self.admin = admin_runtime.Administration(self.root,self.key,self.config,self.credentials,Bridge)
        self.assertEqual(scope.database(self.root,self.admin.effective()[0]), alternate_path)
        self.assertEqual(self.approve(self.preview(self.original,[1]))['status'],'success')
        cfg, credentials, disabled = self.admin.effective()
        self.assertEqual(scope.database(self.root,cfg),self.root/'state.sqlite')
        self.assertEqual(cfg['mappings'],self.config['mappings'])
        self.assertNotEqual(alternate.config['_bookorbit_activation'],cfg['_bookorbit_activation'])
        original = open_bridge(cfg,credentials)
        self.assertEqual(original.load('book'),{'baseline':{'position':90}})
        self.assertEqual(original.load('_podcast:sample'),{'baseline':'new podcast progress'})
        # A previous or changed peer status is never used as a source after returning.
        with self.assertRaises(scope.ScopeError):
            scope.initial_agreement(original,'audio:book',{'abs':90,'bo':10},int.__eq__)
        self.assertTrue(scope.initial_agreement(original,'audio:book',{'abs':90,'bo':90},int.__eq__))
        self.assertTrue(scope.baseline_needed(original,'audio:book'))
        original.save('book',{'baseline':{'position':90}})
        scope.mark_agreement(original,'audio:book')
        self.assertFalse(scope.baseline_needed(original,'audio:book'))
        self.assertEqual(alternate.load('book'),{'baseline':'other account'})
        self.assertNotIn('OTHER-PRIVATE', json.dumps(self.admin.manager.public()))
        self.assertNotIn(b'OTHER-PRIVATE', self.admin.manager.path.read_bytes())

    def test_library_change_same_account_is_a_distinct_scope(self):
        result = self.approve(self.preview(self.original,[9]))
        self.assertEqual(result['status'],'success',result)
        cfg, _, _ = self.admin.effective()
        self.assertEqual(cfg['bookorbit_instance']['user_id'],'1')
        self.assertEqual(cfg['auto_discovery']['bo_library_ids'],[9])
        self.assertIn('_bookorbit_storage',cfg)
        self.assertEqual(cfg['mappings'],[])

    def test_confirmation_expiry_and_revision_are_bound_to_the_selected_candidate(self):
        preview = self.preview()
        self.assertEqual(self.approve(preview,confirm=False)['status'],'failed')
        with patch('time.time', return_value=time.time()+301):
            self.assertEqual(self.approve(preview)['status'],'failed')
        self.admin.manager.mutate('update',dict(expected_revision=1,id=self.alternate,name='Neuer Profilname'))
        self.assertEqual(self.approve(preview)['status'],'failed')
        self.assertEqual(self.admin.manager.effective()[1]['bo_username'],'reader')

    def test_changed_account_root_or_instance_receipt_invalidates_review(self):
        for change in ('account','root','runtime'):
            with self.subTest(change=change):
                preview = self.preview()
                if change=='account': self.accounts['second']+=1
                if change=='root': self.roots[8]+='/neu'
                if change=='runtime':
                    self.gate['bookorbit_instance']['container_id']='9'*64
                    self.write_gate()
                self.assertEqual(self.approve(preview)['status'],'failed')
                self.assertEqual(self.admin.manager.effective()[1]['bo_username'],'reader')
        self.roots[8]='/unknown'
        self.assertEqual(self.preview()['status'],'failed')
        self.gate['bookorbit_instance'].pop('image')
        self.write_gate()
        self.assertEqual(self.preview()['status'],'failed')

    def test_pending_book_journal_blocks_even_when_both_accounts_still_authenticate(self):
        preview = self.preview()
        self.put('_jellyfin_ebook:3',{'pending':{'id':'op'}})
        self.assertEqual(self.approve(preview)['status'],'failed')
        self.assertEqual(self.admin.manager.effective()[1]['bo_username'],'reader')
