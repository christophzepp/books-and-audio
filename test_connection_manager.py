import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from connection_manager import ConnectionManager, ConnectionManagerError, SCHEMAS, validate_url


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = Fernet.generate_key()
        self.config = dict(abs_url='http://abs:80', abs_version='2.37.1', bo_url='http://bo:3000',
            ntfy_url='https://notify.example/books', mappings=[{'abs_id': 'book', 'bo_id': 7}],
            auto_discovery=dict(enabled=True, abs_library_id='abs-library', bo_library_ids=[1, 2], stable_seconds=300),
            jellyfin=dict(enabled=True, url='http://jf:8096', user_id='jf-user', username='Reader',
                version='10.11.0', audio_library_id='audio-library', ebooks=[{'file_id': 4}],
                auto_ebooks=dict(enabled=True, bo_library_id=2, jf_library_id='books',
                                bo_root='/books', jf_root='/media/books', local_root='/books')))
        self.credentials = dict(abs_token='ABS-SUPER-SECRET', bo_username='reader', bo_password='BO-SUPER-SECRET',
            jellyfin_token='JF-SUPER-SECRET', ntfy_authorization='Bearer NTFY-SUPER-SECRET', unrelated='retained')
        self.manager = ConnectionManager(self.root, self.config, self.credentials, self.key)
        dns = patch('connection_manager.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.4', 80))])
        dns.start(); self.addCleanup(dns.stop)
        self.probes, self.compatibility = [], []

    def seed(self):
        return self.manager.seed()

    def probe(self, kind, settings, secrets):
        self.probes.append((kind, settings, secrets))
        return dict(ok=True, server_id='same-server', user_id='same-user', version='1',
                    ignored_token='NEVER-EXPOSE-PROBE')

    def compatible(self, *args):
        self.compatibility.append(args)
        return True

    def mutate(self, action, **payload):
        payload.setdefault('expected_revision', self.manager.public()['revision'])
        return self.manager.mutate(action, payload, self.probe, self.compatible)

    def create_bo(self):
        return self.mutate('create', kind='bookorbit', name='Second BookOrbit',
            settings=dict(url='http://second-bo:3000', username='reader', library_ids=[1, 2]),
            secrets=dict(password='SECOND-SECRET'))['profile_id']

    def test_constructor_and_reads_never_create_files(self):
        self.assertFalse((self.root / 'control').exists())
        public = self.manager.public()
        self.assertEqual(public['revision'], 0)
        self.assertEqual(len(public['profiles']), 4)
        self.assertEqual(self.manager.effective(), (self.config, self.credentials, None))
        self.assertFalse((self.root / 'control').exists())

    def test_seed_is_idempotent_private_encrypted_and_preserves_legacy(self):
        original = copy.deepcopy((self.config, self.credentials))
        first = self.seed(); raw = self.manager.path.read_bytes()
        self.assertEqual(self.seed(), first)
        self.assertEqual(self.manager.path.read_bytes(), raw)
        self.assertEqual((self.config, self.credentials), original)
        self.assertEqual(stat.S_IMODE(self.manager.path.stat().st_mode), 0o600)
        state = json.loads(Fernet(self.key).decrypt(raw))
        self.assertEqual(state['effective_credentials'], self.credentials)
        self.assertEqual(state['base_credentials'], {'unrelated': 'retained'})
        for secret in self.credentials.values():
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual({p['id'] for p in first['profiles']}, {kind + '-default' for kind in SCHEMAS if kind not in {'bookbridge', 'pinepods'}})

    def test_public_contains_only_secret_presence_and_editable_schemas(self):
        public = self.seed(); serialized = json.dumps(public)
        for key in ('abs_token', 'bo_password', 'jellyfin_token', 'ntfy_authorization'):
            self.assertNotIn(self.credentials[key], serialized)
        bo = next(p for p in public['profiles'] if p['kind'] == 'bookorbit')
        self.assertEqual(bo['settings']['username'], 'reader')
        self.assertEqual(bo['has_secrets'], {'password': True})
        self.assertNotIn('secrets', bo)
        self.assertEqual(next(f for f in public['schemas']['bookorbit']['fields'] if f['key'] == 'library_ids')['type'], 'json')
        public['profiles'][0]['settings']['url'] = 'http://changed'
        self.assertEqual(self.manager.effective()[0]['abs_url'], self.config['abs_url'])

    def test_mutation_requires_explicit_seed(self):
        result = self.mutate('deactivate', id='audiobookshelf-default')
        self.assertEqual(result['code'], 'connections_not_initialized')
        self.assertFalse(self.manager.path.exists())

    def test_create_is_saved_inactive_and_preserves_active_connection(self):
        self.seed(); identity = self.create_bo()
        public = self.manager.public()
        self.assertEqual(public['revision'], 1)
        self.assertFalse(next(p for p in public['profiles'] if p['id'] == identity)['active'])
        self.assertEqual(self.manager.effective()[0]['bo_url'], 'http://bo:3000')
        self.assertEqual(self.probes, [])
        self.assertEqual(self.mutate('create', kind='ntfy', name='Draft', settings={}, secrets={})['ok'], True)

    def test_activate_requires_fresh_probe_and_explicit_compatibility(self):
        self.seed(); identity = self.create_bo()
        self.assertTrue(self.mutate('test', id=identity)['ok'])
        self.assertEqual(self.manager.public()['revision'], 1)
        self.assertTrue(self.mutate('activate', id=identity)['ok'])
        self.assertEqual(len(self.probes), 2)
        self.assertEqual(len(self.compatibility), 1)
        self.assertNotIn('ignored_token', self.compatibility[0][-1])
        self.assertEqual(self.manager.effective()[1]['bo_password'], 'SECOND-SECRET')
        self.assertEqual(sum(p['active'] for p in self.manager.public()['profiles'] if p['kind'] == 'bookorbit'), 1)

    def test_active_update_checks_entire_candidate_and_preserves_blank_secret(self):
        self.seed()
        result = self.mutate('update', id='bookorbit-default', name='Changed label',
                             settings={'library_ids': [1]}, secrets={'password': ''})
        self.assertTrue(result['ok'])
        self.assertEqual(self.probes[0][2]['password'], self.credentials['bo_password'])
        args = self.compatibility[0]
        self.assertEqual(args[1]['auto_discovery']['bo_library_ids'], [1, 2])
        self.assertEqual(args[3]['auto_discovery']['bo_library_ids'], [1])
        self.assertEqual(args[3]['mappings'], self.config['mappings'])
        self.assertEqual(self.manager.effective()[1]['bo_password'], self.credentials['bo_password'])

    def test_failed_probe_never_changes_store_or_echoes_exception(self):
        self.seed(); raw = self.manager.path.read_bytes()
        def failed(*_):
            raise RuntimeError('PASSWORD SECRET https://name:token@host')
        result = self.manager.mutate('update', dict(expected_revision=0, id='bookorbit-default',
            secrets={'password': 'NEW-PASSWORD'}), failed, self.compatible)
        self.assertEqual(result, dict(ok=False, code='probe_failed', revision=0))
        self.assertEqual(self.manager.path.read_bytes(), raw)
        self.assertEqual(self.compatibility, [])

    def test_compatibility_failure_is_sanitized_and_atomic(self):
        self.seed(); raw = self.manager.path.read_bytes()
        def failed(*_):
            raise ValueError('private response contains SECRET')
        result = self.manager.mutate('update', dict(expected_revision=0, id='bookorbit-default',
            settings={'url': 'http://other'}), self.probe, failed)
        self.assertEqual(result['code'], 'incompatible_connection')
        self.assertEqual(self.manager.path.read_bytes(), raw)
        for callback in (None, lambda *_: {'ok': True}, lambda *_: 1):
            result = self.manager.mutate('activate', dict(expected_revision=0, id='bookorbit-default'), self.probe, callback)
            self.assertEqual(result['code'], 'incompatible_connection')

    def test_required_delete_removes_inherited_secret_after_restart(self):
        self.seed()
        self.assertTrue(self.mutate('delete', id='bookorbit-default')['ok'])
        cfg, credentials, reason = self.manager.effective()
        self.assertEqual(reason, 'required_connection_disabled')
        self.assertNotIn('bo_url', cfg)
        self.assertNotIn('bo_username', credentials); self.assertNotIn('bo_password', credentials)
        restarted = ConnectionManager(self.root, self.config, self.credentials, self.key)
        self.assertEqual(restarted.effective(), (cfg, credentials, reason))
        state = json.loads(Fernet(self.key).decrypt(self.manager.path.read_bytes()))
        self.assertNotIn('BO-SUPER-SECRET', json.dumps(state))
        backup = next((self.root / 'control').glob('connections.rev-*.enc'))
        self.assertIn('BO-SUPER-SECRET', Fernet(self.key).decrypt(backup.read_bytes()).decode())

    def test_required_deactivate_pauses_and_reactivation_needs_validation(self):
        self.seed(); self.mutate('deactivate', id='audiobookshelf-default')
        self.assertNotIn('abs_token', self.manager.effective()[1])
        self.assertIsNotNone(self.manager.effective()[2])
        self.assertTrue(self.mutate('activate', id='audiobookshelf-default')['ok'])
        self.assertIsNone(self.manager.effective()[2])

    def test_optional_disable_removes_effective_credentials_keeps_ebook_mappings(self):
        self.seed(); self.mutate('deactivate', id='jellyfin-default')
        self.mutate('delete', id='ntfy-default')
        cfg, creds, reason = self.manager.effective()
        self.assertIsNone(reason)
        self.assertFalse(cfg['jellyfin']['enabled'])
        self.assertEqual(cfg['jellyfin']['ebooks'], self.config['jellyfin']['ebooks'])
        self.assertEqual(cfg['jellyfin']['auto_ebooks'], self.config['jellyfin']['auto_ebooks'])
        self.assertNotIn('jellyfin_token', creds); self.assertNotIn('ntfy_authorization', creds)
        self.assertNotIn('ntfy_url', cfg)

    def test_compare_and_swap_rejects_stale_other_instance_before_probe(self):
        self.seed()
        other = ConnectionManager(self.root, self.config, self.credentials, self.key)
        self.mutate('deactivate', id='ntfy-default')
        result = other.mutate('test', dict(expected_revision=0, id='bookorbit-default'), self.probe, self.compatible)
        self.assertEqual(result['code'], 'stale_revision')
        self.assertEqual(self.probes, [])
        self.assertEqual(self.manager.public()['revision'], 1)

    def test_backup_is_versioned_private_and_failed_replace_keeps_previous(self):
        self.seed(); old = self.manager.path.read_bytes()
        with patch('connection_manager.os.replace', side_effect=OSError('SECRET')):
            result = self.mutate('deactivate', id='ntfy-default')
        self.assertFalse(result['ok']); self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(self.manager.path.read_bytes(), old)
        backup = self.root / 'control' / ('connections.rev-' + '0' * 20 + '.enc')
        self.assertEqual(backup.read_bytes(), old)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(self.mutate('deactivate', id='ntfy-default')['ok'])
        self.assertEqual(len(list((self.root / 'control').glob('connections.rev-*.enc'))), 1)
        self.assertFalse(list((self.root / 'control').glob('.connections-*')))

    def test_missing_seeded_store_never_restores_legacy_credentials(self):
        self.seed(); self.mutate('delete', id='bookorbit-default'); self.manager.path.unlink()
        restarted = ConnectionManager(self.root, self.config, self.credentials, self.key)
        with self.assertRaisesRegex(ConnectionManagerError, '^connection_store_unavailable$'):
            restarted.effective()
        with self.assertRaisesRegex(ConnectionManagerError, '^connection_seed_failed$'):
            restarted.seed()
        self.assertFalse(self.manager.path.exists())

    def test_corrupt_store_wrong_key_and_symlink_fail_without_secret_details(self):
        self.seed()
        other = ConnectionManager(self.root, self.config, self.credentials, Fernet.generate_key())
        with self.assertRaisesRegex(ConnectionManagerError, '^connection_store_unavailable$'):
            other.public()
        self.manager.path.chmod(0o644)
        with self.assertRaisesRegex(ConnectionManagerError, '^connection_store_unavailable$'):
            self.manager.public()
        self.manager.path.unlink(); self.manager.path.symlink_to('/dev/null')
        with self.assertRaisesRegex(ConnectionManagerError, '^connection_store_unavailable$'):
            self.manager.effective()

    def test_incomplete_profiles_can_be_saved_but_never_tested_or_activated(self):
        self.seed()
        profile = self.mutate('create', kind='bookorbit', name='Draft')['profile_id']
        self.assertEqual(self.mutate('test', id=profile)['code'], 'incomplete_profile')
        self.assertEqual(self.mutate('activate', id=profile)['code'], 'incomplete_profile')
        self.assertEqual(self.probes, [])

    def test_unsupported_fields_and_invalid_scopes_fail_without_mutation(self):
        self.seed(); raw = self.manager.path.read_bytes()
        for payload in ({'settings': {'password': 'SECRET'}}, {'secrets': {'unknown': ''}},
                        {'settings': {'library_ids': [True]}}, {'settings': {'library_ids': [1, 1]}},
                        {'settings': {'library_ids': ['1']}}, {'settings': {'url': 'http://user:SECRET@host'}},
                        {'active': True}):
            result = self.mutate('update', id='bookorbit-default', **payload)
            self.assertFalse(result['ok']); self.assertNotIn('SECRET', json.dumps(result))
            self.assertEqual(self.manager.path.read_bytes(), raw)
        result = self.mutate('create', kind='ntfy', name='Draft', active=True)
        self.assertFalse(result['ok'])

    def test_non_secret_values_never_accept_control_characters(self):
        self.seed()
        for payload in ({'name': 'x\nSECRET'}, {'settings': {'username': 'x\rSECRET'}},
                        {'secrets': {'password': 'x\nSECRET'}}):
            self.assertFalse(self.mutate('update', id='bookorbit-default', **payload)['ok'])

    def test_metadata_dns_alias_blocks_probe(self):
        self.seed(); profile = self.create_bo()
        with patch('connection_manager.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('169.254.169.254', 80))]):
            self.assertEqual(self.mutate('activate', id=profile)['code'], 'unsafe_address')
        self.assertEqual(self.probes, [])

    def test_probe_identity_must_be_bounded_primitive_fields(self):
        self.seed()
        for result in ({'ok': True, 'server_id': {'token': 'SECRET'}}, {'ok': 1},
                       {'ok': True, 'library_ids': [False]}, {'ok': True, 'user_id': 'x' * 513}):
            output = self.manager.mutate('test', dict(expected_revision=0, id='bookorbit-default'),
                                         lambda *_: result, self.compatible)
            self.assertEqual(output['code'], 'probe_failed')
            self.assertNotIn('SECRET', json.dumps(output))

    def test_callback_cannot_mutate_internal_data(self):
        self.seed()
        def probe(kind, settings, secrets):
            settings['url'] = 'http://unwanted'; secrets['password'] = 'unwanted'
            return {'ok': True}
        def compatible(kind, old_cfg, old_creds, new_cfg, new_creds, proof):
            new_cfg['mappings'].clear(); new_creds.clear(); proof['ok'] = False
            return True
        result = self.manager.mutate('activate', dict(expected_revision=0, id='bookorbit-default'), probe, compatible)
        self.assertTrue(result['ok'])
        self.assertEqual(self.manager.effective(), (self.config, self.credentials, None))


class URLTests(unittest.TestCase):
    def test_private_docker_tailscale_and_valid_base_paths_allowed(self):
        for url in ('http://bookorbit:3000', 'http://10.0.0.1', 'http://192.168.1.8:80/base',
                    'https://100.79.5.2', 'http://[fd7a:115c:a1e0::1]', 'http://localhost', 'http://127.0.0.1'):
            with self.subTest(url=url):
                self.assertEqual(validate_url(url), url)

    def test_metadata_odd_addresses_and_credential_urls_rejected(self):
        for url in ('http://169.254.169.254/latest', 'http://[fe80::1]', 'http://[fd00:ec2::254]',
                    'http://[::ffff:169.254.169.254]', 'http://100.100.100.200', 'http://168.63.129.16',
                    'http://metadata.google.internal', 'http://metadata', 'http://0.0.0.0',
                    'http://[::]', 'http://224.0.0.1', 'http://2130706433', 'http://0177.0.0.1',
                    'http://0x7f000001', 'http://user:secret@host', 'http://host?secret=x',
                    'http://host#token', 'http://host\\@other', 'file:///etc/passwd',
                    'http://host:0', 'http://host:65536', 'http://host\n', 'http://[fe80::1%25eth0]'):
            with self.subTest(url=url):
                with self.assertRaises(ConnectionManagerError):
                    validate_url(url)


if __name__ == '__main__':
    unittest.main()

class ProviderTests(ConnectionTests):
    def setup_bb(self):
        self.seed()
        result=self.mutate('create',kind='bookbridge',name='BookBridge',
            settings={'url':'http://bb:5757','username':'reader','version':'7.11.0'},secrets={'password':'BB-PRIVATE'})
        self.assertTrue(self.mutate('activate',id=result['profile_id'])['ok'])
        return result['profile_id']
    def request_provider(self,provider='bookbridge',check=None,apply=False):
        return self.manager.provider({'expected_revision':self.manager.public()['revision'],'provider':provider},check or (lambda *_:{'ok':True}),apply=apply)
    def test_optional_bookbridge_is_encrypted_and_does_not_switch_mode(self):
        self.setup_bb()
        self.assertEqual(self.manager.public()['mapping_provider'],'bookorbit')
        self.assertNotIn('BB-PRIVATE',json.dumps(self.manager.public()))
        self.assertNotIn(b'BB-PRIVATE',self.manager.path.read_bytes())
    def test_preview_then_switch_without_bookorbit_preserves_legacy_mappings(self):
        self.setup_bb(); before=self.manager.path.read_bytes()
        self.assertTrue(self.request_provider()['ok']); self.assertEqual(self.manager.path.read_bytes(),before)
        self.assertTrue(self.request_provider(apply=True)['ok'])
        self.mutate('deactivate',id='bookorbit-default')
        cfg,creds,reason=self.manager.effective()
        self.assertIsNone(reason); self.assertEqual(cfg['mapping_provider'],'bookbridge')
        self.assertNotIn('bo_password',creds); self.assertEqual(cfg['mappings'],self.config['mappings'])
        self.assertEqual(creds['bookbridge_password'],'BB-PRIVATE')
    def test_missing_provider_or_failed_preview_never_switches(self):
        self.seed(); self.assertEqual(self.request_provider(apply=True)['code'],'required_connection_disabled')
        self.setup_bb(); before=self.manager.path.read_bytes()
        self.assertEqual(self.request_provider(check=lambda *_:{'ok':False},apply=True)['code'],'provider_not_ready')
        self.assertEqual(self.manager.path.read_bytes(),before)
    def test_queued_provider_change_rejects_stale_revision(self):
        self.setup_bb()
        value=self.manager.provider({'expected_revision':0,'provider':'bookbridge'},lambda *_:{'ok':True},apply=True)
        self.assertEqual(value['code'],'stale_revision')
    def test_exception_never_exposes_credentials(self):
        self.setup_bb()
        def bad(*_): raise RuntimeError('BB-PRIVATE')
        value=self.request_provider(check=bad,apply=True)
        self.assertNotIn('BB-PRIVATE',json.dumps(value)); self.assertFalse(value['ok'])
