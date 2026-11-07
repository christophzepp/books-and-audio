"""Podcast controls preserve independent book state and never publish feed secrets."""
import copy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import admin_runtime as runtime
from connection_manager import ConnectionManager
from test_admin_runtime import Fixture, CandidateBridge, CloseClient


class PodcastAdminTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config['podcasts'] = dict(enabled=False, abs_library_id='podcasts-abs', jellyfin_library_id='podcasts-jf',
            abs_root='/podcasts', jellyfin_root='/data/podcasts', worker_root='/podcasts', expected_abs_username='reader')
        self.admin=runtime.Administration(self.root,self.key,self.config,self.credentials,CandidateBridge)
        self.settings=dict(enabled=True,abs_library_id='podcasts-abs',jellyfin_library_id='podcasts-jf',pinepods_enabled=False,bookorbit_enabled=False)

    def command(self,kind,payload):
        queued=self.admin.store.submit(kind,payload)
        self.assertTrue(self.admin.process_one(None))
        return self.admin.store.command(queued['id'])

    def podcast(self,apply=False,**changes):
        settings=dict(self.settings,**changes)
        return self.command('podcast.configure' if apply else 'podcast.preview',dict(expected_revision=self.admin.manager.public()['revision'],settings=settings))

    def test_preview_uses_isolated_database_without_bookorbit_guard(self):
        before=self.saved_state();raw=self.admin.manager.path.read_bytes();seen=[]
        def preview(bridge):
            seen.append(bridge)
            self.assertEqual(bridge.config['podcasts']['worker_root'],'/podcasts')
            bridge.db.execute('INSERT INTO state VALUES (?,?)',('candidate-only','{}'));bridge.db.commit()
            return dict(ok=True,mapped=4,episodes=[dict(title='Podcast',feed='https://private/?token=SECRET')])
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=preview)}),patch.object(CandidateBridge,'guard',side_effect=AssertionError('BookOrbit must not be consulted')):
            result=self.podcast()
        self.assertEqual(result['status'],'success');self.assertEqual(result['result']['preview'],dict(ok=True,mapped=4,scope=dict(episodes=1,position=0,status_only=0,waiting=0,blocked=0,proposed=0)))
        self.assertFalse(result['result']['applied']);self.assertEqual(self.saved_state(),before)
        self.assertEqual(self.admin.manager.path.read_bytes(),raw);self.assertNotEqual(seen[0].root,self.root)
        self.assertFalse(seen[0].root.exists());self.assertNotIn('SECRET',json.dumps(result))

    def test_primary_source_is_validated_persisted_and_preserved_for_older_clients(self):
        self.assertEqual(self.admin.manager.public()['podcasts']['primary_source'], 'auto')
        with patch.dict('sys.modules', {'podcast_sync': SimpleNamespace(preview=lambda b:dict(ok=True,mapped=2))}):
            for invalid in (None, [], 'bo', 'gpodder'):
                self.assertEqual(self.podcast(True,primary_source=invalid)['status'], 'failed')
            self.assertEqual(self.podcast(True,primary_source='pinepods')['status'], 'failed')
            for primary in ('abs', 'jf', 'auto'):
                self.assertEqual(self.podcast(True,primary_source=primary)['status'], 'success')
                self.assertEqual(self.admin.manager.effective()[0]['podcasts']['primary_source'],primary)
                self.assertEqual(self.podcast(True)['status'], 'success')
                self.assertEqual(self.admin.manager.public()['podcasts']['primary_source'],primary)

    def test_primary_preview_does_not_change_live_settings(self):
        before=self.admin.manager.path.read_bytes(); seen=[]
        def preview(bridge):
            seen.append(bridge.config['podcasts']['primary_source']);return dict(ok=True,mapped=2)
        with patch.dict('sys.modules', {'podcast_sync': SimpleNamespace(preview=preview)}):
            self.assertEqual(self.podcast(False,primary_source='jf')['status'],'success')
        self.assertEqual(seen,['jf']);self.assertEqual(self.admin.manager.path.read_bytes(),before)

    def test_primary_only_change_retains_existing_identity_lock_without_approving_it(self):
        from pinepods_client import EpisodeUnavailable
        import copy
        with patch.dict('sys.modules', {'podcast_sync': SimpleNamespace(preview=lambda b:dict(ok=True,mapped=1))}):
            self.assertEqual(self.podcast(True)['status'],'success')
        reason=EpisodeUnavailable.REASONS['duration_mismatch']
        row=dict(id='_podcast:a',pine_reason_code='duration_mismatch',pine_status='blocked',
                 progress_status='blocked',reason=reason,position=0,status='not_started',duration=100,
                 states=dict(abs=dict(position=0,status='not_started'),jf=dict(position=0,status='not_started')))
        result=dict(ok=False,mode='dry-run',enabled=True,mapped=1,scope_fingerprint='a'*64,
                    episodes=[row],blocked=[dict(id='_podcast:a',reason=reason)])
        def preview(b):return copy.deepcopy(result)
        module=SimpleNamespace(preview=preview,pine_history_ids=lambda b:set())
        before=self.saved_state()
        with patch.dict('sys.modules',{'podcast_sync':module}):
            checked=self.podcast(False,primary_source='jf')
            self.assertEqual(checked['status'],'success')
            self.assertEqual(checked['result']['preview']['retained_blocked'],1)
            applied=self.podcast(True,primary_source='jf')
            self.assertEqual(applied['status'],'success')
            self.assertEqual(self.podcast(True,primary_source='abs',abs_library_id='different')['status'],'failed')
        self.assertEqual(self.saved_state(),before)

    def test_inactive_preview_checks_scope_without_enabling_it(self):
        checked=[]
        def preview(bridge):
            checked.append(bridge.config['podcasts']['enabled'])
            return dict(ok=True,mapped=3)
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=preview)}):result=self.podcast(False,enabled=False)
        self.assertEqual(result['status'],'success');self.assertEqual(checked,[True])
        self.assertFalse(self.admin.manager.public()['podcasts']['enabled']);self.assertEqual(self.admin.manager.public()['revision'],0)

    def test_configure_persists_scope_and_retains_pinned_roots(self):
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=lambda bridge:dict(ok=True,mapped=4))}):result=self.podcast(True)
        self.assertEqual(result['status'],'success');cfg,_,_=self.admin.effective()
        self.assertTrue(cfg['podcasts']['enabled']);self.assertEqual(cfg['podcasts']['worker_root'],'/podcasts')
        self.assertEqual(cfg['mappings'],self.config['mappings']);self.assertEqual(self.admin.manager.public()['revision'],1)

    def test_optional_pine_enrollment_requires_explicit_boolean_opt_in(self):
        self.assertFalse(self.admin.manager.public()['podcasts']['pinepods_optional_enrollment'])
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=lambda b:dict(ok=True,mapped=2))}):
            result=self.podcast(True)
            self.assertEqual(result['status'],'success')
            self.assertFalse(self.admin.manager.public()['podcasts']['pinepods_optional_enrollment'])
            rejected=self.podcast(True,pinepods_optional_enrollment=1)
            self.assertEqual(rejected['status'],'failed')
            result=self.podcast(True,pinepods_optional_enrollment=True)
            self.assertEqual(result['status'],'success')
            self.assertTrue(self.admin.manager.effective()[0]['podcasts']['pinepods_optional_enrollment'])
            legacy_update=self.podcast(True)
            self.assertEqual(legacy_update['status'],'success')
            self.assertTrue(self.admin.manager.public()['podcasts']['pinepods_optional_enrollment'])

    def test_preview_preserves_only_safe_pine_counts(self):
        preview=lambda b:dict(ok=True,mapped=2,pine_waiting=1,pine_enrolled=1,pine_joining='SECRET',episodes=[{'pine_reason':'SECRET'}])
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=preview)}):result=self.podcast()
        self.assertEqual(result['result']['preview'],dict(ok=True,mapped=2,pine_waiting=1,pine_enrolled=1,scope=dict(episodes=1,position=0,status_only=0,waiting=0,blocked=0,proposed=0)))
        self.assertNotIn('SECRET',json.dumps(result))

    def test_variant_status_mode_is_explicit_typed_and_preserved_by_older_clients(self):
        key='pinepods_variant_status_only'
        self.assertFalse(self.admin.manager.public()['podcasts'][key])
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=lambda b:dict(ok=True,mapped=2))}):
            for invalid in (1, 'true', None):
                self.assertEqual(self.podcast(True,**{key:invalid})['status'],'failed')
            self.assertEqual(self.podcast(True,**{key:True})['status'],'success')
            self.assertTrue(self.admin.manager.effective()[0]['podcasts'][key])
            self.assertEqual(self.podcast(True)['status'],'success')
            self.assertTrue(self.admin.manager.public()['podcasts'][key])
            self.assertEqual(self.podcast(True,**{key:False})['status'],'success')
            self.assertFalse(self.admin.manager.public()['podcasts'][key])

    def test_status_only_mode_is_explicit_and_does_not_publish_raw_proof_or_errors(self):
        rows=[dict(id='variant',pine_mode='status_only',pine_status='enrolled',
                   status='in_progress',progress_status='unchanged',participants=['abs','jf','pinepods','gpodder'],
                   variant_proof={'token':'SECRET'},pine_status_reason='SECRET'),
              dict(id='conflict',pine_mode='status_only',pine_status='blocked',
                   pine_status_reason_code='variant_initial_status_conflict'),
              dict(id='invalid',pine_mode='SECRET',pine_status='waiting',pine_status_reason_code='SECRET')]
        result=runtime.public_podcasts(dict(episodes=rows),dict(self.settings,pinepods_variant_status_only=True),False)
        self.assertEqual(result['pine_status_only'],1)
        self.assertEqual(result['episodes'][0]['pine_mode'],'status_only')
        self.assertIn('Anfangszustände',result['episodes'][1]['pine_status_reason'])
        self.assertNotIn('pine_mode',result['episodes'][2])
        self.assertNotIn('SECRET',json.dumps(result))

    def test_stale_revision_does_not_overwrite_saved_scope(self):
        with patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(preview=lambda b:dict(ok=True,mapped=0))}):self.podcast(True)
        result=self.command('podcast.configure',dict(expected_revision=0,settings=dict(self.settings,enabled=False)))
        self.assertEqual(result['status'],'failed');self.assertTrue(self.admin.manager.public()['podcasts']['enabled'])

    def test_bookorbit_participant_cannot_be_enabled(self):
        result=self.podcast(True,bookorbit_enabled=True)
        self.assertEqual(result['status'],'failed');self.assertIn('vorbereitet',result['error'])

    def test_unconnected_pinepods_cannot_be_enabled(self):
        result=self.podcast(True,pinepods_enabled=True)
        self.assertEqual(result['status'],'failed');self.assertIn('PinePods',result['error'])

    def test_roots_cannot_be_replaced_through_gui_payload(self):
        settings=dict(self.settings,worker_root='/etc')
        result=self.command('podcast.configure',dict(expected_revision=0,settings=settings))
        self.assertEqual(result['status'],'failed');self.assertEqual(self.admin.manager.public()['revision'],0)

    def test_pending_podcast_prevents_scope_change_and_disable(self):
        self.put('_podcast:episode',dict(pending=dict(before={'abs':{},'jf':{}})))
        for enabled in (True,False):
            result=self.podcast(True,enabled=enabled)
            self.assertEqual(result['status'],'failed')
        self.assertEqual(self.admin.manager.public()['revision'],0)

    def test_pending_variant_transfer_prevents_scope_change_and_disconnect(self):
        self.put('_podcast_status:episode',dict(pending=dict(before={'abs':{},'jf':{}})))
        original=self.admin.manager.path.read_bytes()
        self.assertEqual(self.podcast(True,pinepods_variant_status_only=True)['status'],'failed')
        self.assertEqual(self.podcast(True,enabled=False)['status'],'failed')
        for ident in ('audiobookshelf-default','jellyfin-default'):
            result=self.command('connection.deactivate',dict(expected_revision=0,id=ident))
            self.assertEqual(result['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),original)

    def test_pending_podcast_prevents_participant_disconnect(self):
        self.put('_podcast:episode',dict(pending={'stage':'sending'}))
        before=self.admin.manager.path.read_bytes()
        for ident in ('audiobookshelf-default','jellyfin-default'):
            result=self.command('connection.deactivate',dict(expected_revision=0,id=ident))
            self.assertEqual(result['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),before)

    def test_public_episode_snapshot_excludes_feed_urls_and_secrets(self):
        result=runtime.public_podcasts(dict(at='2026-11-01',episodes=[dict(id='episode',title='Title',show='Show',position=40,status='ok',progress_status='in_progress',participants=['abs','jf'],feed='SECRET',enclosure='SECRET',pending={'token':'SECRET'})]),self.settings,False)
        self.assertEqual(result['episodes'][0]['position'],40)
        self.assertNotIn('SECRET',json.dumps(result));self.assertEqual(result['bookorbit'],'prepared')

    def test_optional_pine_waiting_keeps_successful_core_status_and_active_peers(self):
        row = dict(id='episode',status='in_progress',progress_status='synced',position=120,
                   participants=['abs','jf'],pine_status='waiting',pine_reason_code='duration_mismatch')
        result = runtime.public_podcasts(dict(episodes=[row]),dict(self.settings,pinepods_enabled=True),False)
        episode = result['episodes'][0]
        self.assertEqual(episode['status'],'synced')
        self.assertEqual(episode['participants'],['abs','jf'])
        self.assertEqual(result['pine_waiting'],1)
        self.assertEqual(result['pine_enrolled'],0)
        self.assertIn('Audiodauer',episode['pine_reason'])

    def test_pine_enrollment_and_blocked_peer_are_displayed_separately(self):
        rows = [dict(id='active',status='in_progress',progress_status='unchanged',participants=['abs','jf','pinepods','gpodder'],pine_status='enrolled'),
                dict(id='blocked',status='in_progress',progress_status='blocked',participants=['abs','jf','pinepods','gpodder'],pine_status='blocked')]
        result = runtime.public_podcasts(dict(episodes=rows),dict(self.settings,pinepods_enabled=True),False)
        self.assertEqual(result['episodes'][0]['participants'],['abs','jf','pinepods','gpodder'])
        self.assertEqual(result['episodes'][0]['status'],'ok')
        self.assertEqual(result['episodes'][1]['status'],'blocked')
        self.assertEqual(result['pine_enrolled'],1)
        self.assertEqual(result['pine_blocked'],1)
        self.assertEqual(result['pine_waiting'],0)

    def test_unresolved_pine_identity_is_visible_without_claiming_missing_or_synced(self):
        row=dict(id='episode',status='in_progress',progress_status='synced',participants=['abs','jf'],
            pine_status='waiting',pine_reason_code='episode_identity_unresolved')
        result=runtime.public_podcasts(dict(episodes=[row]),dict(self.settings,pinepods_enabled=True),False)
        episode=result['episodes'][0]
        self.assertEqual(episode['participants'],['abs','jf'])
        self.assertEqual(episode['pine_status'],'waiting')
        self.assertEqual(episode['pine_reason_code'],'episode_identity_unresolved')
        self.assertIn('Zuordnung ungeklärt',episode['pine_reason'])
        self.assertIn('kein',episode['pine_reason'].lower())
        self.assertIn('eindeutige Zuordnung erforderlich',episode['pine_reason'])
        self.assertEqual(result['pine_waiting'],1)
        self.assertEqual(result['pine_enrolled'],0)

    def test_pine_details_do_not_publish_raw_errors_or_feed_urls(self):
        rows = [dict(id='one',pine_status='waiting',pine_reason='https://private/?token=SECRET',pine_reason_code='SECRET'),
                dict(id='two',pine_status='blocked',pine_reason={'password':'SECRET'},pine_reason_code=['SECRET'])]
        result = runtime.public_podcasts(dict(episodes=rows),self.settings,False)
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertEqual(result['episodes'][0]['pine_reason'],'Die PinePods-Zuordnung ist noch nicht bestätigt.')
        self.assertIn('gesperrt',result['episodes'][1]['pine_reason'])

    def test_changed_variant_identity_exposes_explicit_editor_without_approving_it(self):
        rows=[dict(id='_podcast:show:'+code,progress_status='blocked',pine_status='blocked',
                   pine_mode='status_only',pine_status_reason_code=code,reviewed_route=False)
              for code in ('variant_identity_changed','variant_proof_stale','variant_pending_review')]
        rows.append(dict(rows[0],id='unmapped'))
        result=runtime.public_podcasts(dict(episodes=rows),dict(self.settings,pinepods_enabled=True),False)
        for row in result['episodes'][:2]:
            self.assertTrue(row['can_edit_status'])
            self.assertFalse(row['can_review'])
            self.assertEqual(row['status'],'blocked')
        self.assertFalse(result['episodes'][2]['can_edit_status'])
        self.assertFalse(result['episodes'][3]['can_edit_status'])

    def test_pause_preserves_pine_admission_information_without_core_success(self):
        row = dict(id='episode',status='in_progress',progress_status='unchanged',participants=['abs','jf'],pine_status='joining')
        result = runtime.public_podcasts(dict(episodes=[row]),self.settings,True)
        self.assertEqual(result['episodes'][0]['status'],'paused')
        self.assertEqual(result['episodes'][0]['pine_status'],'joining')
        self.assertEqual(result['pine_joining'],1)

    def test_gui_disable_does_not_touch_progress(self):
        before=self.saved_state()
        result=self.podcast(True,enabled=False)
        self.assertEqual(result['status'],'success');self.assertEqual(self.saved_state(),before)
        self.assertFalse(self.admin.manager.public()['podcasts']['enabled'])

    def test_optional_pinepods_credentials_encrypted_and_crud(self):
        result=self.command('connection.create',dict(expected_revision=0,kind='pinepods',name='PinePods',settings=dict(url='http://pinepods:8040',username='reader',device_id='book-progress-sync',server_version='0.9.0',completion_margin_seconds=60),secrets=dict(password='PRIVATE-PINE-PASSWORD')))
        self.assertEqual(result['status'],'success');ident=result['result']['profile_id']
        public=self.admin.manager.public();self.assertFalse(next(x for x in public['profiles'] if x['id']==ident)['active'])
        self.assertNotIn('PRIVATE-PINE-PASSWORD',json.dumps(public));self.assertNotIn(b'PRIVATE-PINE-PASSWORD',self.admin.manager.path.read_bytes())
        result=self.command('connection.delete',dict(expected_revision=1,id=ident))
        self.assertEqual(result['status'],'success')

    def test_pinepods_missing_device_cannot_activate(self):
        result=self.command('connection.create',dict(expected_revision=0,kind='pinepods',name='PinePods',settings=dict(url='http://pinepods:8040',username='reader',device_id='book-progress-sync',server_version='0.9.0'),secrets=dict(password='PRIVATE')))
        ident=result['result']['profile_id']
        with patch('admin_runtime.probe',return_value=dict(ok=True,username='reader',device_id='book-progress-sync',server_version='0.9.0',device_exists=False,protocol='gpodder-v2',progress_sync_supported=True)),patch('connection_manager.validate_url',side_effect=lambda url,**kwargs:url):
            result=self.command('connection.activate',dict(expected_revision=1,id=ident))
        self.assertEqual(result['status'],'failed');self.assertFalse(any(p['active'] for p in self.admin.manager.public()['profiles'] if p['kind']=='pinepods'))

    def test_pinepods_activation_does_not_contact_bookorbit(self):
        result=self.command('connection.create',dict(expected_revision=0,kind='pinepods',name='PinePods',settings=dict(url='http://pinepods:8040',username='reader',device_id='book-progress-sync',server_version='0.9.0'),secrets=dict(password='PRIVATE')))
        ident=result['result']['profile_id']
        with patch('admin_runtime.probe',return_value=dict(ok=True,username='reader',device_id='book-progress-sync',server_version='0.9.0',device_exists=True,protocol='gpodder-v2',progress_sync_supported=True)),patch('connection_manager.validate_url',side_effect=lambda url,**kwargs:url),patch.object(self.admin,'ensure_identities',side_effect=AssertionError('BookOrbit not required')):
            result=self.command('connection.activate',dict(expected_revision=1,id=ident))
        self.assertEqual(result['status'],'success')
        cfg,creds,_=self.admin.effective();self.assertTrue(cfg['pinepods']['enabled']);self.assertEqual(creds['pinepods_password'],'PRIVATE')
        self.assertFalse(cfg['podcasts']['enabled'])

    def test_pinepods_probe_strips_actions_and_requires_readonly_client(self):
        client=CloseClient();client.probe=lambda:dict(username='reader',device_id='sync',device_exists=True,protocol='gpodder-v2',progress_sync_supported=True,actions=[{'episode':'https://private/SECRET'}])
        fake=SimpleNamespace(PinepodsClient=Mock(return_value=client))
        with patch.dict('sys.modules',{'pinepods_client':fake}):result=runtime.probe('pinepods',dict(url='http://pine',username='reader',device_id='sync'),dict(password='SECRET'))
        self.assertTrue(result['ok']);self.assertTrue(client.closed);self.assertNotIn('SECRET',json.dumps(result))

    def test_static_deployment_roots_fill_missing_keys_without_replacing_verified_scope(self):
        self.admin.base_config['podcasts']['worker_root']='/new-trusted-root'
        cfg,_,_=self.admin.effective();self.assertEqual(cfg['podcasts']['worker_root'],'/podcasts')
        self.assertEqual(self.admin.manager.effective()[0]['podcasts']['worker_root'],'/podcasts')


class PodcastLoopTests(Fixture):
    def test_podcast_cycle_runs_when_bookorbit_is_disabled(self):
        self.config['podcasts']=dict(enabled=True,abs_library_id='podcast',jellyfin_library_id='podcast')
        admin=runtime.Administration(self.root,self.key,self.config,self.credentials,CandidateBridge)
        admin.manager.mutate('deactivate',dict(expected_revision=0,id='bookorbit-default'))
        args=SimpleNamespace(key=self.key,apply=True,settle=30)
        run=Mock()
        class PodcastOnlyBridge(CandidateBridge):
            def __init__(self,root,config,credentials):
                adjusted=copy.deepcopy(config);adjusted['bo_url']='unused'
                creds=dict(credentials,bo_username='unused',bo_password='unused')
                super().__init__(root,adjusted,creds)
            def cycle(self,*args):raise AssertionError('Books must remain paused')
            def deliver_notifications(self):pass
        with patch('admin_runtime.Administration.ensure_identities'),patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(run=run)}),patch('admin_runtime.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):runtime.run_admin_loop(args,self.root,self.config,self.credentials,PodcastOnlyBridge)
        run.assert_called_once();self.assertEqual(json.loads((self.root/'last-run.json').read_text())['mode'],'paused')

    def test_podcast_cycle_survives_bookorbit_runtime_failure(self):
        self.config['podcasts']=dict(enabled=True,abs_library_id='podcast',jellyfin_library_id='podcast')
        args=SimpleNamespace(key=self.key,apply=True,settle=30);run=Mock()
        class BookFailureBridge(CandidateBridge):
            def cycle(self,*args):raise RuntimeError('BookOrbit unavailable')
            def save(self,key,value):
                self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value)));self.db.commit()
            def alert(self,*args):pass
            def deliver_notifications(self):pass
        with patch('admin_runtime.Administration.ensure_identities'),patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(run=run)}),patch('admin_runtime.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):runtime.run_admin_loop(args,self.root,self.config,self.credentials,BookFailureBridge)
        run.assert_called_once();self.assertEqual(json.loads((self.root/'last-run.json').read_text())['fatal'],'BookOrbit unavailable')

    def test_global_pause_also_stops_podcasts(self):
        self.config['podcasts']=dict(enabled=True,abs_library_id='podcast',jellyfin_library_id='podcast')
        admin=runtime.Administration(self.root,self.key,self.config,self.credentials,CandidateBridge);admin.store.pause(True)
        run=Mock();args=SimpleNamespace(key=self.key,apply=True,settle=30)
        with patch('admin_runtime.Administration.ensure_identities'),patch.dict('sys.modules',{'podcast_sync':SimpleNamespace(run=run)}),patch('admin_runtime.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):runtime.run_admin_loop(args,self.root,self.config,self.credentials,CandidateBridge)
        run.assert_not_called()


if __name__=='__main__':unittest.main()
