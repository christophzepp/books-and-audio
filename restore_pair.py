"""Publish one completed backup generation for the offline recovery checker (standard library only)."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time


class PairError(ValueError):
    def __init__(self, code, captured_at=None):
        super().__init__(code)
        self.captured_at = captured_at


def need(condition):
    if not condition:
        raise PairError('backup_pair')


@contextmanager
def regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        need(stat.S_ISREG(os.fstat(fd).st_mode))
        yield stream


def document(path):
    with regular(path) as stream:
        raw = stream.read(8 * 1024**2 + 1)
    need(len(raw) <= 8 * 1024**2)
    result = json.loads(raw)
    need(isinstance(result, dict))
    return result


def digest(path):
    with regular(path) as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
        return checksum.hexdigest()


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.publish-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(catalog, *, publish=False):
    flags = os.O_RDWR | os.O_CREAT if publish else os.O_RDONLY
    with os.fdopen(os.open(catalog/'pair.lock', flags | os.O_NOFOLLOW, 0o600), 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if publish else fcntl.LOCK_SH)
        yield


def members(main, relay):
    need(not any(p.is_symlink() for p in (main, relay, relay/'state')))
    manifest = document(main/'manifest.json')
    image = document(relay/'image.json')
    need(manifest.get('schema') == 1 and isinstance(manifest.get('protected'), dict))
    if 'snapshot_id' in manifest or 'snapshot_id' in image:
        group = manifest.get('snapshot_id')
        need(isinstance(group, str) and re.fullmatch('[A-Za-z0-9_-]{12,128}', group)
             and group == image.get('snapshot_id') and 'captured_at' in manifest and 'captured_at' in image)
    else:
        need(re.fullmatch('[0-9a-f]{40}', manifest.get('revision', ''))
             and manifest['revision'] == image.get('commit') and manifest.get('version')
             and manifest['version'] == image.get('version'))
    files = {'main/manifest.json': main/'manifest.json', 'main/stack-before.tar.gz': main/'stack-before.tar.gz',
             'relay/image.json': relay/'image.json', 'relay/config.json': relay/'config.json',
             'relay/state/relay.sqlite': relay/'state/relay.sqlite'}
    for suffix in ('-wal', '-journal'):
        name = 'state/relay.sqlite' + suffix
        if (relay/name).exists() or (relay/name).is_symlink():
            files['relay/' + name] = relay/name
    cfg = document(relay/'config.json')
    if cfg.get('native_source') is not None:
        name = Path(cfg['native_source']['api_key_file']).name
        need(name not in ('', '.', '..', 'image.json', 'config.json', 'state', 'relay.sqlite'))
        files['relay/'+name] = relay/name
    return manifest, files


def check_times(main_at, relay_at, now):
    need(all(type(t) is int and 0 < t <= now + 300 for t in (main_at, relay_at)))
    need(abs(main_at-relay_at) <= 86400)
    return min(main_at, relay_at)


def current_capture(catalog):
    """Publication timestamp and producer snapshot ID, without reading the backup payloads."""
    try:
        with locked(catalog):
            return _current_capture(catalog)
    except FileNotFoundError:
        if (catalog/'current.json').exists():
            raise PairError('backup_pair') from None
        return None


def _current_capture(catalog):
    index = catalog/'current.json'
    if not index.exists():
        return None
    name = document(index).get('pair')
    need(isinstance(name, str) and re.fullmatch('[0-9a-f]{64}', name))
    root = catalog/name
    need(not root.is_symlink() and digest(root/'pair.json') == name)
    meta = document(root/'pair.json')
    return min(meta['main_at'], meta['relay_at']), document(root/'main/manifest.json').get('snapshot_id')


def publish(catalog, main, relay, now=None):
    now = int(time.time()) if now is None else now
    paths = [catalog.resolve(), main.resolve(), relay.resolve()]
    for i, a in enumerate(paths):
        need(all(not a.is_relative_to(b) for j,b in enumerate(paths) if j != i))
    need(not catalog.is_symlink())
    manifest, files = members(main, relay)
    main_at = manifest.get('captured_at', int((main/'manifest.json').stat().st_mtime))
    relay_at = document(relay/'image.json').get('captured_at', int((relay/'image.json').stat().st_mtime))
    check_times(main_at, relay_at, now)
    catalog.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(catalog.is_dir() and not catalog.stat().st_mode & 0o077)
    with locked(catalog, publish=True):
        previous_capture = _current_capture(catalog)
        need(not previous_capture or min(main_at,relay_at) >= previous_capture[0])
        index = catalog/'current.json'
        previous = document(index)['pair'] if index.exists() else None
        if previous is not None:
            need(isinstance(previous,str) and re.fullmatch('[a-f0-9]{64}', previous))
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=catalog) as temporary:
            stage = Path(temporary)/'pair'
            hashes = {}
            for name, source in files.items():
                need(source.is_file() and not source.is_symlink())
                target = stage/name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with regular(source) as src, target.open('xb') as dst:
                    os.fchmod(dst.fileno(), 0o600)
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                hashes[name] = digest(target)
            need(hashes['main/stack-before.tar.gz'] == manifest['archive_sha256'])
            meta = dict(schema=1, main_at=main_at, relay_at=relay_at, files=hashes)
            atomic_json(stage/'pair.json', meta)
            pair = digest(stage/'pair.json')
            dest = catalog/pair
            if not dest.exists():
                for directory in (stage/'main', stage/'relay/state', stage/'relay', stage):
                    sync_dir(directory)
                stage.rename(dest)
            else:
                need(not dest.is_symlink() and digest(dest/'pair.json') == pair
                     and all(digest(dest/name)==sha for name,sha in hashes.items()))
            atomic_json(index, {'schema': 1, 'pair': pair})
            if previous and previous != pair:
                old = catalog/previous
                # Only remove the previously registered catalog copy, never a source directory.
                if old.is_dir() and not old.is_symlink() and digest(old/'pair.json') == previous:
                    shutil.rmtree(old)
    return pair


def selected(catalog, max_age_days, now):
    """Call under a shared catalog lock. Do not open backup files between scheduled checks."""
    index = document(catalog/'current.json')
    pair = index.get('pair')
    need(index.get('schema') == 1 and isinstance(pair, str) and re.fullmatch('[a-f0-9]{64}', pair))
    root = catalog/pair
    need(not root.is_symlink() and digest(root/'pair.json') == pair)
    meta = document(root/'pair.json')
    need(meta.get('schema') == 1)
    captured = check_times(meta['main_at'], meta['relay_at'], now)
    if now - captured > max_age_days * 86400:
        raise PairError('backup_stale', captured)
    _, files = members(root/'main', root/'relay')
    need(files.keys()==meta['files'].keys())
    # The drill verifies the main archive against the manifest during restoration.
    need(document(root/'main/manifest.json')['archive_sha256'] == meta['files']['main/stack-before.tar.gz'])
    need(all(digest(path)==meta['files'][name] for name,path in files.items() if name!='main/stack-before.tar.gz'))
    return root, captured


def main():
    parser = argparse.ArgumentParser(description='Ein abgeschlossenes Sicherungspaar aus demselben Rollout für die Wiederherstellungsprobe registrieren.')
    for name in ('main','relay','catalog'):
        parser.add_argument('--'+name, type=Path, required=True)
    args=parser.parse_args()
    try:
        publish(args.catalog, args.main, args.relay)
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1,'Das Sicherungspaar konnte nicht registriert werden. Pfade, Rollout-Kennung und Manifest prüfen.\n')
    print('Sicherungspaar registriert.')


if __name__=='__main__':
    main()
