import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import watch_deployment as watcher


IMAGE = 'sha256:' + 'f' * 64


def config():
    return {'folder_access_roots': [{'worker': '/audiobooks', 'server': '/srv/example-media/audiobooks'},
                                     {'worker': '/ebooks', 'server': '/srv/example-media/books'}],
            'folder_deployment': {
                'abs': {'container': 'audiobookshelf', 'image': IMAGE},
                'jf': {'container': 'jellyfin', 'image': IMAGE},
                'worker': {'container': 'book-progress-sync', 'image': IMAGE}}}


def bind(source, destination, rw=False):
    return {'Type': 'bind', 'Source': source, 'Destination': destination, 'RW': rw}


def container(name, mounts):
    return {'Name': '/' + name, 'Image': IMAGE, 'State': {'Status': 'running'}, 'Mounts': mounts}


def containers():
    return [container('audiobookshelf', [bind('/srv/example-media/audiobooks', '/audiobooks', True),
                                        bind('/srv/example-stacks/abs/config', '/config', True)]),
            container('jellyfin', [bind('/srv/example-media', '/data'),
                                  bind('/srv/example-stacks/jellyfin/secrets', '/config', True)]),
            container('book-progress-sync', [bind('/srv/example-media/audiobooks', '/audiobooks'),
                                              bind('/srv/example-media/books', '/books'),
                                              bind('/srv/example-stacks/book-progress-sync/config', '/config')])]


