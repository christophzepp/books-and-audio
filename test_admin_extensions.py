"""Queued folder/provider administration; no network or production writes."""
import copy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import test_admin_runtime as fixtures
import admin_runtime as runtime
from folder_manager import default_rows


class ExtensionCandidate:
    instances=[]
    def __init__(self, root, config, credentials):
        self.root=Path(root);self.config=copy.deepcopy(config);self.credentials=copy.deepcopy(credentials)
        self.abs,self.bo=fixtures.CloseClient(),fixtures.CloseClient()
        self.bookbridge=fixtures.CloseClient() if config.get('mapping_provider')=='bookbridge' else None
        self.jellyfin=None
        self.db=sqlite3.connect(self.root/'state.sqlite');self.reads=[]
        self.instances.append(self)
    def guard(self): pass
    def list_mappings(self):return copy.deepcopy(self.config['mappings'])
    def read_pair(self,mapping):
        self.reads.append(mapping['abs_id'])
        # Even a badly behaved candidate can only write its disposable copy.
        self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('candidate-only','{}'));self.db.commit()
        return {},{}
    def load(self,key):
        row=self.db.execute('SELECT value FROM state WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}


class AdministrationExtensions(fixtures.Fixture):
    def setUp(self):
        super().setUp();self.media=self.root.resolve()/'media';self.media.mkdir()
        self.audio=self.media/'audio';self.audio.mkdir();self.ebooks=self.media/'ebooks';self.ebooks.mkdir()
        self.config['jellyfin']['enabled']=False
        self.config['folder_access_roots']=[dict(worker=str(self.audio),server='/srv/example-media/audiobooks'),dict(worker=str(self.ebooks),server='/srv/example-media/ebooks')]
        self.config['folder_mappings']=default_rows({})
        self.config['folder_mappings'][0]['paths']['worker']=str(self.audio)
        self.config['folder_mappings'][1]['paths']['worker']=str(self.ebooks)
        self.config['bookbridge']=dict(enabled=True,url='http://bookbridge',username='reader',version='7.11.0')
        self.credentials['bookbridge_password']='PRIVATE-BB-PASSWORD'
        mounted=patch('folder_manager.os.path.ismount',return_value=True);mounted.start();self.addCleanup(mounted.stop)
        self.admin=runtime.Administration(self.root,self.key,self.config,self.credentials,ExtensionCandidate)
        self.admin.identities={'audiobookshelf':dict(username='reader',user_id='abs-user',library_ids=['library'])}
        ExtensionCandidate.instances=[]

    def command(self,kind,payload,bridge=None):
        queued=self.admin.store.submit(kind,payload)
        self.assertTrue(self.admin.process_one(bridge))
        return self.admin.store.command(queued['id'])

    def test_folder_create_test_and_update_use_real_queue(self):
        before=self.saved_state()
        result=self.command('folder.create',dict(expected_revision=0,kind='ebooks',name='Testordner',paths=dict(worker=str(self.ebooks),bo='/books',jf='/data/ebooks',bb='/books')))
        self.assertEqual(result['status'],'success');ident=result['result']['folder_id']
        rows=self.admin.folders.public()['rows'];self.assertFalse(next(x for x in rows if x['id']==ident)['active'])
        checked=self.command('folder.test',dict(expected_revision=1,id=ident))
        self.assertEqual(checked['status'],'success');self.assertEqual(checked['result']['code'],'folder_checked')
        updated=self.command('folder.update',dict(expected_revision=1,id=ident,name='Umbenannt'))
        self.assertEqual(updated['status'],'success')
        self.assertEqual(self.admin.folders.public()['revision'],2)
        self.assertEqual(self.saved_state(),before)
        self.assertEqual(list(self.ebooks.iterdir()),[])

    def test_active_folder_change_calls_compatibility_before_persistence(self):
        before=self.admin.folders.public();calls=[]
        def refuse(old,new):calls.append((old,new));return False
        with patch.object(self.admin,'folder_compatible',side_effect=refuse):
            result=self.command('folder.update',dict(expected_revision=0,id='audiobooks-default',paths=dict(jf='/different')))
        self.assertEqual(result['status'],'failed');self.assertEqual(len(calls),1)
        self.assertEqual(self.admin.folders.public(),before)

    def test_stale_queued_folder_change_cannot_replace_later_revision(self):
        first=self.admin.store.submit('folder.update',dict(expected_revision=0,id='audiobooks-default',name='Erste'))
        second=self.admin.store.submit('folder.update',dict(expected_revision=0,id='audiobooks-default',name='Veraltet'))
        with patch.object(self.admin,'folder_compatible',return_value=True):
            self.admin.process_one(None);self.admin.process_one(None)
        self.assertEqual(self.admin.store.command(first['id'])['status'],'success')
        self.assertEqual(self.admin.store.command(second['id'])['status'],'failed')
        self.assertEqual(self.admin.folders.public()['rows'][0]['name'],'Erste')

    def test_provider_preview_uses_copy_and_never_changes_selected_provider(self):
        before=self.saved_state();settings=self.admin.manager.path.read_bytes()
        def inspect(candidate):
            candidate.read_pair({'abs_id':'preview-only'})
            return dict(provider='bookbridge',mapped=0,matched_jellyfin=0,mappings=[],unsupported=[],jellyfin_ebook_supported=False)
        with patch('provider_runtime.provider_preview',side_effect=inspect):
            result=self.command('provider.preview',dict(expected_revision=0,provider='bookbridge'))
        self.assertEqual(result['status'],'success');self.assertFalse(result['result']['applied'])
        self.assertEqual(self.admin.manager.public()['mapping_provider'],'bookorbit')
        self.assertEqual(self.admin.manager.path.read_bytes(),settings)
        self.assertEqual(self.saved_state(),before)
        candidate=ExtensionCandidate.instances[-1]
        self.assertNotEqual(candidate.root,self.root);self.assertFalse(candidate.root.exists())
        self.assertTrue(candidate.abs.closed and candidate.bo.closed and candidate.bookbridge.closed)

    def test_provider_activate_switches_once_preserving_baselines(self):
        before=self.saved_state()
        with patch('provider_runtime.provider_preview',return_value=dict(provider='bookbridge',mapped=0)):
            result=self.command('provider.activate',dict(expected_revision=0,provider='bookbridge'))
        self.assertEqual(result['status'],'success');self.assertTrue(result['result']['applied'])
        self.assertEqual(self.admin.manager.public()['mapping_provider'],'bookbridge')
        self.assertEqual(self.saved_state(),before)
        self.assertFalse(self.admin.process_one(None))
        self.assertEqual(self.admin.manager.public()['revision'],1)

    def test_provider_preview_fails_with_pending_operation_without_candidate(self):
        with sqlite3.connect(self.root/'state.sqlite') as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?)',('pending','book','{}','pending'))
        result=self.command('provider.preview',dict(expected_revision=0,provider='bookbridge'))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(ExtensionCandidate.instances,[])
        self.assertEqual(self.admin.manager.public()['mapping_provider'],'bookorbit')

    def test_provider_preview_fails_with_pending_local_journal(self):
        self.put('book',{'pending':{'id':'uncertain'}})
        result=self.command('provider.preview',dict(expected_revision=0,provider='bookbridge'))
        self.assertEqual(result['status'],'failed');self.assertEqual(ExtensionCandidate.instances,[])

    def test_provider_failure_and_public_views_never_echo_credentials(self):
        with patch('provider_runtime.provider_preview',side_effect=RuntimeError('PRIVATE-BB-PASSWORD')):
            result=self.command('provider.preview',dict(expected_revision=0,provider='bookbridge'))
        public=json.dumps([result,self.admin.manager.public(),self.admin.folders.public(),self.admin.store.events()])
        for key,secret in self.credentials.items():
            if key != 'bo_username':self.assertNotIn(secret,public)
        self.assertNotIn(b'PRIVATE-BB-PASSWORD',self.admin.manager.path.read_bytes())
        self.assertEqual(result['status'],'failed')

    def test_queued_credentials_remain_encrypted_and_never_enter_audit(self):
        ident=self.admin.store.submit('connection.update',dict(expected_revision=0,id='bookbridge-default',secrets={'password':'NEW-PRIVATE-BB-PASSWORD'}))['id']
        with self.admin.store.db() as db:payload=db.execute('SELECT payload FROM commands WHERE id=?',(ident,)).fetchone()[0]
        self.assertNotIn(b'NEW-PRIVATE-BB-PASSWORD',payload)
        self.assertNotIn('NEW-PRIVATE-BB-PASSWORD',json.dumps(self.admin.store.events()))

    def test_pending_bookbridge_audio_cannot_be_hidden_by_disabled_jellyfin(self):
        self.put('_bookbridge_audio:book', {'pending': {'id':'uncertain', 'goal':{'position':42}}})
        cfg = copy.deepcopy(self.config); cfg['jellyfin']['enabled'] = False
        self.assertIsNotNone(runtime.participant_block(self.root, cfg))
        cfg['jellyfin']['enabled'] = True
        self.assertIsNone(runtime.participant_block(self.root, cfg))

    def test_known_bookbridge_writer_cannot_be_detached_while_still_enabled(self):
        before=self.admin.manager.path.read_bytes()
        probe=Mock(return_value=dict(abs_enabled=True,bookorbit_enabled=False,username='reader',version='7.11.0'))
        client=SimpleNamespace(probe=probe,close=lambda:None)
        with patch('bookbridge_client.BookBridgeClient',return_value=client), patch('connection_manager.validate_url',side_effect=lambda value,**kwargs:value):
            result=self.command('connection.deactivate',dict(expected_revision=0,id='bookbridge-default'))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),before)
        probe.assert_called_once()

    def test_bookbridge_profile_removal_succeeds_only_with_writers_disabled(self):
        probe=Mock(return_value=dict(abs_enabled=False,bookorbit_enabled=False,username='reader',version='7.11.0'))
        client=SimpleNamespace(probe=probe,close=lambda:None)
        with patch('bookbridge_client.BookBridgeClient',return_value=client), patch('connection_manager.validate_url',side_effect=lambda value,**kwargs:value):
            result=self.command('connection.delete',dict(expected_revision=0,id='bookbridge-default'))
        self.assertEqual(result['status'],'success')
        probe.assert_called_once()
        self.assertFalse(any(p['id']=='bookbridge-default' for p in self.admin.manager.public()['profiles']))

    def test_fresh_bookbridge_only_identity_setup_never_queries_bookorbit(self):
        cfg=copy.deepcopy(self.config);cfg['mapping_provider']='bookbridge';cfg.pop('bo_url')
        cfg['auto_discovery'].pop('bo_library_ids');cfg['jellyfin']['enabled']=False
        credentials={k:v for k,v in self.credentials.items() if not k.startswith('bo_')}
        isolated=self.root/'standalone';isolated.mkdir()
        admin=runtime.Administration(isolated,self.key,cfg,credentials,ExtensionCandidate)
        calls=[]
        def probe(kind,settings,secrets):
            calls.append(kind)
            if kind=='bookorbit':raise AssertionError('BookOrbit must remain unused')
            return dict(ok=True,user_id='abs-user',username='reader',version='2.37.1',library_ids=['library'])
        with patch('admin_runtime.probe',side_effect=probe):admin.ensure_identities()
        self.assertIn('audiobookshelf',calls);self.assertNotIn('bookorbit',calls)
        self.assertEqual(admin.identities['audiobookshelf']['username'],'reader')

    def test_unmounted_root_blocks_effective_without_replacing_connections(self):
        before=self.admin.manager.path.read_bytes()
        self.admin.folders.mount_checker=lambda p:False
        with self.assertRaises(ValueError): self.admin.media_guard('audiobooks')
        cfg,creds,disabled=self.admin.effective()
        self.assertTrue(disabled);self.assertEqual(self.admin.manager.path.read_bytes(),before)
        self.assertEqual(self.admin.manager.public()['mapping_provider'],'bookorbit')


if __name__=='__main__':unittest.main()
