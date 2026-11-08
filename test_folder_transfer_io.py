"""Logical progress guards consult kernel/state only, including refusal paths."""
import builtins
from contextlib import contextmanager, ExitStack
import copy
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch

from folder_manager import FolderManager, FolderManagerError
from io_metrics import snapshot


class FolderTransferIOTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.media=self.root/'media';self.media.mkdir()
        self.audio=self.media/'audio';self.audio.mkdir()
        self.text=self.media/'text';self.text.mkdir()
        self.state=self.root/'nvme-state';self.state.mkdir()
        self.config={'folder_mappings':[
            dict(id='audio',kind='audiobooks',name='Audio',active=True,paths={'worker':str(self.audio)}),
            dict(id='text',kind='ebooks',name='Text',active=True,paths={'worker':str(self.text)})]}
        self.mount={'stable':'media-filesystem','runtime':['mount-1']}
        self.available=True;self.now=1000
        self.manager=self.make();self.manager.seed()

    def make(self):
        return FolderManager(self.state,self.config,
            [{'worker':str(self.audio)},{'worker':str(self.text)}],
            mount_checker=lambda _:self.available,generation_reader=lambda:self.mount,clock=lambda:self.now)

    def edit(self,action):
        state=json.loads(self.manager.path.read_text());state['revision']+=1;action(state)
        self.manager.path.write_text(json.dumps(state));self.manager.path.chmod(0o600)

    @contextmanager
    def no_media(self,manager=None):
        """Catch real path calls, not just the high-level physical-check helper."""
        manager=manager or self.manager
        descriptors={};attempts=[];media=PurePosixPath(str(self.media))
        original_open,original_close=os.open,os.close
        def check(path,dir_fd=None):
            if isinstance(path,int):candidate=descriptors.get(path)
            else:
                candidate=PurePosixPath(os.fsdecode(path))
                if not candidate.is_absolute() and dir_fd in descriptors:
                    candidate=descriptors[dir_fd]/candidate
            if candidate is not None and candidate.is_relative_to(media):
                attempts.append(str(candidate));raise AssertionError('media path accessed: '+str(candidate))
            return candidate
        def checked_open(path,flags,mode=0o777,*,dir_fd=None):
            candidate=check(path,dir_fd)
            result=original_open(path,flags,mode,dir_fd=dir_fd)
            if candidate is not None:descriptors[result]=candidate
            return result
        def checked_close(fd):
            descriptors.pop(fd,None);return original_close(fd)
        def wrap(function):
            def call(path,*args,**kwargs):
                check(path,kwargs.get('dir_fd'));return function(path,*args,**kwargs)
            return call
        with ExitStack() as stack:
            stack.enter_context(patch.object(manager,'check',side_effect=AssertionError('physical folder check')))
            stack.enter_context(patch.object(manager,'refresh',side_effect=AssertionError('folder refresh fallback')))
            stack.enter_context(patch.object(manager,'mount_checker',side_effect=AssertionError('physical mount lookup')))
            for module,name in ((os,'stat'),(os,'lstat'),(os,'scandir'),(builtins,'open'),(io,'open')):
                stack.enter_context(patch.object(module,name,wrap(getattr(module,name))))
            stack.enter_context(patch.object(os,'open',checked_open))
            stack.enter_context(patch.object(os,'close',checked_close))
            yield
        self.assertEqual(attempts,[])

    def refused(self,kind='ebooks',code='folder_verification_required',manager=None):
        manager=manager or self.manager
        with self.no_media(manager):
            with self.assertRaisesRegex(FolderManagerError,'^'+code+'$'):
                manager.require_observation(kind)

    def test_repeated_progress_guards_do_not_touch_media_and_return_same_generation(self):
        generation=self.manager.observation_generation('ebooks',context={'folder_mounts':{'worker':[]}})
        checks=snapshot(self.manager)['folder_checks']
        with self.no_media():
            for _ in range(100):
                self.assertEqual(self.manager.require_observation('ebooks',context={'folder_mounts':{'worker':[]}}),generation)
        self.assertEqual(snapshot(self.manager)['folder_checks'],checks)
        self.assertEqual(snapshot(self.manager)['folder_observation_guards'],100)

    def test_restart_after_many_years_uses_existing_nvme_history_without_seed_or_refresh(self):
        expected=self.manager.require_observation('ebooks');self.now+=20*365*86400
        manager=self.make()
        with self.no_media(manager):self.assertEqual(manager.require_observation('ebooks'),expected)
        self.assertEqual(snapshot(manager).get('folder_checks',0),0)

    def test_missing_corrupt_or_incomplete_witness_rejects_without_physical_fallback(self):
        row=self.manager._read()[0]['rows'][1]
        binding=self.manager._local_binding(row,'media-filesystem')
        name=self.manager.observations._name('folders','text')
        path=self.manager.observations.directory/name;original=path.read_bytes()
        path.unlink();self.refused()
        path.write_bytes(b'corrupt');path.chmod(0o600);self.refused()
        for value in ({'check':{'ok':True,'code':'folder_checked'}},
                      {'check':{'ok':True,'code':'folder_checked'},'identity':{'device':True,'inode':1}},
                      {'check':{'ok':1,'code':'folder_checked'},'identity':{'device':1,'inode':1}},
                      {'check':{'ok':False,'code':'folder_checked'},'identity':{'device':1,'inode':1}}):
            self.assertTrue(self.manager.observations.put('folders','text',binding,value));self.refused()
        path.write_bytes(original);path.chmod(0o600)
        with self.no_media():self.assertTrue(self.manager.require_observation('ebooks'))

    def test_changed_active_alias_rejects_only_affected_kind_without_path_lookup(self):
        audio=self.manager.require_observation('audiobooks')
        self.edit(lambda state:state['rows'][1]['paths'].update(worker=str(self.text/'unseen')))
        self.refused()
        with self.no_media():self.assertEqual(self.manager.require_observation('audiobooks'),audio)

    def test_unrelated_inactive_alias_and_display_name_do_not_revoke_evidence(self):
        expected=self.manager.require_observation('ebooks')
        def edit(state):
            state['rows'][1]['name']='Renamed books'
            state['rows'].append(dict(id='inactive-podcast',kind='podcasts',name='Inactive',active=False,
                paths={'worker':str(self.media/'absent')}))
        self.edit(edit)
        with self.no_media():self.assertEqual(self.manager.require_observation('ebooks'),expected)

    def test_changed_or_missing_kernel_mount_rejects_without_physical_fallback(self):
        self.mount={'stable':'different-filesystem','runtime':['mount-2']};self.refused()
        self.mount=None;self.refused(code='folder_generation_unavailable')

    def test_runtime_remount_revokes_previous_witness_even_when_stable_filesystem_is_same(self):
        self.mount['runtime']=['remounted'];self.refused()
        self.mount['runtime']=['mount-1'];self.refused()
        # A deliberate physical validation can establish the replacement epoch.
        self.manager.require('ebooks')
        with self.no_media():self.assertTrue(self.manager.require_observation('ebooks'))

    def test_failed_manual_check_revokes_logical_permission_across_restart(self):
        before=self.manager.require_observation('ebooks')
        self.available=False
        result=self.manager.mutate('test',dict(expected_revision=0,id='text'))
        self.assertFalse(result['ok']);self.refused()
        manager=self.make();self.refused(manager=manager)
        with self.no_media():self.assertTrue(self.manager.require_observation('audiobooks'))
        self.available=True;self.manager.require('ebooks')
        with self.no_media():self.assertNotEqual(self.manager.require_observation('ebooks'),before)

    def test_global_invalidation_requires_targeted_physical_verification(self):
        self.manager.invalidate();self.refused();self.refused('audiobooks')
        manager=self.make();self.refused(manager=manager)

    def test_absent_active_kind_and_invalid_kind_fail_without_media(self):
        self.edit(lambda state:state['rows'][1].update(active=False))
        self.refused(code='folder_mapping_missing')
        self.refused('invalid',code='invalid_folder_kind')

    def test_epoch_corruption_does_not_resurrect_old_positive_history(self):
        name=self.manager.observations._name('folder-epoch','ebooks')
        (self.manager.observations.directory/name).write_bytes(b'corrupt')
        manager=self.make();self.refused(manager=manager)
        with self.no_media(manager):self.assertTrue(manager.require_observation('audiobooks'))


if __name__=='__main__':unittest.main()
