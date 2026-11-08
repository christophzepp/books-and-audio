import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from folder_manager import FolderManager, FolderManagerError, clean_path, translate, default_rows, validate_service_paths


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.audio = self.root / 'audio'; self.audio.mkdir()
        self.ebook = self.root / 'ebooks'; self.ebook.mkdir()
        self.state = self.root / 'state'; self.state.mkdir()
        self.config = dict(jellyfin=dict(auto_ebooks=dict(enabled=True, local_root=str(self.ebook), bo_root='/books', jf_root='/data/ebooks')),
            folder_mappings=[dict(id='audio', kind='audiobooks', name='Hörbücher', active=True,
                paths=dict(worker=str(self.audio), abs='/audiobooks', bo='/audiobooks', jf='/data/audiobooks', bb='/audiobooks')),
              dict(id='text', kind='ebooks', name='E-Books', active=True,
                paths=dict(worker=str(self.ebook), bo='/books', jf='/data/ebooks', bb='/books'))])
        self.roots = [dict(worker=str(self.audio), server='/srv/example-media/audiobooks'),
                      dict(worker=str(self.ebook), server='/srv/example-media/ebooks')]
        self.available = True
        self.manager = FolderManager(self.state, self.config, self.roots, lambda p: self.available)
        self.manager.seed()

    def tearDown(self): self.tmp.cleanup()

    def mutate(self, action, **kwargs):
        kwargs.setdefault('expected_revision', self.manager.public()['revision'])
        return self.manager.mutate(action, kwargs, compatibility_callback=lambda a, b: True)

    def create(self, **kwargs):
        data = dict(kind='ebooks', name='Weitere E-Books', paths=dict(worker=str(self.ebook / 'new'), bo='/new-books', jf='/data/new-books', bb='/new-books'))
        data.update(kwargs)
        return self.mutate('create', **data)

    def test_seed_preserves_original_config_and_active_paths(self):
        expected = copy.deepcopy(self.config)
        result = self.manager.effective()
        self.assertEqual(expected, self.config)
        self.assertEqual(result['jellyfin'], self.config['jellyfin'])
        self.assertEqual(result['folder_mappings'][0]['paths']['server'], '/srv/example-media/audiobooks')
        self.assertEqual(self.manager.public()['revision'], 0)
        self.manager.seed()
        self.assertEqual(self.manager.public()['revision'], 0)

    def test_defaults_use_current_application_aliases(self):
        rows = default_rows({})
        self.assertEqual(rows[1]['paths']['bo'], '/books')
        self.assertEqual(rows[1]['paths']['bb'], '/books')

    def test_translate_round_trip_unicode_and_spaces(self):
        cfg = self.manager.effective()
        path = '/books/Änne A/Das Buch.epub'
        worker = translate(path, 'bo', 'worker', 'ebooks', cfg)
        self.assertEqual(worker, str(self.ebook / 'Änne A/Das Buch.epub'))
        self.assertEqual(translate(worker, 'worker', 'bo', 'ebooks', cfg), path)
        self.assertEqual(translate('/audiobooks/Title/a.mp3', 'abs', 'jf', 'audiobooks', cfg), '/data/audiobooks/Title/a.mp3')

    def test_prefix_collision_not_accepted(self):
        with self.assertRaisesRegex(FolderManagerError, 'folder_mapping_missing'):
            translate('/books-old/x.epub', 'bo', 'jf', 'ebooks', self.manager.effective())

    def test_translation_refuses_ambiguous_rows(self):
        cfg = self.manager.effective()
        cfg['folder_mappings'].append(copy.deepcopy(cfg['folder_mappings'][1]))
        with self.assertRaisesRegex(FolderManagerError, 'ambiguous_folder_mapping'):
            translate('/books/x.epub', 'bo', 'jf', 'ebooks', cfg)

    def test_translation_refuses_missing_alias(self):
        with self.assertRaisesRegex(FolderManagerError, 'folder_mapping_missing'):
            translate('/books/x.epub', 'bo', 'abs', 'ebooks', self.manager.effective())

    def test_path_sanitization(self):
        for value in ('/', 'relative', '/books/../etc', '/books/./x', '/books//x', '//books/x', '/books/', '/books/\x00x', '/books\\x', 1):
            with self.subTest(value=value), self.assertRaises(FolderManagerError): clean_path(value)

    def test_create_is_inactive_and_does_not_create_folder(self):
        result = self.create()
        self.assertTrue(result['ok'])
        row = next(x for x in self.manager.public()['rows'] if x['id'] == result['folder_id'])
        self.assertFalse(row['active'])
        self.assertEqual(row['check']['code'], 'folder_check_pending')
        self.assertFalse((self.ebook / 'new').exists())
        self.assertEqual(row['paths']['server'], '/srv/example-media/ebooks/new')

    def test_activation_requires_existing_mounted_directory(self):
        result = self.create()
        activation = self.mutate('activate', id=result['folder_id'])
        self.assertFalse(activation['ok'])
        self.assertEqual(activation['code'], 'folder_unavailable')
        (self.ebook / 'new').mkdir()
        activation = self.mutate('activate', id=result['folder_id'])
        self.assertTrue(activation['ok'])
        cfg = self.manager.effective()
        self.assertEqual(cfg['jellyfin']['auto_ebooks']['local_root'], str(self.ebook / 'new'))
        self.assertEqual(cfg['jellyfin']['auto_ebooks']['bo_root'], '/new-books')
        self.assertEqual(len([x for x in self.manager.public()['rows'] if x['active'] and x['kind'] == 'ebooks']), 1)

    def test_activation_requires_callback_and_preserves_previous_on_refusal(self):
        (self.ebook / 'new').mkdir()
        created = self.create(); old = self.manager.effective()
        result = self.manager.mutate('activate', dict(id=created['folder_id'], expected_revision=1))
        self.assertFalse(result['ok']); self.assertEqual(result['code'], 'incompatible_folder_mapping')
        self.assertEqual(old, self.manager.effective())

    def test_callback_must_return_exact_true(self):
        (self.ebook / 'new').mkdir(); created = self.create()
        for answer in (1, {}, None, 'yes'):
            with self.subTest(answer=answer):
                result = self.manager.mutate('activate', dict(id=created['folder_id'], expected_revision=1), lambda a,b: answer)
                self.assertEqual(result['code'], 'incompatible_folder_mapping')

    def test_callback_exception_is_sanitized(self):
        (self.ebook / 'new').mkdir(); created = self.create()
        def bad(a,b): raise RuntimeError('private example')
        result = self.manager.mutate('activate', dict(id=created['folder_id'], expected_revision=1), bad)
        self.assertNotIn('private', json.dumps(result)); self.assertFalse(result['ok'])

    def test_mount_loss_blocks_effective_and_test(self):
        self.available = False
        with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'):
            self.manager.require('audiobooks')
        with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'): self.manager.effective()
        result = self.mutate('test', id='audio')
        self.assertEqual(result['code'], 'folder_mount_unavailable')

    def test_mount_list_cannot_be_supplied_by_ui(self):
        result = self.mutate('update', id='audio', access_roots=[dict(worker='/')])
        self.assertEqual(result['code'], 'invalid_folder_field')

    def test_outside_mount_can_be_saved_but_never_activated(self):
        result = self.create(paths=dict(worker='/outside', bb='/books'))
        self.assertTrue(result['ok'])
        result = self.mutate('activate', id=result['folder_id'])
        self.assertEqual(result['code'], 'folder_not_in_allowed_mount')

    def test_root_cannot_be_allowed(self):
        for worker in ('/', '/config', '/state', '/etc', '/mnt'):
            with self.subTest(worker=worker), self.assertRaises(FolderManagerError):
                FolderManager(self.state, self.config, [dict(worker=worker)])

    def test_overlapping_access_roots_refused(self):
        with self.assertRaisesRegex(FolderManagerError, 'overlapping_folder_access_roots'):
            FolderManager(self.state, self.config, [dict(worker=str(self.ebook)), dict(worker=str(self.ebook / 'new'))])

    def test_server_path_cannot_be_forged(self):
        result = self.create(paths=dict(worker=str(self.ebook), server='/etc', bo='/books'))
        self.assertEqual(result['code'], 'server_folder_mismatch')
        result = self.create(paths=dict(worker='/outside', server='/srv/example-media/ebooks'))
        self.assertEqual(result['code'], 'server_folder_unverified')

    def test_symlink_directory_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.ebook / 'link').symlink_to(outside, target_is_directory=True)
        created = self.create(paths=dict(worker=str(self.ebook / 'link'), bo='/books'))
        result = self.mutate('activate', id=created['folder_id'])
        self.assertEqual(result['code'], 'folder_symlink_or_not_directory')

    def test_symlink_ancestor_refused(self):
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'nested').mkdir()
        (self.ebook / 'link').symlink_to(outside, target_is_directory=True)
        created = self.create(paths=dict(worker=str(self.ebook / 'link/nested'), bo='/books'))
        result = self.mutate('test', id=created['folder_id'])
        self.assertEqual(result['code'], 'folder_symlink_or_not_directory')

    def test_file_directory_refused(self):
        (self.ebook / 'new').write_text('test')
        created = self.create()
        self.assertEqual(self.mutate('test', id=created['folder_id'])['code'], 'folder_symlink_or_not_directory')

    def test_test_does_not_write_revision_or_state(self):
        before = self.manager.path.read_bytes()
        self.assertTrue(self.mutate('test', id='audio')['ok'])
        self.assertEqual(self.manager.path.read_bytes(), before)

    def test_active_update_needs_compatibility(self):
        result = self.manager.mutate('update', dict(expected_revision=0, id='audio', paths=dict(jf='/other-audio')))
        self.assertEqual(result['code'], 'incompatible_folder_mapping')
        self.assertEqual(self.manager.effective()['folder_mappings'][0]['paths']['jf'], '/data/audiobooks')

    def test_active_delete_needs_compatibility(self):
        result = self.manager.mutate('delete', dict(expected_revision=0, id='audio'))
        self.assertEqual(result['code'], 'incompatible_folder_mapping')
        self.assertEqual(len(self.manager.public()['rows']), 2)

    def test_inactive_delete_needs_no_callback(self):
        created = self.create()
        result = self.manager.mutate('delete', dict(expected_revision=1, id=created['folder_id']))
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.manager.public()['rows']), 2)

    def test_deactivation_disables_ebook_discovery(self):
        result = self.mutate('deactivate', id='text')
        self.assertTrue(result['ok'])
        self.assertFalse(self.manager.effective()['jellyfin']['auto_ebooks']['enabled'])

    def test_stale_revision_refused(self):
        self.create()
        result = self.mutate('update', id='audio', expected_revision=0, name='Changed')
        self.assertEqual(result['code'], 'stale_folder_revision')

    def test_revision_backup_and_private_permissions(self):
        before = self.manager.path.read_bytes()
        self.create()
        backup = self.manager.root / 'folders.rev-00000000000000000000.json'
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)

    def test_deleted_seeded_store_fails_closed(self):
        self.manager.path.unlink()
        with self.assertRaisesRegex(FolderManagerError, 'folder_store_unavailable'): self.manager.public()

    def test_corrupt_store_fails_closed(self):
        self.manager.path.write_text('{}')
        with self.assertRaisesRegex(FolderManagerError, 'folder_store_unavailable'): self.manager.public()

    def test_symlink_store_fails_closed(self):
        self.manager.path.unlink(); self.manager.path.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(FolderManagerError, 'folder_store_unavailable'): self.manager.public()

    def test_world_readable_store_fails_closed(self):
        self.manager.path.chmod(0o644)
        with self.assertRaisesRegex(FolderManagerError, 'folder_store_unavailable'): self.manager.public()

    def test_trusted_service_mounts_prove_same_host_subfolder(self):
        row=dict(paths=dict(worker='/ebooks/sub',server='/srv/example-media/ebooks/sub',bo='/books/sub',jf='/data/ebooks/sub'))
        mounts=[dict(endpoint='worker',container='/ebooks',server='/srv/example-media/ebooks'),
                dict(endpoint='bo',container='/books',server='/srv/example-media/ebooks'),
                dict(endpoint='jf',container='/data',server='/srv/example-media')]
        self.assertTrue(validate_service_paths(row,mounts))
        wrong=copy.deepcopy(mounts);wrong[1]['server']='/other/ebooks'
        with self.assertRaisesRegex(FolderManagerError,'folder_service_mount_mismatch'):validate_service_paths(row,wrong)
        missing=mounts[:2]
        with self.assertRaisesRegex(FolderManagerError,'folder_service_mounts_unverified'):validate_service_paths(row,missing)
        nested=mounts+[dict(endpoint='bo',container='/books/sub/nested',server='/other')]
        with self.assertRaisesRegex(FolderManagerError,'folder_nested_service_mount'):validate_service_paths(row,nested)

    def test_public_has_no_secret_or_media_content(self):
        result = self.manager.public()
        self.assertEqual(set(result), {'revision','rows','kinds','endpoints','access_roots'})
        self.assertEqual(len(list(self.audio.iterdir())), 0)


if __name__ == '__main__': unittest.main()
