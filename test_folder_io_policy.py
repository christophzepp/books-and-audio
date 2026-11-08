"""Real temporary directories and mocked mount generations; no live media I/O."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from folder_manager import FolderManager, FolderManagerError
from io_metrics import snapshot
import admin_runtime
import test_admin_runtime as fixtures


class FolderSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.audio = self.root / 'audio'; self.audio.mkdir()
        self.ebooks = self.root / 'ebooks'; self.ebooks.mkdir()
        self.state = self.root / 'state'; self.state.mkdir()
        self.available, self.mount, self.now = True, 'mount-generation-1', 1000.0
        self.config = {'folder_mappings': [
            dict(id='audio', kind='audiobooks', name='Audio', active=True,
                 paths={'worker': str(self.audio)}),
            dict(id='text', kind='ebooks', name='Text', active=True,
                 paths={'worker': str(self.ebooks)}),
            dict(id='inactive', kind='ebooks', name='Inactive', active=False,
                 paths={'worker': str(self.ebooks / 'absent')})]}
        self.manager = FolderManager(self.state, self.config,
            [{'worker': str(self.audio)}, {'worker': str(self.ebooks)}],
            lambda path: self.available, generation_reader=lambda: self.mount,
            clock=lambda: self.now)
        self.manager.seed()

    def test_idle_effective_and_gui_reuse_one_snapshot_without_traversals(self):
        with patch.object(self.manager, 'check', side_effect=AssertionError('media traversal')):
            for _ in range(100):
                self.manager.public()
                self.manager.effective()
                self.manager.generation('ebooks')
        self.assertEqual(snapshot(self.manager)['folder_checks'], 2)

    def test_integrity_interval_checks_active_rows_once_not_inactive_rows(self):
        self.manager.base_config['io_policy'] = {'folder_validation_interval_seconds': 900}
        self.now += 899
        with patch.object(self.manager, 'check', wraps=self.manager.check) as check:
            self.manager.effective(); self.assertEqual(check.call_count, 0)
            self.now += 1
            self.manager.effective()
            self.assertEqual([call.args[0]['id'] for call in check.call_args_list], ['audio', 'text'])
            self.manager.public(); self.manager.effective()
            self.assertEqual(check.call_count, 2)

    def test_negative_result_retries_after_one_minute(self):
        self.available = False; self.mount = 'mount-generation-2'
        with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'):
            self.manager.effective()
        self.available = True; self.now += 59
        with self.assertRaises(FolderManagerError): self.manager.effective()
        self.now += 1
        self.assertTrue(self.manager.effective()['folder_mappings'])

    def test_remount_at_same_path_invalidates_positive_and_fastpath_generation(self):
        before = self.manager.generation('ebooks')
        self.mount = 'mount-generation-2'; self.available = False
        self.assertNotEqual(before, self.manager.generation('ebooks'))
        with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'):
            self.manager.effective()
        self.available = True; self.mount = 'mount-generation-3'
        self.assertTrue(self.manager.effective()['folder_mappings'])

    def test_missing_or_untrusted_generation_never_reuses_cached_positive(self):
        for value in (None, '', {}, False):
            self.mount = value; self.available = False
            with self.assertRaisesRegex(FolderManagerError, 'folder_generation_unavailable'):
                self.manager.generation('ebooks')
            with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'):
                self.manager.effective()

    def test_known_media_error_invalidates_idle_binding(self):
        before = self.manager.generation('ebooks')
        self.available = False
        with self.assertRaises(FolderManagerError): self.manager.require('ebooks')
        self.assertNotEqual(before, self.manager.generation('ebooks'))

    def test_repeated_failed_podcast_checks_do_not_invalidate_healthy_ebook_binding(self):
        podcasts = self.root / 'podcasts'; podcasts.mkdir()
        config = copy.deepcopy(self.config)
        config['folder_mappings'].append(dict(id='podcasts', kind='podcasts',
            name='Podcasts', active=True, paths={'worker': str(podcasts / 'missing')}))
        state = self.root / 'separate-state'; state.mkdir()
        manager = FolderManager(state, config,
            [{'worker': str(self.audio)}, {'worker': str(self.ebooks)}, {'worker': str(podcasts)}],
            lambda path: True, generation_reader=lambda: self.mount, clock=lambda: self.now)
        manager.seed()
        ebooks = manager.generation('ebooks')
        previous_podcast = manager.generation('podcasts')
        for _ in range(3):
            self.now += 60
            effective = manager.effective()
            self.assertEqual(effective['podcasts']['folder_error'], 'folder_unavailable')
            self.assertEqual(ebooks, manager.generation('ebooks'))
            self.assertNotEqual(previous_podcast, manager.generation('podcasts'))
            previous_podcast = manager.generation('podcasts')

    def test_generation_survives_normal_worker_restart_and_is_pure(self):
        before = self.manager.generation('ebooks', context={'ok': True})
        self.manager.refresh(context={'ok': True})
        restored = FolderManager(self.state, self.config,
            [{'worker': str(self.audio)}, {'worker': str(self.ebooks)}],
            lambda path: self.available, generation_reader=lambda: self.mount,
            clock=lambda: self.now)
        restored.seed()
        self.assertEqual(before, restored.generation('ebooks', context={'ok': True}))
        with patch.object(self.manager, 'check', side_effect=AssertionError('traversal')):
            self.manager.generation('ebooks', context={'ok': False})
            self.manager.effective()  # A scoped token must not mutate snapshot context.

    def test_require_checks_only_the_kind_used_by_the_operation(self):
        with patch.object(self.manager, 'check', wraps=self.manager.check) as check:
            self.manager.require('ebooks', context={'ok': True})
        self.assertEqual([call.args[0]['id'] for call in check.call_args_list], ['text'])

    def test_require_detects_symlink_replacement_even_with_same_mount_generation(self):
        self.audio.rename(self.root / 'old-audio')
        self.audio.symlink_to(self.ebooks, target_is_directory=True)
        with self.assertRaisesRegex(FolderManagerError, 'folder_symlink_or_not_directory'):
            self.manager.require('audiobooks')

    def test_require_reopens_replaced_root_instead_of_reusing_its_old_identity(self):
        key = self.manager._signature(self.config['folder_mappings'][0])
        before = self.manager._checks[key]['identity']
        self.audio.rename(self.root / 'old-audio'); self.audio.mkdir()
        self.manager.require('audiobooks')
        self.assertNotEqual(before, self.manager._checks[key]['identity'])

    def test_changed_config_is_pending_in_gui_then_checked_before_effective_use(self):
        state = json.loads(self.manager.path.read_text())
        state['revision'] += 1
        state['rows'][0]['paths']['worker'] = str(self.audio / 'missing')
        self.manager.path.write_text(json.dumps(state)); self.manager.path.chmod(0o600)
        with patch.object(self.manager, 'check', wraps=self.manager.check) as check:
            self.assertEqual(self.manager.public()['rows'][0]['check']['code'], 'folder_check_pending')
            self.assertEqual(check.call_count, 0)
            with self.assertRaisesRegex(FolderManagerError, 'folder_unavailable'):
                self.manager.effective()
            self.assertEqual(check.call_count, 1)

    def test_deployment_identity_change_invalidates_without_extending_receipt_ttl(self):
        self.manager.refresh(context={'ok': True, 'folder_mounts': {'worker': 'first'}})
        before = self.manager.generation('ebooks')
        with patch.object(self.manager, 'check', wraps=self.manager.check) as check:
            self.manager.refresh(context={'ok': True, 'folder_mounts': {'worker': 'first'}})
            self.assertEqual(check.call_count, 0)
            self.manager.refresh(context={'ok': False, 'folder_mounts': {}})
            # Local worker mounts did not change; separate fresh deployment
            # guards handle the remote service outage without waking media.
            self.assertEqual(check.call_count, 0)
        self.assertNotEqual(before, self.manager.generation('ebooks'))


class PresentationWriteTests(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.admin = admin_runtime.Administration(self.root, self.key, self.config,
                                                  self.credentials, fixtures.CandidateBridge)

    def test_no_replacement_of_unchanged_disposable_gui_snapshots(self):
        with patch('admin_runtime.atomic', wraps=admin_runtime.atomic) as atomic:
            self.admin.publish(None)
            first = atomic.call_count
            for _ in range(50): self.admin.publish(None)
            self.assertEqual(atomic.call_count, first)
            self.assertGreater(snapshot(self.admin)['gui_snapshot_skips'], 0)

    def test_status_heartbeat_remains_fresh_and_connections_do_not_rewrite(self):
        with patch('admin_runtime.time.monotonic', return_value=1000):
            self.admin.publish(None)
        old = json.loads((self.admin.store.control / 'status.json').read_text())
        with patch('admin_runtime.time.monotonic', return_value=1060), \
             patch('admin_runtime.atomic', wraps=admin_runtime.atomic) as atomic:
            self.admin.publish(None)
        self.assertEqual([call.args[0].name for call in atomic.call_args_list], ['status.json'])
        current = self.admin.store.status()
        self.assertFalse(current['stale'])
        self.assertGreaterEqual(current['published_at'], old['published_at'])

    def test_actual_status_change_publishes_immediately(self):
        self.admin.publish(None)
        self.admin.store.pause(True)
        with patch('admin_runtime.atomic', wraps=admin_runtime.atomic) as atomic:
            self.admin.publish(None)
        self.assertIn('status.json', [call.args[0].name for call in atomic.call_args_list])
        self.assertTrue(self.admin.store.status()['paused'])

    def test_missing_snapshot_is_recreated_despite_matching_fingerprint(self):
        self.admin.publish(None)
        (self.admin.store.control / 'connections-public.json').unlink()
        self.admin.publish(None)
        self.assertTrue((self.admin.store.control / 'connections-public.json').exists())

    def test_deployment_heartbeat_time_does_not_change_media_generation_context(self):
        first = self.admin.folder_context()
        receipt = json.loads((self.root / 'deployment.json').read_text())
        receipt['checked_at'] += 60
        (self.root / 'deployment.json').write_text(json.dumps(receipt))
        self.assertEqual(first, self.admin.folder_context())
        receipt['ok'] = False
        (self.root / 'deployment.json').write_text(json.dumps(receipt))
        self.assertNotEqual(first, self.admin.folder_context())

    def test_podcast_and_unrelated_endpoint_changes_do_not_invalidate_ebooks(self):
        gate = dict(ok=True, bookbridge_ok=False, folder_gate_ok=True,
                    folder_mounts={'worker': [], 'bo': [], 'jf': [], 'abs': []},
                    folder_errors={}, podcasts_ok=True, pinepods_ok=True)
        (self.root / 'deployment.json').write_text(json.dumps(gate))
        first = self.admin.folder_context('ebooks')
        gate.update(checked_at=100, podcasts_ok=False, pinepods_ok=False,
                    podcast_mounts={'abs': {'at': 12345}},
                    folder_errors={'abs': 'stopped'})
        (self.root / 'deployment.json').write_text(json.dumps(gate))
        self.assertEqual(first, self.admin.folder_context('ebooks'))
        gate['folder_errors']['bo'] = 'stopped'
        (self.root / 'deployment.json').write_text(json.dumps(gate))
        self.assertNotEqual(first, self.admin.folder_context('ebooks'))


if __name__ == '__main__': unittest.main()
