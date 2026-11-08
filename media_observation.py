"""Durable historical observations on the coordinator state disk.

These records are disposable indexes, never fresh evidence or write authority.
Only callers decide whether current service identities still match. No method
resolves or opens any path from a stored value. No record expires by age.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from cache_maintenance import mutation

VERSION = 1
MAX_RECORD_BYTES = 128 * 1024
MAX_DISK_BYTES = 128 * 1024 * 1024
MAX_DISK_ENTRIES = 100000


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class ObservationStore:
    def __init__(self,root,*,max_bytes=MAX_DISK_BYTES,max_entries=MAX_DISK_ENTRIES):
        self.root=Path(root) if root is not None else None
        self.max_bytes=max_bytes;self.max_entries=max_entries
        if type(max_bytes)is not int or max_bytes<1 or type(max_entries)is not int or max_entries<1:
            raise ValueError('Invalid observation cache bounds')
        self.directory=self.root/'media-observations' if self.root is not None else None

    def _name(self,namespace,key):
        if not isinstance(namespace,str) or not re.fullmatch('[a-z0-9_-]{1,40}',namespace):
            raise ValueError('Invalid observation namespace')
        return fingerprint([namespace,key])+'.json'

    def _directory(self,create=False):
        if self.directory is None:return None
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise ValueError('Observation cache requires absolute state path')
        parent=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0))
        try:
            if create:
                try:
                    os.mkdir('media-observations',mode=0o700,dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:pass
            descriptor=os.open('media-observations',os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0),dir_fd=parent)
        finally:os.close(parent)
        info=os.fstat(descriptor)
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
            os.close(descriptor);raise OSError('Observation cache requires private owned state directory')
        return descriptor

    def get(self,namespace,key,identity):
        directory=None
        try:
            name=self._name(namespace,key);directory=self._directory()
            if directory is None:return None
            fd=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=directory)
            with os.fdopen(fd,'rb') as source:
                info=os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                        or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>MAX_RECORD_BYTES):return None
                raw=source.read(MAX_RECORD_BYTES+1)
            if len(raw)>MAX_RECORD_BYTES:return None
            record=json.loads(raw)
            if (not isinstance(record,dict) or set(record)!={'version','namespace','key','identity','value','sha256'}
                    or type(record['version'])is not int or record['version']!=VERSION or record['namespace']!=namespace or record['key']!=fingerprint(key)
                    or record['identity']!=fingerprint(identity)):
                return None
            body={k:v for k,v in record.items() if k!='sha256'}
            if record['sha256']!=fingerprint(body):return None
            return copy.deepcopy(record['value'])
        except (OSError,ValueError,TypeError,KeyError,RecursionError):return None
        finally:
            if directory is not None:os.close(directory)

    def put(self,namespace,key,identity,value):
        directory=None;temporary=None
        try:
            name=self._name(namespace,key)
            body=dict(version=VERSION,namespace=namespace,key=fingerprint(key),identity=fingerprint(identity),value=value)
            record=dict(body,sha256=fingerprint(body));raw=encoded(record)
            if len(raw)>min(MAX_RECORD_BYTES,self.max_bytes):return False
            directory=self._directory(create=True)
            if directory is None:return False
            epochs={self._name('folder-epoch',kind) for kind in ('all','audiobooks','ebooks','podcasts')}
            with mutation(directory,max_bytes=self.max_bytes,max_entries=self.max_entries,pinned=epochs) as catalog:
                # A random owned temporary plus directory-relative rename never
                # follows a substituted cache leaf to a media or user file.
                temporary='.observation-'+os.urandom(16).hex()
                fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=directory)
                with os.fdopen(fd,'wb') as target:
                    target.write(raw);target.flush();os.fsync(target.fileno())
                os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory);temporary=None
                catalog.record(name);catalog.prune(name)
            return True
        except (OSError,ValueError,TypeError,KeyError,RecursionError):return False
        finally:
            if directory is not None:
                if temporary is not None:
                    try:os.unlink(temporary,dir_fd=directory)
                    except OSError:pass
                os.close(directory)

    def forget(self,namespace,key):
        directory=None
        try:
            name=self._name(namespace,key);directory=self._directory()
            if directory is not None:
                epochs={self._name('folder-epoch',kind) for kind in ('all','audiobooks','ebooks','podcasts')}
                with mutation(directory,max_bytes=self.max_bytes,max_entries=self.max_entries,pinned=epochs) as catalog:
                    try:os.unlink(name,dir_fd=directory)
                    except FileNotFoundError:return
                    catalog.discard(name)
        except (OSError,ValueError,TypeError):pass
        finally:
            if directory is not None:os.close(directory)
