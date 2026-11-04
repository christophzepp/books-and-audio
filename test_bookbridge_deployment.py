"""Independent provider gate: no BookOrbit, Docker or server access required."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import watch_deployment as watcher


def config(local_bb=True):
    specs = {}
    for index, name in enumerate(('audiobookshelf', 'book-progress-sync', 'jellyfin')):
        specs[name] = {'image': 'sha256:' + str(index + 1) * 64,
                       'audio_root': '/data/audiobooks' if name == 'jellyfin' else '/audiobooks',
                       'media_source': '/srv/example-media/audiobooks'}
    if local_bb:
        specs['bookbridge'] = {'image': 'sha256:' + '4' * 64}
    return {'bookbridge_deployment': specs}


def containers(local_bb=True):
    result = []
    for name, spec in config(local_bb)['bookbridge_deployment'].items():
        row = {'Name': '/' + name, 'Image': spec['image'], 'State': {'Status': 'running'}}
        if name != 'bookbridge':
            row['Mounts'] = [{'Type': 'bind', 'Source': '/srv/example-media/audiobooks',
                              'Destination': spec['audio_root'], 'RW': name != 'book-progress-sync'},
                             {'Type': 'bind', 'Source': '/srv/example-stacks/' + name,
                              'Destination': '/config', 'RW': True}]
        result.append(row)
    return result


class BookBridgeDeploymentTests(unittest.TestCase):
    def test_complete_gate_works_without_any_bookorbit_policy_or_container(self):
        self.assertNotIn('bookorbit', config()['bookbridge_deployment'])
        watcher.validate_bookbridge_containers(containers(), config())

    def test_optional_remote_bookbridge_still_requires_other_three_containers(self):
        watcher.validate_bookbridge_containers(containers(False), config(False))
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(containers(False))))
        watcher.check_bookbridge_deployment(config(False), runner)
        self.assertEqual(runner.call_args.args[0],
                         ['docker', 'inspect', 'audiobookshelf', 'book-progress-sync', 'jellyfin'])

    def test_missing_policy_does_not_fall_back_to_bookorbit_trust(self):
        with self.assertRaisesRegex(watcher.DeploymentError, 'not configured'):
            watcher.bookbridge_deployment_policy({'deployment': config()['bookbridge_deployment']})

    def test_parent_mount_resolves_same_exact_host_root(self):
        rows = containers()
        rows[2]['Mounts'][0].update(Destination='/data', Source='/srv/example-media')
        watcher.validate_bookbridge_containers(rows, config())

    def test_wrong_host_root_detected_for_all_media_consumers(self):
        for index in range(3):
            rows = containers()
            rows[index]['Mounts'][0]['Source'] = '/mnt/other/audiobooks'
            with self.subTest(index=index), self.assertRaisesRegex(watcher.DeploymentError, 'source changed'):
                watcher.validate_bookbridge_containers(rows, config())

    def test_config_roots_must_agree(self):
        cfg = config()
        cfg['bookbridge_deployment']['jellyfin']['media_source'] = '/other/audiobooks'
        with self.assertRaisesRegex(watcher.DeploymentError, 'one host directory'):
            watcher.validate_bookbridge_containers(containers(), cfg)

    def test_changed_images_and_stopped_services_fail_independently(self):
        for index in range(4):
            for field, value in (('Image', 'sha256:' + 'f' * 64), ('State', {'Status': 'exited'})):
                rows = containers()
                rows[index][field] = value
                with self.subTest(index=index, field=field), self.assertRaises(watcher.DeploymentError):
                    watcher.validate_bookbridge_containers(rows, config())

    def test_nested_mounts_cannot_mask_audio_files(self):
        for index in range(3):
            rows = containers()
            root = rows[index]['Mounts'][0]['Destination']
            rows[index]['Mounts'].append({'Type': 'bind', 'Destination': root + '/hidden',
                                         'Source': '/other/files', 'RW': False})
            with self.subTest(index=index), self.assertRaisesRegex(watcher.DeploymentError, 'Nested'):
                watcher.validate_bookbridge_containers(rows, config())

    def test_worker_media_writable_or_unknown_access_is_rejected(self):
        for mode in (True, None, 'false', 0):
            rows = containers()
            rows[1]['Mounts'][0]['RW'] = mode
            with self.subTest(mode=mode), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_containers(rows, config())

    def test_named_volume_not_equivalent_to_verified_host_bind(self):
        rows = containers()
        rows[1]['Mounts'][0]['Type'] = 'volume'
        with self.assertRaisesRegex(watcher.DeploymentError, 'host bind'):
            watcher.validate_bookbridge_containers(rows, config())

    def test_duplicate_or_missing_containers_and_mounts_rejected(self):
        cases = [containers()[:-1], containers() + [containers()[0]],
                 containers()[1:] + [containers()[1]]]
        for rows in cases:
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_containers(rows, config())
        rows = containers()
        rows[0]['Mounts'].append(copy.deepcopy(rows[0]['Mounts'][0]))
        with self.assertRaisesRegex(watcher.DeploymentError, 'Duplicate'):
            watcher.validate_bookbridge_containers(rows, config())
        rows[0]['Mounts'] = []
        with self.assertRaisesRegex(watcher.DeploymentError, 'no verified bind'):
            watcher.validate_bookbridge_containers(rows, config())

    def test_noncanonical_or_unreviewed_configuration_rejected(self):
        for path in ('/', 'relative', '/srv/example-media/../audio', '/mnt//audio', '/mnt/audio/', '/mnt/./audio', '/mnt/au\ndio'):
            cfg = config()
            for entry in cfg['bookbridge_deployment'].values():
                if 'media_source' in entry:
                    entry['media_source'] = path
            with self.subTest(path=path), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_deployment_policy(cfg)
        cfg = config()
        cfg['bookbridge_deployment']['jellyfin']['image'] = 'jellyfin:latest'
        with self.assertRaises(watcher.DeploymentError):
            watcher.bookbridge_deployment_policy(cfg)
        cfg = config()
        cfg['bookbridge_deployment']['book-progress-sync']['audio_root'] = '/other-root'
        with self.assertRaises(watcher.DeploymentError):
            watcher.bookbridge_deployment_policy(cfg)

    def run_main(self, cfg, runner):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'state').mkdir()
            (root / 'config/config.json').write_text(json.dumps(cfg))
            result = watcher.main(root, runner, clock=lambda: 12345)
            gatefile = root / 'state/deployment.json'
            gate = json.loads(gatefile.read_text())
            self.assertEqual(gatefile.stat().st_mode & 0o777, 0o600)
        return result, gate

    def test_main_emits_successful_bb_gate_even_without_bookorbit(self):
        # Legacy check fails due to no BookOrbit config, but the independent
        # check still consumes its own fresh, complete Docker snapshot.
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(containers())))
        code, gate = self.run_main(config(), runner)
        self.assertFalse(gate['ok'])
        self.assertTrue(gate['bookbridge_ok'])
        self.assertEqual(gate['checked_at'], 12345)
        self.assertNotIn('bookbridge_reason', gate)
        self.assertEqual(code, 1)  # Preserve the legacy cron exit-code contract.
        self.assertTrue(any('book-progress-sync' in c.args[0] for c in runner.call_args_list))

    def test_main_failure_is_independent_and_sanitized(self):
        runner = Mock(side_effect=subprocess.CalledProcessError(1, ['secret-command'],
                                                                output='secret output', stderr='secret token'))
        code, gate = self.run_main(config(), runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['bookbridge_ok'])
        self.assertEqual(gate['bookbridge_reason'], 'CalledProcessError')
        self.assertNotIn('secret', json.dumps(gate))

    def test_not_configured_gate_is_explicit_and_never_calls_extra_docker(self):
        runner = Mock(return_value=SimpleNamespace(stdout='[]'))
        _, gate = self.run_main({}, runner)
        self.assertIs(gate['bookbridge_ok'], False)
        self.assertEqual(gate['bookbridge_reason'], 'BookBridge deployment is not configured')
        self.assertEqual(runner.call_count, 1)


if __name__ == '__main__':
    unittest.main()
