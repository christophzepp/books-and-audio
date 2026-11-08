"""Durable ebook observations survive time, restarts and unrelated additions."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jellyfin_ebook import EbookCoordinator
from test_ebook_io import EbookIOTests


class EbookStandbyTests(EbookIOTests):
    def test_year_later_and_recreated_bridge_preserve_checkpoint_without_media(self):
        self.b.media_observation_generation=lambda kind:'same-media-mount'
        self.run_cycle()
        before=copy.deepcopy(self.store)
        rebuilt=SimpleNamespace(**{k:v for k,v in vars(self.b).items() if not k.startswith('_')})
        rebuilt.clock=lambda:1000+400*86400
        self.b=rebuilt;self.c=EbookCoordinator(rebuilt)
        with patch('jellyfin_ebook.Path.resolve',side_effect=AssertionError('media lookup')), \
             patch('epub_index_cache.verified_bytes',side_effect=AssertionError('EPUB read')):
            self.assertEqual(self.run_cycle(),'unchanged')
        self.assertEqual(self.store,before);self.assertFalse(self.writes)

    def test_unrelated_podcast_or_audio_folders_and_display_title_do_not_invalidate(self):
        self.b.media_observation_generation=lambda kind:'same-ebook-mount'
        self.run_cycle();before=copy.deepcopy(self.store)
        self.b.config.update(folder_mappings=[dict(id='pod',kind='podcasts',active=True,paths={'worker':'/podcasts'}),
            dict(id='audio',kind='audiobooks',active=True,paths={'worker':'/audiobooks'})],podcasts={'enabled':True})
        self.m=dict(self.m,title='Display title renamed')
        with patch('jellyfin_ebook.Path.resolve',side_effect=AssertionError('unrelated media lookup')):
            self.assertEqual(self.run_cycle(),'unchanged')
        self.assertEqual(self.store,before)

    def test_missing_mount_generation_disables_idle_observation(self):
        self.b.media_observation_generation=lambda kind:'valid'
        self.run_cycle();self.b.media_observation_generation=lambda kind:None
        with patch('epub_index_cache.verified_bytes',side_effect=ValueError('current file required')):
            with self.assertRaisesRegex(ValueError,'current file required'):self.run_cycle()
        self.assertFalse(self.writes)

    def test_restarted_hidden_file_replacement_is_checked_before_changed_progress(self):
        self.b.media_observation_generation=lambda kind:'same-media-mount';self.run_cycle()
        self.b.__dict__.pop('_ebook_index_cache',None);self.c=EbookCoordinator(self.b)
        data=self.path.read_bytes();self.path.write_bytes(data.replace(b'Hello',b'Other',1))
        self.set_bo(3)
        with self.assertRaisesRegex(ValueError,'changed'):self.run_cycle()
        self.assertFalse(self.writes)


if __name__=='__main__':unittest.main()
