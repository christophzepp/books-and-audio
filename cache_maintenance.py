"""Incremental bookkeeping for private disposable NVMe cache directories.

This index is never read authority. Cache readers still validate each payload.
Directory locks serialize writers across processes; an external rename, removal,
restart or policy change rebuilds bookkeeping from owned regular cache leaves.
Ordinary insertions update one heap entry instead of scanning every old file.
"""
from collections import OrderedDict
from contextlib import contextmanager
import fcntl
import heapq
import os
import re
import stat


MAX_CATALOGS = 8
MAX_CATALOG_ENTRIES = 200000
_catalogs = OrderedDict()


def _stamp(info):
    return info.st_mtime_ns, info.st_ctime_ns


class Catalog:
    def __init__(self, directory, max_bytes, max_entries, pinned):
        self.directory = directory
        self.limits = max_bytes, max_entries, frozenset(pinned)
        self.rows = {}
        self.heap = []
        self.used = 0
        self.sequence = 0
        self.changed = False
        for entry in os.scandir(directory):
            if not re.fullmatch(r'[0-9a-f]{64}\.json', entry.name):
                continue
            try: info = entry.stat(follow_symlinks=False)
            except FileNotFoundError: continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                self._remember(entry.name, info)
        self.stamp = _stamp(os.fstat(directory))

    def _remember(self, name, info):
        previous = self.rows.get(name)
        if previous is not None: self.used -= previous[0]
        self.sequence += 1
        row = info.st_size, info.st_mtime_ns, self.sequence
        self.rows[name] = row
        self.used += row[0]
        if name not in self.limits[2]:
            heapq.heappush(self.heap, (row[1], name, row[2]))

    def record(self, name):
        if not re.fullmatch(r'[0-9a-f]{64}\.json', name):
            raise ValueError('Invalid cache leaf')
        info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError('Cache entry must be an owned regular file')
        self._remember(name, info)
        self.changed = True

    def discard(self, name):
        previous = self.rows.pop(name, None)
        if previous is not None: self.used -= previous[0]
        self.changed = True

    def prune(self, current):
        maximum_bytes, maximum_entries, pinned = self.limits
        deferred = []
        while self.used > maximum_bytes or len(self.rows) > maximum_entries:
            if not self.heap:
                if not deferred: break  # Mount epochs are deliberately pinned.
                self.heap, deferred = deferred, []
                heapq.heapify(self.heap)
                current = None
            modified, name, serial = heapq.heappop(self.heap)
            row = self.rows.get(name)
            if row is None or (row[1], row[2]) != (modified, serial): continue
            if name == current:
                deferred.append((modified, name, serial))
                continue
            try: os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError: pass
            self.discard(name)
        for item in deferred: heapq.heappush(self.heap, item)
        # Repeated replacement of one key must not grow stale heap entries forever.
        if len(self.heap) > 2 * len(self.rows) + 64:
            self.heap = [(row[1], name, row[2]) for name, row in self.rows.items() if name not in pinned]
            heapq.heapify(self.heap)


@contextmanager
def mutation(directory, *, max_bytes, max_entries, pinned=()):
    """Hold only an NVMe directory lock; never inspect stored media paths."""
    fcntl.flock(directory, fcntl.LOCK_EX)
    identity = None
    try:
        info = os.fstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise OSError('Cache directory must be private and owned')
        identity = info.st_dev, info.st_ino
        limits = max_bytes, max_entries, frozenset(pinned)
        catalog = _catalogs.get(identity)
        if catalog is None or catalog.stamp != _stamp(info) or catalog.limits != limits:
            catalog = Catalog(directory, *limits)
        catalog.directory = directory
        catalog.changed = False
        yield catalog
        if catalog.changed: os.fsync(directory)
        catalog.stamp = _stamp(os.fstat(directory))
        _catalogs.pop(identity, None)
        _catalogs[identity] = catalog
        while len(_catalogs) > MAX_CATALOGS or sum(len(c.rows) for c in _catalogs.values()) > MAX_CATALOG_ENTRIES:
            _catalogs.popitem(last=False)
    except BaseException:
        # A write may have reached rename before failing. Rescan next mutation.
        if identity is not None: _catalogs.pop(identity, None)
        raise
    finally:
        fcntl.flock(directory, fcntl.LOCK_UN)
