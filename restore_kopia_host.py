"""Prepare just the Books & Audio recovery inputs from one completed Kopia snapshot."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

import restore_pair


NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z')
OBJECT = re.compile(r'[a-zA-Z0-9]{16,128}\Z')
LIMIT = 8 * 1024**3


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum(path):
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def select_snapshot(rows, source, now):
    require(isinstance(rows, list), 'invalid_snapshot_list')
    usable = []
    for row in rows:
        meta = row['source']
        if source != f"{meta['userName']}@{meta['host']}:{meta['path']}":
            continue
        stats = row['stats']
        require(isinstance(stats, dict), 'invalid_snapshot_stats')
        errors = (stats.get('errorCount', 0), stats.get('ignoredErrorCount', 0))
        if row.get('incomplete') or row.get('incompleteReason') or any(
                type(count) is not int or count != 0 for count in errors):
            continue
        end = datetime.fromisoformat(row['endTime'].replace('Z', '+00:00'))
        require(end.tzinfo is not None, 'invalid_snapshot_time')
        stamp = int(end.timestamp())
        require(0 < stamp <= now + 60, 'invalid_snapshot_time')
        root = row['rootEntry']
        require(root.get('type') == 'd' and OBJECT.fullmatch(row['id']) and OBJECT.fullmatch(root['obj']),
                'invalid_snapshot_root')
        usable.append((stamp, row['id'], root['obj']))
    require(usable, 'no_complete_snapshot_for_source')
    return max(usable)


class Kopia:
    def __init__(self, container):
        require(NAME.fullmatch(container), 'invalid_container')
        self.command = ['docker', 'exec', container, 'kopia', '--no-progress', '--disable-file-logging']

    def json(self, *args):
        data = subprocess.check_output(self.command + list(args), stderr=subprocess.DEVNULL, timeout=120)
        return json.loads(data)

    def copy(self, obj, target, size):
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            subprocess.run(self.command + ['show', obj], stdout=stream, stderr=subprocess.DEVNULL,
                           timeout=600, check=True)
        require(target.stat().st_size == size, 'snapshot_file_size_changed')


def package(kopia, root_object, main_stack, relay_stack, source_id, captured_at, work):
    require(NAME.fullmatch(main_stack) and NAME.fullmatch(relay_stack) and main_stack != relay_stack,
            'invalid_stack_name')
    sizes = 0

    def list_dir(path):
        rows = kopia.json('show', root_object + '/' + path)['entries']
        result = {r['name']: r for r in rows}
        require(len(result) == len(rows), 'duplicate_directory_entry')
        return result

    def take(remote, names, dest, optional=(), entries=None):
        nonlocal sizes
        entries = list_dir(remote) if entries is None else entries
        for name in names:
            row = entries.get(name)
            if row is None and name in optional:
                continue
            require(row and row['type'] == 'f' and NAME.fullmatch(name), 'missing_or_invalid_file')
            size = row.get('size', 0)
            require(type(size) is int and 0 <= size <= LIMIT - sizes and OBJECT.fullmatch(row['obj']),
                    'invalid_snapshot_file')
            sizes += size
            kopia.copy(row['obj'], dest / name, size)

    def db(remote, base, dest, entries):
        side = [base + ext for ext in ('-wal', '-journal')]
        take(remote, [base, *side], dest, side, entries)

    data, main, relay = (work / name for name in ('data', 'main', 'relay'))
    main.mkdir(mode=0o700)
    take(main_stack + '/config', ['config.json', 'credentials.enc', 'secret.key'], data / 'config')
    state = main_stack + '/state'
    state_dir = data / 'state'
    entries = list_dir(state)
    db(state, 'state.sqlite', state_dir, entries)
    take(state + '/web-auth', ['credentials.json'], state_dir / 'web-auth')
    control = state + '/control'
    cent = list_dir(control)
    take(control, ['connections.enc', 'folders.json'], state_dir / 'control', entries=cent)
    db(control, 'admin.sqlite', state_dir / 'control', cent)
    if 'tracking-alias' in cent:
        require(cent['tracking-alias']['type'] == 'd', 'invalid_alias_directory')
        aliases = list_dir(control + '/tracking-alias')
        names = [n for n in aliases if re.fullmatch(r'[0-9a-f]{64}\.json', n)]
        take(control + '/tracking-alias', names, state_dir / 'control/tracking-alias', entries=aliases)
    if 'bookorbit-scopes' in entries:
        require(entries['bookorbit-scopes']['type'] == 'd', 'invalid_scope_directory')
        scopes = list_dir(state + '/bookorbit-scopes')
        for name in scopes:
            if re.fullmatch(r'[0-9a-f]{64}\.sqlite', name):
                db(state + '/bookorbit-scopes', name, state_dir / 'bookorbit-scopes', scopes)

    tracking = relay_stack + '/tracking'
    relay_entries = list_dir(tracking)
    take(tracking, ['config.json'], relay, entries=relay_entries)
    native = json.loads((relay / 'config.json').read_bytes()).get('native_source')
    if native is not None:
        key = Path(native['api_key_file']).name
        require(key not in ('config.json', 'image.json', 'state') and NAME.fullmatch(key), 'invalid_key_name')
        take(tracking, [key], relay, entries=relay_entries)
    db(tracking + '/state', 'relay.sqlite', relay / 'state', list_dir(tracking + '/state'))

    with tarfile.open(main / 'stack-before.tar.gz', 'w:gz', compresslevel=1) as tar:
        for path in sorted(data.rglob('*')):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(data).as_posix(), recursive=False)
    pair = {'snapshot_id': source_id, 'captured_at': captured_at}
    protected = {p.relative_to(data).as_posix(): checksum(p) for p in data.rglob('*') if p.is_file()}
    (main / 'manifest.json').write_text(json.dumps(dict(pair, schema=1, protected=protected,
                                                        archive_sha256=checksum(main / 'stack-before.tar.gz'))))
    (relay / 'image.json').write_text(json.dumps(pair))
    return main, relay


def refresh(kopia, source, main_stack, relay_stack, catalog):
    now = int(time.time())
    when, ident, obj = select_snapshot(kopia.json('snapshot', 'list', source, '--json'), source, now)
    catalog.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not catalog.is_symlink() and catalog.stat().st_mode & 0o077 == 0, 'catalog_must_be_private')
    current = restore_pair.current_capture(catalog)
    if current and (current[0] > when or current == (when, ident)):
        return 'already_current'
    with tempfile.TemporaryDirectory(prefix='.capture-', dir=catalog.parent) as tmp:
        main, relay = package(kopia, obj, main_stack, relay_stack, ident, when, Path(tmp))
        restore_pair.publish(catalog, main, relay)
    return 'published'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Kopia source in user@host:/path form')
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--main-stack', default='book-progress-sync')
    parser.add_argument('--relay-stack', default='books-audio-tracking')
    parser.add_argument('--kopia-container', default='kopia')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:/[^\x00-\x1f]+', args.source),
                'invalid_snapshot_source')
        print(refresh(Kopia(args.kopia_container), args.source, args.main_stack, args.relay_stack, args.catalog))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, RuntimeError) as exc:
        parser.exit(1, 'Kopia recovery input unavailable (' + type(exc).__name__ + ').\n')


if __name__ == '__main__':
    main()
