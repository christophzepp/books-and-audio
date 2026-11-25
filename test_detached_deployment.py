"""Detachment must remove process/DB dependencies without bypassing media gates."""
import contextlib
import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import watch_deployment as watcher
from test_watch_deployment import ABS_ID, native_config, native_containers


def detached_config():
    cfg = native_config()
    cfg['bookbridge_policy'] = {'phase': 'detached'}
    return cfg


class DetachedDeploymentTests(unittest.TestCase):
    def test_abs_and_bookorbit_suffice_even_if_bookbridge_is_gone(self):
        cfg = detached_config()
        before = copy.deepcopy(cfg)
        # Any second command (including SQL/docker exec) is an error.
        runner = Mock(side_effect=[SimpleNamespace(stdout=json.dumps(native_containers()[:2]))])
        watcher.check_deployment(cfg, runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], ['docker', 'inspect', 'audiobookshelf', 'bookorbit'])
        self.assertEqual(cfg, before)

    def test_empty_book_inventory_gets_host_receipt_and_worker_discovers_a_later_import(self):
        from sync import Bridge
        from test_discovery import ABS_LIBRARY, FakeABS, FakeBO, book

        cfg = dict(detached_config(), mappings=[], native_readalong={}, abs_url='http://abs',
                   bo_url='http://bo', abs_version='2.37',
                   auto_discovery=dict(enabled=True, abs_library_id=ABS_LIBRARY, bo_library_ids=[7],
                                       interval_seconds=300, stable_seconds=300))
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(native_containers()[:2])))
        clock = Mock(return_value=1000)
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            (root/'state').mkdir()
            (root/'config').mkdir()
            (root/'config/config.json').write_text(json.dumps(cfg))
            def receipt(stamp):
                clock.return_value = stamp
                watcher.main(root, runner, clock)
                gate = json.loads((root/'state/deployment.json').read_text())
                self.assertTrue(gate['ok'], gate)
            receipt(1000)
            abs_client = FakeABS([])
            abs_client.version = lambda: cfg['abs_version']
            with patch('sync.ABSClient', return_value=abs_client), patch('sync.BOClient', return_value=FakeBO([])):
                bridge = Bridge(root/'state', cfg, dict(abs_token='test',bo_username='test',bo_password='test'),clock)
            self.addCleanup(bridge.db.close)
            first = bridge.cycle(apply=True,settle=0)
            self.assertEqual(first['mapped'],0)
            self.assertTrue(first['discovery']['scan_ok'])
            self.assertEqual(bridge.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
            library = [book()]
            bridge.abs, bridge.bo = FakeABS(library), FakeBO(library)
            bridge.abs.version = lambda: cfg['abs_version']
            receipt(1300)
            self.assertEqual(bridge.refresh_mappings(True)[0], [])
            receipt(1600)
            mappings, report = bridge.refresh_mappings(True)
            self.assertEqual(report['added'],1)
            self.assertEqual([row['abs_id'] for row in mappings],['a'])
            self.assertEqual(bridge.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
            for call in runner.call_args_list:
                self.assertEqual(call.args[0],['docker','inspect','audiobookshelf','bookorbit'])

    def test_empty_list_keeps_deployment_and_explicit_pin_requirements(self):
        cfg = dict(detached_config(), mappings=[], native_readalong={})
        replacing = native_containers()[:2]
        replacing[1]['Mounts'][0]['Source'] = '/wrong'
        with self.assertRaisesRegex(watcher.DeploymentError, 'Media mount'):
            watcher.validate_containers(replacing,cfg)
        for key in ('mappings','native_readalong'):
            invalid = cfg.copy()
            del invalid[key]
            with self.subTest(key=key), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(invalid)
        with self.assertRaisesRegex(watcher.DeploymentError, 'no unique audio mapping'):
            watcher.bookbridge_policy(dict(detached_config(),mappings=[]))

    def test_detachment_keeps_images_state_paths_and_version_checks(self):
        for index in (0, 1):
            for field in ('image', 'state', 'mount'):
                actual = native_containers()[:2]
                if field == 'image': actual[index]['Image'] = 'sha256:' + 'f' * 64
                if field == 'state': actual[index]['State']['Status'] = 'exited'
                if field == 'mount': actual[index]['Mounts'][0]['Source'] = '/wrong'
                with self.subTest(index=index, field=field), self.assertRaises(watcher.DeploymentError):
                    watcher.validate_containers(actual, detached_config())
        actual = native_containers()[:2]
        actual[1]['Config']['Env'] = ['APP_VERSION=unreviewed']
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_containers(actual, detached_config())

    def test_invalid_or_unmapped_native_epub_blocks_before_docker(self):
        variants = []
        for key, value in (('bo_id', 99), ('overlay_file_id', True), ('overlay_size', 0),
                           ('overlay_path', '/audiobooks/Other/test.epub')):
            cfg = detached_config(); cfg['native_readalong'][ABS_ID][key] = value; variants.append(cfg)
        cfg = detached_config(); cfg['mappings'] *= 2; variants.append(cfg)
        cfg = detached_config(); cfg['native_readalong']['missing'] = cfg['native_readalong'].pop(ABS_ID); variants.append(cfg)
        cfg = detached_config(); del cfg['native_readalong']; variants.append(cfg)
        for cfg in variants:
            runner = Mock()
            with self.subTest(cfg=cfg), self.assertRaises(watcher.DeploymentError):
                watcher.check_deployment(cfg, runner)
            runner.assert_not_called()

    def test_old_bookbridge_fields_cannot_silently_mix_with_detachment(self):
        cfg = native_config(); cfg['bookbridge_policy']['phase'] = 'detached'
        with self.assertRaises(watcher.DeploymentError):
            watcher.bookbridge_policy(cfg)
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_bookbridge_snapshot({}, detached_config())

    def test_broken_docker_evidence_remains_blocked(self):
        for actual in ([], native_containers()[:1], native_containers(), None):
            runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(actual)))
            with self.subTest(actual=actual), self.assertRaises(watcher.DeploymentError):
                watcher.check_deployment(detached_config(), runner)


if __name__ == '__main__':
    unittest.main()
