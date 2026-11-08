"""The production transfer guard retains fresh host authority without media I/O."""
import copy
import json
from types import SimpleNamespace
import time
import unittest

from admin_runtime import Administration
from folder_manager import FolderManager, FolderManagerError
from watch_deployment import DeploymentError
import test_folder_transfer_io as fixtures


class AdminMediaGuardTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.FolderTransferIOTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.media = self.f.media
        rows = copy.deepcopy(self.f.config['folder_mappings'])
        for row in rows:
            name = 'audio' if row['kind'] == 'audiobooks' else 'text'
            row['paths'].update(server='/srv/' + name, bo='/' + name,
                                abs='/' + name, jf='/' + name)
        self.cfg = dict(mapping_provider='bookorbit', folder_mappings=rows,
                        jellyfin=dict(enabled=True, auto_ebooks=dict(enabled=True)))
        self.folders = FolderManager(self.f.state, self.cfg,
            [{k: r['paths'][k] for k in ('worker', 'server')} for r in rows],
            mount_checker=lambda _: True, generation_reader=lambda: 'stable-mount')
        # Create the full configuration in a fresh state, not the fixture's worker-only one.
        self.folders.path.unlink()
        (self.folders.root / '.folders.seeded').unlink()
        self.folders.seed()
        self.admin = Administration.__new__(Administration)
        self.admin.root = self.f.state
        self.admin.folders = self.folders
        self.admin.manager = SimpleNamespace(effective=lambda: (copy.deepcopy(self.cfg), {}, []))
        self.admin.podcast_effective = lambda cfg: cfg
        self.gate = dict(checked_at=time.time(), folder_gate_ok=True, ok=True,
                         folder_mounts={role: [dict(source=r['paths']['server'],
                             destination=r['paths'][role], rw=False) for r in rows]
                             for role in ('worker', 'bo', 'abs', 'jf')})
        self.write_gate()
        self.folders.require('ebooks', context=self.admin.folder_context('ebooks'))

    def write_gate(self):
        (self.f.state / 'deployment.json').write_text(json.dumps(self.gate))

    def no_media(self):
        return fixtures.FolderTransferIOTests.no_media(self, self.folders)

    def test_current_host_and_cached_folder_proof_authorize_without_a_walk(self):
        with self.no_media():
            self.assertTrue(self.admin.media_observation_guard('ebooks'))

    def test_stale_host_receipt_blocks_even_with_positive_history_without_media(self):
        self.gate['checked_at'] -= 200
        self.write_gate()
        with self.no_media(), self.assertRaisesRegex(DeploymentError, 'Fresh folder'):
            self.admin.media_observation_guard('ebooks')

    def test_wrong_current_mount_alias_blocks_without_media(self):
        self.gate['folder_mounts']['bo'][1]['source'] = '/different/text'
        self.write_gate()
        with self.no_media(), self.assertRaisesRegex(DeploymentError, 'same verified media'):
            self.admin.media_observation_guard('ebooks')

    def test_missing_folder_witness_cannot_trigger_physical_fallback(self):
        path = self.folders.observations.directory / self.folders.observations._name('folders', 'text')
        path.unlink()
        with self.no_media(), self.assertRaisesRegex(FolderManagerError, 'folder_verification_required'):
            self.admin.media_observation_guard('ebooks')


if __name__ == '__main__':
    unittest.main()
