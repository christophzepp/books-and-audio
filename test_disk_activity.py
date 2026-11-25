import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid

from disk_activity import Sampler, samples, WEEK


class DiskActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.control = root / 'control'
        self.control.mkdir()
        self.sysfs = root / 'sys'
        self.blockclass = self.sysfs / 'class/block'
        self.blockclass.mkdir(parents=True)
        self.boot = root / 'boot_id'
        self.boot.write_text(str(uuid.uuid4()))
        self.s = self.sampler()
        self.config(['sdb', 'sdc'])
        self.b = self.device('sdb', '8:16', 1)
        self.c = self.device('sdc', '8:32', 2)

    def sampler(self):
        return Sampler(self.control, sysfs=self.sysfs, boot_file=self.boot)

    def config(self, devices):
        (self.control / 'media-disks.json').write_text(json.dumps(dict(devices=devices)))

    def device(self, name, number, seq):
        block = self.sysfs / 'devices/pci0000' / f'target{seq}' / 'block' / name
        (block / 'queue').mkdir(parents=True)
        (block / 'queue/rotational').write_text('1')
        (block / 'queue/iostats').write_text('1')
        (block / 'dev').write_text(number)
        (block / 'diskseq').write_text(str(seq))
        (block / 'device').symlink_to(block.parents[1])
        (self.blockclass / name).symlink_to(block)
        self.stats(block, 8, 9)
        return block

    def stats(self, block, reads, writes):
        (block / 'stat').write_text(f'{reads} 0 2 3 {writes} 5 4 3 0 0 0 0 0 0 0 0 0')

    def records(self):
        return samples(self.control, 0, 5000000)

    def tick(self, at):
        self.s.tick(at, at)

    def test_data_and_parity_deltas_and_idle_are_distinct_from_missing_minutes(self):
        self.tick(1200)
        self.stats(self.b, 11, 12)
        self.stats(self.c, 10, 13)
        self.tick(1260)
        self.tick(1320)
        self.assertEqual(self.records(), [dict(at=1260,reads=5,writes=7),dict(at=1320,reads=0,writes=0)])
        self.assertEqual(self.s.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(samples(self.control,1200,1270), self.records()[:1])

    def test_restart_replacement_or_reset_never_creates_false_delta(self):
        self.tick(1200)
        self.s = self.sampler()
        self.tick(1260)
        self.boot.write_text(str(uuid.uuid4()))
        self.stats(self.b, 90000, 30000)
        self.tick(1320)
        (self.b / 'diskseq').write_text('3')
        self.tick(1380)
        self.stats(self.b, 8, 9)
        self.stats(self.c, 350000, 60000)
        self.tick(1440)
        self.assertEqual(self.records(), [])
        self.tick(1500)
        self.assertEqual(self.records(), [dict(at=1500,reads=0,writes=0)])

    def test_partial_offline_late_sample_and_changed_selection_leave_gaps(self):
        self.tick(1200)
        (self.c / 'stat').unlink()
        self.tick(1260)
        self.stats(self.c, 10000, 20000)
        self.tick(1320)
        self.tick(1500)
        self.config(['sdb'])
        self.tick(1560)
        self.s.tick(1680, 1620)  # Wall clock jumped a minute.
        self.assertEqual(self.records(), [])
        self.s.tick(1740,1680)
        self.assertEqual(self.records(), [dict(at=1740,reads=0,writes=0)])

    def test_omitted_empty_or_invalid_allowlist_performs_no_sysfs_probe(self):
        with patch('disk_activity.counters', side_effect=AssertionError('unselected HDD')) as read:
            (self.control / 'media-disks.json').unlink()
            self.tick(1200)
            for config in ([], ['../../sda'], ['sda','sda'], ['sdb/partition']):
                self.config(config)
                self.tick(1260)
            read.assert_not_called()
            self.assertFalse(self.s.path.exists())
            self.assertEqual(self.records(), [])
            self.assertFalse(self.s.path.exists())

    def test_disabled_kernel_accounting_is_a_gap_even_when_counters_are_zero(self):
        self.tick(1200)
        (self.c / 'queue/iostats').write_text('0')
        self.tick(1260)
        (self.c / 'queue/iostats').write_text('1')
        self.tick(1320)
        self.assertEqual(self.records(), [])
        self.tick(1380)
        self.assertEqual(self.records(), [dict(at=1380,reads=0,writes=0)])

    def test_rejects_virtual_disks_ssd_partitions_and_missing_kernel_identity(self):
        self.config(['sdb'])
        (self.b/'queue/rotational').write_text('0')
        self.tick(1200)
        self.assertIsNone(self.s.previous)
        (self.b/'queue/rotational').write_text('1')
        (self.b/'partition').touch()
        self.tick(1260)
        self.assertIsNone(self.s.previous)
        (self.b/'partition').unlink()
        (self.b/'diskseq').unlink()
        self.tick(1320)
        self.assertIsNone(self.s.previous)
        (self.b/'diskseq').write_text('5')
        (self.blockclass/'sdb').unlink()
        dest = self.sysfs/'devices/virtual/block/sdb'
        dest.parent.mkdir(parents=True)
        self.b.rename(dest)
        (self.blockclass/'sdb').symlink_to(dest)
        self.tick(1380)
        self.assertIsNone(self.s.previous)

    def test_retention_and_disabled_sampler_leave_previous_source_separate(self):
        self.tick(1200)
        self.tick(1260)
        self.tick(1320 + WEEK)
        self.tick(1380 + WEEK)
        self.assertEqual(self.records(), [dict(at=1380+WEEK,reads=0,writes=0)])
        self.assertFalse((self.control/'disk-observations.json').exists())
        self.config([])
        self.tick(1440 + WEEK)
        self.assertIsNone(self.s.previous)
