"""Administration contracts: no real network calls or application writes."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import admin_runtime as runtime
from admin_store import AdminStore


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / 'secret.key'; self.key.write_bytes(Fernet.generate_key()); self.key.chmod(0o600)
        self.config = dict(abs_url='http://abs', abs_version='2.37.1', bo_url='http://bo',
            auto_discovery=dict(enabled=True, abs_library_id='library', bo_library_ids=[1]),
            mappings=[{'abs_id': 'book', 'bo_id': 7, 'fingerprint': 'unchanged'}],
            jellyfin=dict(enabled=True, url='http://jf', user_id='a' * 32, username='Reader',
                          version='10.11', audio_library_id='library', ebooks=[]))
        self.credentials = dict(abs_token='PRIVATE-ABS-TOKEN', bo_username='reader', bo_password='PRIVATE-BO-PASSWORD',
                                jellyfin_token='PRIVATE-JF-TOKEN')
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            db.executescript('CREATE TABLE state (id TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE operations (id TEXT PRIMARY KEY,book TEXT,value TEXT,status TEXT);')
            db.execute('INSERT INTO state VALUES (?,?)', ('book', json.dumps({'baseline': {'position': 90}})))
        (self.root / 'deployment.json').write_text(json.dumps({'ok': True, 'checked_at': time.time()}))

    def saved_state(self):
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            return (db.execute('SELECT * FROM state ORDER BY id').fetchall(),
                    db.execute('SELECT * FROM operations ORDER BY id').fetchall())

    def put(self, key, value):
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))


class StoreTests(Fixture):
    def setUp(self):
        super().setUp(); self.store = AdminStore(self.root, self.key)

    def test_payload_and_private_review_are_encrypted_and_not_public(self):
        payload = {'expected_revision': 0, 'secrets': {'password': 'PAYLOAD-SECRET'}}
        queued = self.store.submit('connection.update', payload)
        with self.store.db() as db:
            raw = db.execute('SELECT payload FROM commands').fetchone()[0]
        self.assertNotIn(b'PAYLOAD-SECRET', raw)
        self.assertEqual(self.store.decode(raw), payload)
        claimed = self.store.claim(); self.assertEqual(claimed['payload'], payload)
        private = {'credentials': {'token': 'PRIVATE-REVIEW-SECRET'}}
        self.store.finish(queued['id'], {'ok': True}, private=private)
        public = json.dumps([self.store.command(queued['id']), self.store.events()])
        self.assertNotIn('PAYLOAD-SECRET', public); self.assertNotIn('PRIVATE-REVIEW-SECRET', public)
        self.assertNotIn('payload', self.store.command(queued['id']))
        with self.store.db() as db:
            raw = db.execute('SELECT private_result FROM commands').fetchone()[0]
        self.assertNotIn(b'PRIVATE-REVIEW-SECRET', raw)
        self.assertEqual(self.store.decode(raw), private)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_restart_does_not_replay_running_or_completed_commands(self):
        first = self.store.submit('sync.pause', {})['id']
        self.assertEqual(self.store.claim()['id'], first)
        second = self.store.submit('sync.resume', {})['id']
        restarted = AdminStore(self.root, self.key); restarted.recover()
        self.assertEqual(restarted.command(first)['status'], 'failed')
        self.assertEqual(restarted.claim()['id'], second)
        restarted.finish(second, {'paused': False})
        restarted.recover()
        self.assertEqual(restarted.command(second)['status'], 'success')
        self.assertIsNone(restarted.claim())

    def test_concurrent_claims_deliver_a_command_only_once(self):
        ident = self.store.submit('sync.pause', {})['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim(), range(2)))
        self.assertEqual([row['id'] for row in results if row], [ident])
        self.assertEqual(len([row for row in results if row is None]), 1)

    def test_review_is_private_bounded_by_age_and_not_replayed(self):
        ident = self.store.submit('review.preview', {'abs_id': 'book'})['id']
        self.store.claim(); self.store.finish(ident, {'can_approve': True}, private={'proof': 'PRIVATE-PROOF'})
        self.assertEqual(self.store.review(ident), {'proof': 'PRIVATE-PROOF'})
        with patch('admin_store.time.time', return_value=time.time() + 1801):
            with self.assertRaisesRegex(ValueError, 'abgelaufen'):
                self.store.review(ident)
        self.assertIsNone(self.store.claim())

    def test_pause_and_publication_do_not_touch_sync_baselines(self):
        before = self.saved_state()
        self.store.pause(True); self.assertTrue(self.store.paused())
        self.store.publish({'summary': {'healthy': True}}, {'revision': 3, 'profiles': []})
        self.assertFalse(self.store.status()['stale'])
        self.assertEqual(self.store.list_connections()['revision'], 3)
        with patch('admin_store.time.time', return_value=time.time() + 181):
            self.assertFalse(self.store.status()['summary']['healthy'])
        self.store.pause(False); self.assertFalse(self.store.paused())
        self.assertEqual(self.saved_state(), before)


class CloseClient:
    def close(self):
        self.closed = True


class FakeJF(CloseClient):
    def __init__(self):
        self.calls = []
    def items(self, **kwargs):
        self.calls.append(kwargs)
        return [] if 'ids' in kwargs else [{'Id': 'audio'}]


class CandidateBridge:
    instances = []
    def __init__(self, root, config, credentials):
        for key in ('abs_url', 'abs_version', 'bo_url'):
            assert config[key]
        for key in ('abs_token', 'bo_username', 'bo_password'):
            assert credentials[key]
        self.root, self.config = Path(root), copy.deepcopy(config)
        self.credentials = copy.deepcopy(credentials)
        self.abs, self.bo = CloseClient(), CloseClient()
        self.jellyfin = FakeJF() if config.get('jellyfin', {}).get('enabled') else None
        self.db = sqlite3.connect(self.root / 'state.sqlite')
        self.reads = []
        self.instances.append(self)
    def guard(self):
        pass
    def list_mappings(self):
        return copy.deepcopy(self.config['mappings'])
    def read_pair(self, mapping):
        self.reads.append(copy.deepcopy(mapping))
        return {}, {}
    def load(self, key):
        row = self.db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}


class RuntimeTests(Fixture):
    def setUp(self):
        super().setUp(); CandidateBridge.instances = []
        self.admin = runtime.Administration(self.root, self.key, self.config, self.credentials, CandidateBridge)
        self.admin.identities = {
            'audiobookshelf': dict(ok=True, user_id='abs-user', username='reader', version='2.37.1', library_ids=['library']),
            'bookorbit': dict(ok=True, user_id='1', username='reader', library_ids=[1]),
            'jellyfin': dict(ok=True, server_id='jf-server', user_id='a' * 32, username='Reader', version='10.11')}

    def compatibility(self, kind, *, config=None, credentials=None, result=None):
        return self.admin.compatible(kind, self.config, self.credentials,
            copy.deepcopy(self.config if config is None else config),
            copy.deepcopy(self.credentials if credentials is None else credentials),
            copy.deepcopy(self.admin.identities[kind] if result is None else result))

    def test_different_authenticated_account_or_server_is_rejected_before_mapping(self):
        for kind, key in (('audiobookshelf', 'user_id'), ('bookorbit', 'user_id'), ('jellyfin', 'server_id'),
                          ('jellyfin', 'user_id'), ('bookorbit', 'username')):
            with self.subTest(kind=kind, key=key):
                result = dict(self.admin.identities[kind], **{key: 'different'})
                self.assertFalse(self.compatibility(kind, result=result))
        self.assertEqual(CandidateBridge.instances, [])

    def test_matching_accounts_validate_every_mapping_on_copy_without_resetting_state(self):
        before = self.saved_state()
        changed = copy.deepcopy(self.config); changed['bo_url'] = 'http://verified-alias'
        self.assertTrue(self.compatibility('bookorbit', config=changed))
        candidate = CandidateBridge.instances[-1]
        self.assertEqual(candidate.reads, self.config['mappings'])
        self.assertNotEqual(candidate.root, self.root)
        self.assertFalse(candidate.root.exists())
        self.assertTrue(candidate.abs.closed and candidate.bo.closed and candidate.jellyfin.closed)
        self.assertEqual(self.saved_state(), before)

    def test_same_pinned_settings_allow_secret_rotation_while_a_title_is_blocked(self):
        before = self.saved_state()
        credentials = dict(self.credentials, bo_password='NEW-PRIVATE-PASSWORD')
        with patch.object(CandidateBridge, 'read_pair', side_effect=RuntimeError('book requires review')):
            self.assertTrue(self.compatibility('bookorbit', credentials=credentials))
        self.assertEqual(CandidateBridge.instances, [])
        self.assertEqual(self.saved_state(), before)
        changed_account = dict(self.admin.identities['bookorbit'], user_id='another-user')
        self.assertFalse(self.compatibility('bookorbit', credentials=credentials, result=changed_account))

    def test_scope_expansion_still_validates_all_existing_mappings(self):
        changed = copy.deepcopy(self.config); changed['auto_discovery']['bo_library_ids'] = [1, 2]
        result = dict(self.admin.identities['bookorbit'], library_ids=[1, 2])
        self.assertTrue(self.compatibility('bookorbit', config=changed, result=result))
        self.assertEqual(CandidateBridge.instances[-1].reads, self.config['mappings'])

    def test_actual_mapping_failure_refuses_matching_integer_account_ids(self):
        changed = copy.deepcopy(self.config); changed['bo_url'] = 'http://different-instance'
        with patch.object(CandidateBridge, 'read_pair', side_effect=ValueError('fingerprint differs')):
            with self.assertRaises(ValueError):
                self.compatibility('bookorbit', config=changed)
        self.assertTrue(CandidateBridge.instances[-1].bo.closed)

    def test_without_mappings_integer_account_collision_cannot_authorize_new_endpoint(self):
        for kind, key in (('bookorbit', 'bo_url'), ('audiobookshelf', 'abs_url')):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(self.config); changed['mappings'] = []
                changed[key] = 'http://unrelated-instance'
                self.assertFalse(self.compatibility(kind, config=changed))
                changed[key] = self.config[key] + '/'
                self.assertTrue(self.compatibility(kind, config=changed))

    def test_required_reactivation_validation_fills_other_disabled_endpoint_only_in_copy(self):
        cfg, credentials = copy.deepcopy(self.config), copy.deepcopy(self.credentials)
        cfg['abs_url'] = 'http://verified-abs-alias'
        cfg.pop('bo_url'); cfg['auto_discovery'].pop('bo_library_ids')
        credentials.pop('bo_username'); credentials.pop('bo_password')
        before = copy.deepcopy((cfg, credentials))
        self.assertTrue(self.compatibility('audiobookshelf', config=cfg, credentials=credentials))
        candidate = CandidateBridge.instances[-1]
        self.assertEqual(candidate.config['bo_url'], self.config['bo_url'])
        self.assertEqual(candidate.credentials['bo_password'], self.credentials['bo_password'])
        self.assertEqual((cfg, credentials), before)

    def test_scope_narrowing_is_refused_before_mapping(self):
        self.assertFalse(self.compatibility('bookorbit', result=dict(self.admin.identities['bookorbit'], library_ids=[])))
        self.assertFalse(self.compatibility('audiobookshelf', result=dict(self.admin.identities['audiobookshelf'], library_ids=['other'])))
        self.assertEqual(CandidateBridge.instances, [])

    def test_persisted_auto_ebook_mappings_are_checked_for_jellyfin_changes(self):
        self.put('_ebook_discovery', {'mappings': [{'jf_id': 'b' * 32, 'jf_path': '/books/known.epub'}]})
        before = self.saved_state()
        changed = copy.deepcopy(self.config); changed['jellyfin']['url'] = 'http://verified-jf-alias'
        with patch('jellyfin_audio.bind', return_value=[{'id': 'audio'}]):
            self.assertFalse(self.compatibility('jellyfin', config=changed))
        self.assertIn({'ids': ['b' * 32], 'types': 'Book'}, CandidateBridge.instances[-1].jellyfin.calls)
        self.assertEqual(self.saved_state(), before)

    def test_connection_callback_exception_is_not_published(self):
        ident = self.admin.store.submit('connection.test', dict(expected_revision=0, id='bookorbit-default'))['id']
        with patch('connection_manager.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.2', 80))]), \
             patch('admin_runtime.probe', side_effect=RuntimeError('PRIVATE-RESPONSE-SECRET')):
            self.assertTrue(self.admin.process_one(None))
        command = self.admin.store.command(ident)
        self.assertEqual(command['status'], 'failed')
        self.assertNotIn('PRIVATE-RESPONSE-SECRET', json.dumps(self.admin.store.events()))
        self.assertNotIn('PRIVATE-BO-PASSWORD', json.dumps(self.admin.store.events()))
        self.assertFalse(self.admin.process_one(None))

    def test_review_exception_and_arbitrary_private_top_level_are_not_public(self):
        ident = self.admin.store.submit('review.preview', {'abs_id': 'book'})['id']
        with patch('manual_review.preview', side_effect=RuntimeError('PRIVATE-RESPONSE-SECRET')):
            self.assertTrue(self.admin.process_one(object()))
        self.assertNotIn('PRIVATE-RESPONSE-SECRET', json.dumps(self.admin.store.command(ident)))
        source = {'file_id': 1, 'path': '/books/source.epub', 'size': 123, 'sha256': 'a' * 64}
        record = {'files': {'source': source}, 'review_digest': 'digest', 'can_approve': True,
                  'credentials': {'password': 'PRIVATE-RECORD-SECRET'}}
        output = runtime.public_review('command-id', record)
        self.assertEqual(output['files'], [dict(source, role='source')])
        self.assertNotIn('PRIVATE-RECORD-SECRET', json.dumps(output))
        self.assertEqual(record['files'], {'source': source})
        record['files']['source']['private_token'] = 'PRIVATE-FILE-SECRET'
        self.assertNotIn('PRIVATE-FILE-SECRET', json.dumps(runtime.public_review('command', record)))

    def test_review_proof_file_fallback_normalizes_only_display_values(self):
        record = {'proof': {'source': {'pin': {'overlay_path': '/books/x.epub', 'overlay_size': 20},
                                    'sha256': 'a' * 64, 'private_token': 'SECRET'}}}
        output = runtime.public_review('command', record)
        self.assertEqual(output['files'], [{'role': 'source', 'path': '/books/x.epub', 'size': 20, 'sha256': 'a' * 64}])
        self.assertNotIn('SECRET', json.dumps(output))

    def test_optional_jellyfin_disable_blocks_interrupted_audio_and_ebook_work(self):
        self.config['jellyfin']['enabled'] = False
        for key in ('_jellyfin_audio:book', '_jellyfin_ebook:7'):
            with self.subTest(key=key):
                self.put(key, {'pending': {'stage': 'sending', 'goal': {'position': 90}}, 'baseline': {'position': 60}})
                before = self.saved_state()
                self.assertIsNotNone(runtime.participant_block(self.root, self.config))
                self.assertEqual(self.saved_state(), before)
                self.put(key, {'baseline': {'position': 60}})
        self.assertIsNone(runtime.participant_block(self.root, self.config))

    def test_optional_jellyfin_disable_blocks_pending_operation_without_reconciliation(self):
        self.config['jellyfin']['enabled'] = False
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?)', ('op', 'book', '{}', 'pending'))
        before = self.saved_state()
        self.assertIsNotNone(runtime.participant_block(self.root, self.config))
        self.assertEqual(self.saved_state(), before)
        self.config['jellyfin']['enabled'] = True
        self.assertIsNone(runtime.participant_block(self.root, self.config))

    def test_resume_does_not_remove_pending_participant_block(self):
        self.admin.store.pause(True)
        self.put('_jellyfin_audio:book', {'pending': {'goal': {'position': 90}}})
        ident = self.admin.store.submit('sync.resume', {})['id']
        self.assertTrue(self.admin.process_one(None))
        self.assertEqual(self.admin.store.command(ident)['status'], 'success')
        self.assertFalse(self.admin.store.paused())
        cfg = copy.deepcopy(self.config); cfg['jellyfin']['enabled'] = False
        self.assertIsNotNone(runtime.participant_block(self.root, cfg))

    def test_main_loop_applies_jellyfin_disable_before_any_next_cycle(self):
        self.put('_jellyfin_audio:book', {'pending': {'stage': 'sending'}, 'baseline': {'position': 90}})
        before = self.saved_state()
        ident = self.admin.store.submit('connection.deactivate',
            {'expected_revision': 0, 'id': 'jellyfin-default'})['id']
        args = SimpleNamespace(key=self.key, apply=True, settle=0)
        with patch.object(CandidateBridge, 'cycle', create=True) as cycle, \
             patch('admin_runtime.probe', side_effect=lambda kind, *_: copy.deepcopy(self.admin.identities[kind])), \
             patch('admin_runtime.time.sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run_admin_loop(args, self.root, self.config, self.credentials, CandidateBridge)
        cycle.assert_not_called()
        self.assertEqual(self.admin.store.command(ident)['status'], 'success')
        self.assertFalse(self.admin.manager.effective()[0]['jellyfin']['enabled'])
        self.assertNotIn('jellyfin_token', self.admin.manager.effective()[1])
        self.assertEqual(json.loads((self.root / 'last-run.json').read_text())['mode'], 'paused')
        self.assertEqual(self.saved_state(), before)


class ProbeTests(unittest.TestCase):
    def test_abs_probe_strips_token_from_api_me_and_closes_client(self):
        class ABS(CloseClient):
            def version(self): return '2.37.1'
            def _request(self, method, path, label):
                return {'id': 'user', 'username': 'reader', 'token': 'ME-PRIVATE-TOKEN'}
            def libraries(self): return [{'id': 'library'}]
        client = ABS()
        with patch('admin_runtime.ABSClient', return_value=client):
            result = runtime.probe('audiobookshelf', {'url': 'http://abs', 'version': '2.37.1', 'library_id': 'library'}, {'token': 'INPUT-PRIVATE-TOKEN'})
        self.assertTrue(result['ok']); self.assertTrue(client.closed)
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(result))

    def test_bookorbit_probe_uses_authenticated_user_and_exact_library(self):
        class BO(CloseClient):
            user = {'id': 9, 'username': 'verified', 'private_token': 'SECRET'}
            def _login(self): pass
            def library(self, ident): return {'id': ident}
        client = BO()
        with patch('admin_runtime.BOClient', return_value=client):
            result = runtime.probe('bookorbit', {'url': 'http://bo', 'username': 'submitted', 'library_ids': [1]}, {'password': 'SECRET'})
        self.assertEqual(result['user_id'], '9'); self.assertEqual(result['username'], 'verified')
        self.assertNotIn('SECRET', json.dumps(result)); self.assertTrue(client.closed)

    def test_transport_exception_is_fixed_failure_and_client_closed(self):
        class BO(CloseClient):
            def _login(self): raise RuntimeError('PASSWORD SECRET response')
        client = BO()
        with patch('admin_runtime.BOClient', return_value=client):
            self.assertEqual(runtime.probe('bookorbit', {'url': 'http://bo', 'username': 'reader', 'library_ids': [1]}, {'password': 'SECRET'}), {'ok': False})
        self.assertTrue(client.closed)


if __name__ == '__main__':
    unittest.main()
