"""Detachment must remove process/DB dependencies without bypassing media gates."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
