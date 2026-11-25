"""Existing deployments keep their verified beta channel when upgrading the host watcher."""
import copy
import json
import unittest
from types import SimpleNamespace

from watch_deployment import DeploymentError, resolve_dockge_worker


class DockgeWorkerReleaseTest(unittest.TestCase):
    def setUp(self):
        name, repo = 'book-progress-sync', 'ghcr.io/christophzepp/books-and-audio'
        image_id = 'sha256:' + '1' * 64
        self.config = {'dockge_worker_channel': 'beta', 'folder_deployment': {
            'worker': {'container': name, 'image': 'old'}, 'bb': {'image': 'other'}},
            'bookbridge_deployment': {name: {'image': 'old'}},
            'podcast_deployment': {'worker': {'container': name, 'image': 'old'}}}
        self.container = {'Name': '/' + name, 'State': {'Running': True}, 'Image': image_id,
                          'Config': {'Image': repo + ':beta', 'Labels': {'com.docker.compose.project': name,
                                     'com.docker.compose.service': name}}, 'Mounts': [{'Destination': '/state'}]}
        self.image = {'Id': image_id, 'RepoDigests': [repo + '@sha256:' + '2' * 64],
                      'Config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40,
                                            'org.opencontainers.image.version': '0.1.0-beta.14',
                                            'org.opencontainers.image.source': 'https://github.com/christophzepp/books-and-audio'}}}

    def run_inspect(self, command, **kwargs):
        value = {'container': self.container, 'image': self.image}[command[1]]
        return SimpleNamespace(stdout=json.dumps([value]))

    def test_updates_only_worker_image_for_this_observation(self):
        before = copy.deepcopy(self.config)
        actual = resolve_dockge_worker(self.config, self.run_inspect)
        self.assertEqual(self.config, before)
        for group, entry in [('bookbridge_deployment', 'book-progress-sync'),
                             ('folder_deployment', 'worker'), ('podcast_deployment', 'worker')]:
            before[group][entry]['image'] = self.image['Id']
        self.assertEqual(actual, before)

    def test_old_installation_uses_exact_pins(self):
        del self.config['dockge_worker_channel']
        self.assertIs(resolve_dockge_worker(self.config, None), self.config)

    def test_rejects_swapped_tag_code_mount_and_unpublished_image(self):
        for obj, key, value in [(self.container, 'Image', 'sha256:' + '3' * 64),
                                (self.container, 'Mounts', [{'Destination': '/app/sync.py'}]),
                                (self.image, 'RepoDigests', []),
                                (self.config['folder_deployment']['worker'], 'container', 'bookbridge')]:
            with self.subTest(key=key):
                original = obj[key]
                obj[key] = value
                with self.assertRaises(DeploymentError):
                    resolve_dockge_worker(self.config, self.run_inspect)
                obj[key] = original
