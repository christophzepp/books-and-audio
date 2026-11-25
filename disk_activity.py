"""Optional Linux block-layer read/write counters for explicitly selected media HDDs.

The host selects whole HDDs in /state/control/media-disks.json, e.g.
{"devices":["sdb","sdc"]}. Omitting the file or supplying an empty list
disables collection. Device membership follows those kernel names; verify
the selection after hardware reassignment. Only cached kernel attributes are
read. Collection neither opens block devices nor sends power or SMART commands.
"""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

INTERVAL = 60
WEEK = 7 * 86400
DATABASE = 'media-kernel-activity.sqlite'


def selected(control):
    path = control / 'media-disks.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ()
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Invalid HDD configuration file')
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('HDD configuration too large')
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'devices'}:
        raise ValueError('Invalid HDD configuration')
    devices = value['devices']
    if (not isinstance(devices, list) or len(devices) > 32
            or any(type(n) is not str or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,31}', n)
                   for n in devices) or len(set(devices)) != len(devices)):
        raise ValueError('Invalid list of HDDs')
    return tuple(sorted(devices))


def counters(sysfs, name):
    node = sysfs / 'class/block' / name
    block = node.resolve(strict=True)
    relative = block.relative_to((sysfs / 'devices').resolve(strict=True))
    if (not relative.parts or relative.parts[0] == 'virtual' or block.name != name
            or (block / 'partition').exists() or not (block / 'device').is_symlink()
            or (block / 'queue/rotational').read_text().strip() != '1'):
        raise ValueError('A selected device is not a whole physical rotating disk')
    if (block / 'queue/iostats').read_text().strip() != '1':
        raise ValueError('Block device counters are disabled')
    # diskseq identifies the gendisk instance, including replacement hardware
    # appearing under the same sysfs path and major:minor during this boot.
    seq = (block / 'diskseq').read_text().strip()
    dev = (block / 'dev').read_text().strip()
    if not seq.isascii() or not seq.isdigit() or int(seq) < 1 or not re.fullmatch(r'[0-9]+:[0-9]+', dev):
        raise ValueError('Missing kernel disk identity')
    values = (block / 'stat').read_text().split()
    if (len(values) < 11 or any(not n.isascii() or not n.isdigit() for n in values)
            or (block / 'diskseq').read_text().strip() != seq or node.resolve(strict=True) != block):
        raise ValueError('Incomplete kernel counters or device changed')
    return (str(relative), dev, seq), (int(values[0]), int(values[4]))


class Sampler:
    def __init__(self, control, *, sysfs=Path('/sys'), boot_file=Path('/proc/sys/kernel/random/boot_id')):
        self.control = Path(control)
        self.path = self.control / DATABASE
        self.sysfs, self.boot_file = sysfs, boot_file
        self.previous = None
        self.last_error = None

    def tick(self, wall, mono):
        try:
            devices = selected(self.control)
            if not devices:
                self.previous = None
                self.last_error = None
                return
            boot = str(uuid.UUID(self.boot_file.read_text().strip()))
            readings = [counters(self.sysfs, name) for name in devices]
            identities = tuple((name, identity) for name, (identity, _) in zip(devices, readings))
            values = tuple(counts for _, counts in readings)
            previous, self.previous = self.previous, (wall, mono, boot, identities, values)
            if previous is not None:
                old_wall, old_mono, old_boot, old_ids, old_values = previous
                elapsed = mono - old_mono
                if (old_boot == boot and old_ids == identities and 50 <= elapsed <= 70
                        and abs(wall - old_wall - elapsed) <= 2
                        and all(r >= a and w >= b for (r,w),(a,b) in zip(values,old_values))):
                    self.save(int(wall) // 60 * 60, sum(r-a for (r,_),(a,_) in zip(values,old_values)),
                              sum(w-b for (_,w),(_,b) in zip(values,old_values)))
            self.last_error = None
        except (ValueError, OSError, sqlite3.Error, OverflowError, RuntimeError) as exc:
            self.previous = None
            if self.last_error != type(exc).__name__:
                logging.warning('Linux-Festplattenmessung unterbrochen (%s).', type(exc).__name__)
            self.last_error = type(exc).__name__

    def save(self, at, reads, writes):
        # At most one small NVMe transaction per observed minute, including
        # genuinely idle minutes. Lost coverage always remains a gap.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS samples(at INTEGER PRIMARY KEY,reads INTEGER,writes INTEGER)')
                db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?)', (at, reads, writes))
                db.execute('DELETE FROM samples WHERE at < ?', (at - WEEK,))
        finally:
            db.close()


def samples(control, start, end):
    path = Path(control) / DATABASE
    if not path.is_file() or path.is_symlink():
        return []
    try:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
        try:
            rows = db.execute('SELECT at,reads,writes FROM samples WHERE at BETWEEN ? AND ? ORDER BY at',
                              (start, end))
            return [dict(at=at, reads=reads, writes=writes) for at,reads,writes in rows]
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return []


@contextmanager
def sampling(control):
    sampler = Sampler(control)
    stop = threading.Event()
    def poll():
        while not stop.is_set():
            started = time.monotonic()
            sampler.tick(time.time(), started)
            stop.wait(max(0, INTERVAL - (time.monotonic() - started)))
    worker = threading.Thread(target=poll, daemon=True, name='hdd-io')
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=3)
