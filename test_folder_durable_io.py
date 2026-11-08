"""Persistent folder history avoids periodic media walks; force remains physical."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from folder_manager import FolderManager, FolderManagerError
from io_metrics import snapshot


class DurableFolderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir()
        self.audio = self.root / 'audio'; self.audio.mkdir()
        self.ebooks = self.root / 'ebooks'; self.ebooks.mkdir()
        self.roots = [{'worker': str(self.audio)}, {'worker': str(self.ebooks)}]
        self.config = {'folder_mappings': [
            dict(id='audio', kind='audiobooks', name='Audio', active=True, paths={'worker': str(self.audio)}),
            dict(id='text', kind='ebooks', name='Text', active=True, paths={'worker': str(self.ebooks)})]}
        self.now = 1000.
        self.generation = 'stable-mount'
        self.available = True
        self.manager = self.make()
        self.manager.seed()

    def make(self, **kw):
        return FolderManager(self.state, self.config, self.roots,
            mount_checker=kw.pop('mount_checker', lambda _: self.available),
            generation_reader=kw.pop('generation_reader', lambda: self.generation),
            clock=lambda: self.now, **kw)

    def change_store(self, edit):
        value = json.loads(self.manager.path.read_text()); value['revision'] += 1
        edit(value)
        self.manager.path.write_text(json.dumps(value)); self.manager.path.chmod(0o600)

    def test_positive_history_never_expires_by_age_or_restarts(self):
        self.now += 20 * 365 * 86400
        with patch.object(self.manager, 'check', side_effect=AssertionError('media walk')):
            for _ in range(100): self.manager.effective(); self.manager.public()
        restored = self.make(mount_checker=lambda _: (_ for _ in ()).throw(AssertionError('media mount stat')))
        with patch.object(restored, 'check', side_effect=AssertionError('restart media walk')):
            restored.seed(); restored.effective()
        self.assertEqual(snapshot(restored).get('folder_checks', 0), 0)
        self.assertEqual(snapshot(restored)['folder_durable_hits'], 2)

    def test_new_unrelated_folder_does_not_invalidate_ebook_or_audio_history(self):
        before = self.manager.observation_generation('ebooks')
        self.change_store(lambda state: state['rows'].append(dict(id='old-podcast', kind='podcasts',
            name='Inactive podcast', active=False, paths={'worker': str(self.audio / 'unused')})))
        with patch.object(self.manager, 'check', side_effect=AssertionError('unchanged media walk')):
            self.manager.effective()
        self.assertEqual(before, self.manager.observation_generation('ebooks'))

    def test_changed_active_path_checks_only_that_row(self):
        subfolder = self.ebooks / 'new'; subfolder.mkdir()
        self.change_store(lambda state: state['rows'][1]['paths'].update(worker=str(subfolder)))
        with patch.object(self.manager, 'check', wraps=self.manager.check) as checked:
            self.manager.effective()
        self.assertEqual([call.args[0]['id'] for call in checked.call_args_list], ['text'])

    def test_display_name_change_reuses_physical_history(self):
        before = self.manager.observation_generation('ebooks')
        self.change_store(lambda state: state['rows'][1].update(name='Renamed books'))
        with patch.object(self.manager, 'check', side_effect=AssertionError('display change media walk')):
            self.manager.effective()
        self.assertEqual(before, self.manager.observation_generation('ebooks'))

    def test_force_bypasses_persisted_history_for_only_used_kind(self):
        restored = self.make(); restored.seed()
        self.ebooks.rename(self.root / 'old-ebooks'); self.ebooks.symlink_to(self.audio, target_is_directory=True)
        with patch.object(restored, 'check', wraps=restored.check) as checked:
            with self.assertRaisesRegex(FolderManagerError, 'folder_symlink_or_not_directory'):
                restored.require('ebooks')
        self.assertEqual([call.args[0]['id'] for call in checked.call_args_list], ['text'])
        self.assertFalse(restored.public()['rows'][1]['check']['ok'])

    def test_failed_force_forgets_positive_and_cannot_reuse_it_after_restart(self):
        self.available = False
        with self.assertRaises(FolderManagerError): self.manager.require('ebooks')
        restored = self.make()
        restored.seed()
        self.assertFalse(restored.public()['rows'][1]['check']['ok'])
        self.assertTrue(restored.public()['rows'][0]['check']['ok'])

    def test_recovered_folder_proof_and_generation_survive_restart(self):
        unaffected = self.manager.observation_generation('audiobooks')
        self.available = False
        with self.assertRaises(FolderManagerError): self.manager.require('ebooks')
        self.available = True
        self.manager.require('ebooks')
        recovered = self.manager.observation_generation('ebooks')
        self.assertEqual(unaffected, self.manager.observation_generation('audiobooks'))
        restored = self.make()
        with patch.object(restored, 'check', side_effect=AssertionError('recovered restart media walk')):
            restored.seed(); restored.effective()
        self.assertEqual(recovered, restored.observation_generation('ebooks'))
        self.assertEqual(snapshot(restored)['folder_durable_hits'], 2)

    def test_live_remount_recovery_keeps_generation_after_worker_recreation(self):
        mount = dict(stable='same-fs', runtime=['initial-mount'])
        manager = self.make(generation_reader=lambda: mount)
        manager.seed()
        mount['runtime'] = ['remounted']
        manager.require('ebooks')
        recovered = manager.observation_generation('ebooks')
        # New container mount IDs are ephemeral and do not erase recovery.
        mount['runtime'] = ['new-container-mount']
        restored = self.make(generation_reader=lambda: mount)
        with patch.object(restored, 'check', side_effect=AssertionError('remount recovery restart media walk')):
            restored.refresh(kind='ebooks')
        self.assertEqual(recovered, restored.observation_generation('ebooks'))

    def test_failed_manual_folder_test_revokes_prior_positive(self):
        self.available = False
        outcome = self.manager.mutate('test', dict(expected_revision=0, id='text'))
        self.assertFalse(outcome['ok'])
        self.assertFalse(self.manager.public()['rows'][1]['check']['ok'])
        with self.assertRaises(FolderManagerError): self.manager.effective()
        restored = self.make(); restored.seed()
        self.assertFalse(restored.public()['rows'][1]['check']['ok'])

    def test_explicit_global_invalidation_bypasses_durable_history(self):
        self.manager.invalidate()
        with patch.object(self.manager, 'check', wraps=self.manager.check) as checked:
            self.manager.effective()
        self.assertEqual([c.args[0]['id'] for c in checked.call_args_list], ['audio', 'text'])

    def test_explicit_invalidation_is_retained_across_restart_then_reusable(self):
        self.manager.invalidate()
        restored = self.make()
        with patch.object(restored, 'check', wraps=restored.check) as checked:
            restored.seed()
        self.assertEqual([c.args[0]['id'] for c in checked.call_args_list], ['audio', 'text'])
        again = self.make()
        with patch.object(again, 'check', side_effect=AssertionError('global recovery restart media walk')):
            again.seed()

    def test_corrupt_epoch_cannot_restore_old_generation_zero_history(self):
        before = self.manager.observation_generation('ebooks')
        unaffected = self.manager.observation_generation('audiobooks')
        name = self.manager.observations._name('folder-epoch', 'ebooks')
        (self.manager.observations.directory / name).write_bytes(b'corrupt')
        restored = self.make()
        with patch.object(restored, 'check', wraps=restored.check) as checked:
            restored.seed()
        self.assertEqual([c.args[0]['id'] for c in checked.call_args_list], ['text'])
        self.assertNotEqual(before, restored.observation_generation('ebooks'))
        self.assertEqual(unaffected, restored.observation_generation('audiobooks'))

    def test_corrupt_folder_checkpoint_checks_only_affected_folder(self):
        name = self.manager.observations._name('folders', 'text')
        (self.manager.observations.directory / name).write_bytes(b'corrupt')
        restored = self.make()
        with patch.object(restored, 'check', wraps=restored.check) as checked:
            restored.seed()
        self.assertEqual([call.args[0]['id'] for call in checked.call_args_list], ['text'])

    def test_service_outage_preserves_local_directory_history(self):
        context = dict(folder_mounts={'worker': [
            dict(source='/srv/audio', destination=str(self.audio), rw=False),
            dict(source='/srv/ebooks', destination=str(self.ebooks), rw=False)]},
            ok=True, folder_gate_ok=True, folder_errors={})
        before = self.manager.observation_generation('ebooks', context=context)
        unavailable = dict(context, ok=False, folder_gate_ok=False, folder_errors={'bo': 'unavailable'})
        self.assertEqual(before, self.manager.observation_generation('ebooks', context=unavailable))
        with patch.object(self.manager, 'check', side_effect=AssertionError('outage media walk')):
            self.manager.effective(context=unavailable)
            self.manager.effective(context=context)

    def test_remote_mount_changes_are_scoped_without_walking_local_folders(self):
        context = dict(folder_mounts={'worker': [
            dict(source='/srv/audio', destination=str(self.audio), rw=False),
            dict(source='/srv/ebooks', destination=str(self.ebooks), rw=False)]})
        books = self.manager.observation_generation('ebooks', context=context)
        audio = self.manager.observation_generation('audiobooks', context=context)
        changed = copy.deepcopy(context); changed['folder_mounts']['worker'][0]['source'] = '/srv/replaced-audio'
        self.assertEqual(books, self.manager.observation_generation('ebooks', context=changed))
        self.assertNotEqual(audio, self.manager.observation_generation('audiobooks', context=changed))
        with patch.object(self.manager, 'check', side_effect=AssertionError('remote service change media walk')):
            self.manager.effective(context=changed)


class KernelMountScopeTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            '10 1 0:11 / / rw - overlay overlay rw,lowerdir=/old-root',
            '20 10 8:16 /media/books /ebooks ro,relatime - ext4 /dev/sdb rw',
            '21 10 8:32 /media/audio /audiobooks ro,relatime - ext4 /dev/sdc rw',
            '22 10 8:1 /config /config rw - ext4 /dev/nvme0n1p1 rw']
        self.boot = 'same-host-boot'

    def parse(self, paths):
        def read(path, *a, **kw):
            if str(path) == '/proc/self/mountinfo': return '\n'.join(self.rows)
            if str(path) == '/proc/sys/kernel/random/boot_id': return self.boot
            raise AssertionError('unexpected non-kernel read')
        with patch.object(Path, 'read_text', read): return FolderManager._mount_generation(paths)

    def test_worker_recreation_and_unrelated_mount_changes_keep_stable_media_identity(self):
        before = self.parse(['/ebooks'])
        self.rows[0] = self.rows[0].replace('10 1', '30 1').replace('old-root', 'new-root')
        self.rows[1] = self.rows[1].replace('20 10', '40 30')
        self.rows[2] = self.rows[2].replace('/dev/sdc', '/dev/replaced')
        after = self.parse(['/ebooks'])
        self.assertEqual(before['stable'], after['stable'])
        self.assertNotEqual(before['runtime'], after['runtime'])

    def test_media_remount_source_flags_nested_mount_and_boot_changes_are_visible(self):
        before = self.parse(['/ebooks'])
        for row in [self.rows[1].replace('8:16', '8:64'), self.rows[1].replace(' ro,', ' rw,'),
                    self.rows[1].replace('/media/books', '/media/replaced')]:
            original = self.rows[1]; self.rows[1] = row
            self.assertNotEqual(before['stable'], self.parse(['/ebooks'])['stable']); self.rows[1] = original
        self.rows.append('23 20 8:48 / /ebooks/nested ro - ext4 /dev/sdd rw')
        self.assertNotEqual(before['stable'], self.parse(['/ebooks'])['stable'])
        self.rows.pop(); self.boot = 'new-host-boot'
        self.assertNotEqual(before['stable'], self.parse(['/ebooks'])['stable'])

    def test_live_mount_id_change_invalidates_observation_generation(self):
        # No physical paths are consulted when obtaining a generation token.
        manager = object.__new__(FolderManager)
        manager._runtime_mounts = {}; manager._error_epochs = {'ebooks': 0}
        manager._epoch_nonces = {'ebooks': 'f' * 32}; manager.observations = Mock()
        manager.generation_reader = lambda: dict(stable='same-fs', runtime=['old-mount'])
        first = manager._mount_scope('ebooks', [])
        manager.generation_reader = lambda: dict(stable='same-fs', runtime=['new-mount'])
        self.assertEqual(first, manager._mount_scope('ebooks', []))
        self.assertEqual(manager._error_epochs['ebooks'], 1)


if __name__ == '__main__': unittest.main()
