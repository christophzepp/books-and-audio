"""Disposable, bounded EPUB indexes with explicit physical/history entry points.

The directory belongs on the coordinator's SSD state volume, never its media
mount. A cached index is derived data, not a current physical-file verification.
The coordinator separately binds historical evidence to current API identities.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from epub_locations import EpubLocations
from io_metrics import increment


MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_DISK_BYTES = 1024 * 1024 * 1024
MAX_DISK_ENTRIES = 4096
MAX_MEMORY_ENTRIES = 4


def fingerprint(value):
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def verified_bytes(path, expected_hash, expected_size, owner=None):
    """Hash the current original, rejecting in-place edits and replacements."""
    requested=Path(path)
    initial_leaf=requested.lstat()
    if not stat.S_ISREG(initial_leaf.st_mode):raise ValueError('EPUB symlink or non-file excluded')
    # Stable platform aliases such as macOS /var -> /private/var may exist
    # before validation. They cannot redirect to a different canonical path
    # during this read, even if both names still reach the same file inode.
    canonical=requested.resolve(strict=True)
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
    directory=os.open('/',flags|os.O_DIRECTORY)
    try:
        for component in canonical.parts[1:-1]:
            child=os.open(component,flags|os.O_DIRECTORY,dir_fd=directory)
            os.close(directory);directory=child
        descriptor=os.open(canonical.name,flags,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(descriptor,'rb') as source:
        before=os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size!=expected_size
                or fingerprint(before)!=fingerprint(initial_leaf)):
            raise ValueError('EPUB size changed')
        data=source.read()
        if owner is not None:
            increment(owner,'ebook_source_reads')
            increment(owner,'ebook_source_read_bytes',len(data))
        after=os.fstat(source.fileno())
    final_leaf=requested.lstat()
    if (requested.resolve(strict=True)!=canonical or not stat.S_ISREG(final_leaf.st_mode)
            or fingerprint(before)!=fingerprint(after)
            or fingerprint(after)!=fingerprint(final_leaf)):
        raise ValueError('EPUB changed during verification')
    if len(data)!=expected_size or hashlib.sha256(data).hexdigest()!=expected_hash:
        raise ValueError('EPUB file changed')
    return data


class EpubIndexCache:
    def __init__(self, directory=None, max_bytes=MAX_DISK_BYTES, owner=None,
                 max_entries=MAX_DISK_ENTRIES):
        if type(max_bytes) is not int or not 1<=max_bytes<=16*1024*1024*1024:
            raise ValueError('Invalid EPUB index cache byte limit')
        if type(max_entries) is not int or not 1<=max_entries<=100000:
            raise ValueError('Invalid EPUB index cache entry limit')
        self.directory=Path(directory) if directory is not None else None
        self.max_bytes=max_bytes
        self.max_entries=max_entries
        self.owner=owner
        self.memory={}

    def _key(self, sha):
        if not isinstance(sha,str) or len(sha)!=64 or any(x not in '0123456789abcdef' for x in sha):
            raise ValueError('Invalid EPUB content hash')
        return hashlib.sha256((EpubLocations.CACHE_VERSION+':'+sha).encode()).hexdigest()

    def _directory(self):
        if self.directory is None:return None
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise OSError('EPUB index cache must be a private owned directory')
        return self.directory

    @staticmethod
    def _encode(value):
        return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

    def _load(self, key, sha):
        directory=self._directory()
        if directory is None:return None
        descriptor=os.open(directory/(key+'.json'),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_INDEX_BYTES or info.st_uid!=os.geteuid():
                return None
            raw=source.read(MAX_INDEX_BYTES+1)
        if len(raw)>MAX_INDEX_BYTES:return None
        row=json.loads(raw)
        if not isinstance(row,dict) or set(row)!={'version','sha256','data','data_sha256'}:
            return None
        if row['version']!=EpubLocations.CACHE_VERSION or row['sha256']!=sha:
            return None
        if hashlib.sha256(self._encode(row['data'])).hexdigest()!=row['data_sha256']:
            return None
        return EpubLocations.from_cache_data(row['data'],sha)

    def _store(self, key, sha, index):
        directory=self._directory()
        if directory is None:return
        data=index.cache_data()
        raw=self._encode(dict(version=EpubLocations.CACHE_VERSION,sha256=sha,data=data,
                              data_sha256=hashlib.sha256(self._encode(data)).hexdigest()))
        if len(raw)>min(MAX_INDEX_BYTES,self.max_bytes):return
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=directory,prefix='.index-',delete=False) as target:
                temporary=Path(target.name)
                os.fchmod(target.fileno(),0o600)
                target.write(raw);target.flush();os.fsync(target.fileno())
            os.replace(temporary,directory/(key+'.json'))
            temporary=None
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
        # Only our fixed-name regular files are eligible for eviction.
        entries=[]
        for item in directory.glob('*.json'):
            if len(item.stem)!=64 or any(x not in '0123456789abcdef' for x in item.stem):continue
            info=item.lstat()
            if stat.S_ISREG(info.st_mode):entries.append((info.st_mtime_ns,info.st_size,item))
        entries.sort(reverse=True)
        used=0
        for number,(_,size,item) in enumerate(entries):
            used+=size
            if number>=self.max_entries or used>self.max_bytes:item.unlink(missing_ok=True)

    def cached(self, expected_hash):
        """Load only validated SSD data, without inspecting any media path.

        This does not establish current source bytes. Callers must separately
        validate the historical proof and current logical source identity.
        """
        key=self._key(expected_hash)
        index=self.memory.get(key)
        if index is not None and self.owner is not None:increment(self.owner,'ebook_index_ram_hits')
        if index is None:
            try:index=self._load(key,expected_hash)
            except (OSError,ValueError,TypeError,KeyError,RecursionError):index=None
            if index is not None and self.owner is not None:increment(self.owner,'ebook_index_disk_hits')
        if index is not None:self._remember(key,index)
        return index

    def _remember(self,key,index):
        self.memory.pop(key,None);self.memory[key]=index
        while len(self.memory)>MAX_MEMORY_ENTRIES:self.memory.pop(next(iter(self.memory)))

    def forget(self,expected_hash):
        """Revoke derived evidence after a failed explicit physical check."""
        key=self._key(expected_hash)
        self.memory.pop(key,None)
        try:
            directory=self._directory()
            if directory is not None:(directory/(key+'.json')).unlink(missing_ok=True)
        except (OSError,ValueError):pass

    def get(self, path, expected_hash, expected_size):
        """Explicit physical validation; even a RAM hit hashes the original."""
        try:data=verified_bytes(path,expected_hash,expected_size,self.owner)
        except (OSError,ValueError):
            self.forget(expected_hash)
            raise
        key=self._key(expected_hash)
        index=self.cached(expected_hash)
        if index is None:
            index=EpubLocations.from_bytes(data,expected_hash)
            if self.owner is not None:increment(self.owner,'ebook_index_builds')
            try:self._store(key,expected_hash,index)
            except (OSError,ValueError,TypeError):pass  # Disposable cache failure never authorises a write.
        self._remember(key,index)
        return index

    def seed(self, index):
        """Reuse an index just built by validated discovery, never its authority.

        A later get() still hashes the current original before accepting this
        derived data, so seeding cannot bypass file-change detection.
        """
        if not isinstance(index,EpubLocations):raise ValueError('Invalid EPUB index seed')
        key=self._key(index.hash)
        try:self._store(key,index.hash,index)
        except (OSError,ValueError,TypeError):pass
        self.memory.pop(key,None);self.memory[key]=index
        while len(self.memory)>MAX_MEMORY_ENTRIES:self.memory.pop(next(iter(self.memory)))