class FolderDeploymentTests(unittest.TestCase):
    def test_projects_broad_bind_to_only_allowed_media_and_hides_other_mounts(self):
        result = watcher.validate_folder_containers(containers(), config())
        self.assertEqual(result['jf'], [
            {'source': '/srv/example-media/audiobooks', 'destination': '/data/audiobooks', 'rw': False},
            {'source': '/srv/example-media/books', 'destination': '/data/books', 'rw': False}])
        self.assertEqual(result['abs'], [{'source': '/srv/example-media/audiobooks', 'destination': '/audiobooks', 'rw': True}])
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('/opt', json.dumps(result))
        self.assertNotIn('/config', json.dumps(result))

    def test_subfolder_mount_gets_narrow_receipt(self):
        rows = containers()
        rows[0]['Mounts'][0] = bind('/srv/example-media/audiobooks/fiction', '/fiction')
        result = watcher.validate_folder_containers(rows, config())
        self.assertEqual(result['abs'], [{'source': '/srv/example-media/audiobooks/fiction', 'destination': '/fiction', 'rw': False}])

    def test_empty_endpoint_access_does_not_invent_alias(self):
        rows = containers()
        rows[0]['Mounts'] = [bind('/srv/example-stacks/abs', '/config')]
        self.assertEqual(watcher.validate_folder_containers(rows, config())['abs'], [])

    def test_nested_mount_shadowing_is_rejected_including_nonbind(self):
        for mount in (bind('/different', '/data/audiobooks/title'),
                      {'Type': 'volume', 'Source': '/var/lib/docker/volume', 'Destination': '/data/books', 'RW': True},
                      {'Type': 'tmpfs', 'Destination': '/data/audiobooks', 'RW': True}):
            rows = containers()
            rows[1]['Mounts'].append(mount)
            errors = {}
            self.assertNotIn('jf', watcher.validate_folder_containers(rows, config(), errors))
            self.assertIn('Nested', errors['jf'])

    def test_pinned_running_container_required(self):
        for index in range(3):
            for field, value in (('Image', 'sha256:' + 'a' * 64), ('State', {'Status': 'exited'})):
                rows = containers()
                rows[index][field] = value
                errors = {}
                endpoint = ('abs', 'jf', 'worker')[index]
                self.assertNotIn(endpoint, watcher.validate_folder_containers(rows, config(), errors))
                self.assertIn(endpoint, errors)

    def test_worker_readwrite_rejected_but_config_write_irrelevant(self):
        rows = containers()
        rows[2]['Mounts'][0]['RW'] = True
        errors = {}
        self.assertNotIn('worker', watcher.validate_folder_containers(rows, config(), errors))
        self.assertIn('read-only', errors['worker'])
        rows = containers()
        rows[2]['Mounts'][2]['RW'] = True
        watcher.validate_folder_containers(rows, config())

    def test_missing_duplicate_or_unexpected_containers_rejected(self):
        missing_errors = {}
        self.assertNotIn('worker', watcher.validate_folder_containers(containers()[:-1], config(), missing_errors))
        self.assertIn('worker', missing_errors)
        for rows in (containers() + [containers()[0]],
                     containers()[1:] + [container('bookorbit', [])]):
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_folder_containers(rows, config())

    def test_duplicate_destinations_rejected(self):
        rows = containers()
        rows[0]['Mounts'].append(copy.deepcopy(rows[0]['Mounts'][0]))
        errors = {}
        self.assertNotIn('abs', watcher.validate_folder_containers(rows, config(), errors))
        self.assertIn('Duplicate', errors['abs'])

    def test_ambiguous_or_unsafe_policy_rejected(self):
        changes = [dict(folder_access_roots=['/']),
                   dict(folder_access_roots=['/srv/example-media/books', '/srv/example-media/books/sub']),
                   dict(folder_access_roots=['/srv/example-media/books', '/srv/example-media/books']),
                   dict(folder_deployment={'unknown': {'container': 'jellyfin', 'image': IMAGE}}),
                   dict(folder_deployment={'jf': {'container': 'jellyfin;command', 'image': IMAGE}}),
                   dict(folder_deployment={'jf': {'container': 'jellyfin', 'image': 'latest'}}),
                   dict(folder_deployment={'jf': {'container': 'jellyfin', 'image': IMAGE},
                                           'bo': {'container': 'jellyfin', 'image': IMAGE}})]
        for change in changes:
            cfg = config(); cfg.update(change)
            with self.subTest(change=change), self.assertRaises(watcher.DeploymentError):
                watcher.folder_deployment_policy(cfg)

    def test_receipt_is_independent_of_bookorbit_gate(self):
        by_name = {row['Name'][1:]: row for row in containers()}
        runner = Mock(side_effect=lambda args, **kwargs: SimpleNamespace(stdout=json.dumps([by_name[args[-1]]]) if len(args) == 3 and args[-1] in by_name else '[]'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir(); (root / 'state').mkdir()
            (root / 'config/config.json').write_text(json.dumps(config()))
            watcher.main(root, runner, clock=lambda: 4321)
            gate = json.loads((root / 'state/deployment.json').read_text())
        self.assertFalse(gate['ok'])
        self.assertTrue(gate['folder_gate_ok'])
        self.assertEqual(gate['checked_at'], 4321)
        self.assertEqual(set(gate['folder_mounts']), {'abs', 'jf', 'worker'})
        self.assertNotIn('folder_reason', gate)

    def test_failed_receipt_has_no_stale_mounts_or_process_details(self):
        runner = Mock(side_effect=subprocess.CalledProcessError(1, ['secret command'], stderr='secret token'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir(); (root / 'state').mkdir()
            (root / 'config/config.json').write_text(json.dumps(config()))
            (root / 'state/deployment.json').write_text(json.dumps({'folder_gate_ok': True, 'folder_mounts': {'old': []}}))
            watcher.main(root, runner, clock=lambda: 4321)
            gate = json.loads((root / 'state/deployment.json').read_text())
        self.assertTrue(gate['folder_gate_ok'])
        self.assertEqual(gate['folder_mounts'], {})
        self.assertEqual(gate['folder_errors'], dict.fromkeys(('abs', 'jf', 'worker'), 'CalledProcessError'))
        self.assertNotIn('secret', json.dumps(gate))


class FolderAliasGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(mapping_provider='bookbridge', jellyfin={'enabled': True}, folder_mappings=[
            {'id': 'audio', 'kind': 'audiobooks', 'active': True,
             'paths': {'server': '/srv/example-media/audiobooks', 'worker': '/audiobooks',
                       'abs': '/audiobooks', 'bo': '/audiobooks', 'jf': '/data/audiobooks'}}])
        self.gate = {'checked_at': 1000, 'folder_gate_ok': True,
                     'folder_mounts': watcher.validate_folder_containers(containers(), config())}

    def check(self):
        return watcher.validate_folder_aliases(self.cfg, self.gate, now=1000)

    def test_bookbridge_mode_ignores_absent_bookorbit(self):
        self.assertTrue(self.check())

    def test_bookorbit_mode_requires_bookorbit_while_ignoring_inactive_bookbridge(self):
        self.cfg['mapping_provider'] = 'bookorbit'
        self.cfg['folder_mappings'][0]['paths']['bb'] = '/audiobooks'
        with self.assertRaises(watcher.DeploymentError): self.check()
        self.gate['folder_mounts']['bo'] = copy.deepcopy(self.gate['folder_mounts']['abs'])
        self.assertTrue(self.check())

    def test_bookbridge_optional_local_alias_requires_its_own_receipt(self):
        self.cfg['folder_mappings'][0]['paths']['bb'] = '/audio'
        with self.assertRaises(watcher.DeploymentError): self.check()
        self.gate['folder_mounts']['bb'] = [{'source': '/srv/example-media/audiobooks', 'destination': '/audio', 'rw': False}]
        self.assertTrue(self.check())

    def test_false_alias_must_not_point_to_similar_other_files(self):
        for endpoint in ('worker', 'abs', 'jf'):
            original = self.cfg['folder_mappings'][0]['paths'][endpoint]
            self.cfg['folder_mappings'][0]['paths'][endpoint] = original + '/wrong'
            with self.subTest(endpoint=endpoint), self.assertRaises(watcher.DeploymentError): self.check()
            self.cfg['folder_mappings'][0]['paths'][endpoint] = original

    def test_subfolder_aliases_resolve_to_same_host_folder(self):
        self.cfg['folder_mappings'][0]['paths'] = {key: value + '/fiction' for key, value in self.cfg['folder_mappings'][0]['paths'].items()}
        self.assertTrue(self.check())

    def test_ebook_bb_local_alias_checked_without_bookorbit(self):
        self.cfg['folder_mappings'].append({'id': 'ebook', 'kind': 'ebooks', 'active': True,
            'paths': {'server': '/srv/example-media/books', 'worker': '/books', 'bb': '/books'}})
        with self.assertRaises(watcher.DeploymentError): self.check()
        self.gate['folder_mounts']['bb'] = [{'source': '/srv/example-media/books', 'destination': '/books', 'rw': False}]
        self.assertTrue(self.check())

    def test_stale_future_and_nonboolean_gates_refused(self):
        for checked in (849, 1001, float('nan'), True, None):
            self.gate['checked_at'] = checked
            with self.subTest(checked=checked), self.assertRaises(watcher.DeploymentError): self.check()
        self.gate['checked_at'] = 1000
        self.gate['folder_gate_ok'] = 1
        with self.assertRaises(watcher.DeploymentError): self.check()

    def test_missing_alias_and_writable_worker_refused(self):
        del self.cfg['folder_mappings'][0]['paths']['worker']
        with self.assertRaises(watcher.DeploymentError): self.check()
        self.cfg['folder_mappings'][0]['paths']['worker'] = '/audiobooks'
        self.gate['folder_mounts']['worker'][0]['rw'] = True
        with self.assertRaises(watcher.DeploymentError): self.check()

    def test_no_active_audio_is_not_success(self):
        self.cfg['folder_mappings'][0]['active'] = False
        with self.assertRaises(watcher.DeploymentError): self.check()

    def test_legacy_configuration_needs_no_new_receipt(self):
        self.assertTrue(watcher.validate_folder_aliases({}, {}))


if __name__ == '__main__':
    unittest.main()
