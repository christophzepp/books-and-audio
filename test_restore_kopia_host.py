from contextlib import closing, redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import restore_drill
import restore_kopia_host as host
import restore_pair
import test_restore_drill


SOURCE = 'root@backup-host:/source/stacks'
MAIN = 'book-progress-sync'
RELAY = 'books-audio-tracking'
ROOT_OBJECT = 'k' + '3' * 32


def snapshot(stamp, ident='a' * 32, **updates):
    # Kopia omits zero-valued error counters in the output of snapshot list --json.
    manifest = dict(id=ident, source=dict(userName='root', host='backup-host', path='/source/stacks'),
                    endTime=datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z'),
                    stats=dict(fileCount=25), rootEntry=dict(type='d', obj=ROOT_OBJECT))
    return dict(manifest, **updates)


class SnapshotObjects:
    """Immutable repository objects with Kopia's directory entry and raw file formats."""

    def __init__(self, dirs, snapshots):
        self.snapshots = snapshots
        self.dirs, self.blobs, self.copied = {}, {}, []
        self.read_dirs = []
        for remote, local in dirs.items():
            for folder in [local, *(p for p in local.rglob('*') if p.is_dir())]:
                name = str((Path(remote) / folder.relative_to(local)).as_posix())
                self.dirs[name] = []
                for item in folder.iterdir():
                    entry = {'name': item.name, 'type': 'd'}
                    if item.is_file():
                        blob = item.read_bytes()
                        obj = hashlib.sha256(blob).hexdigest()
                        entry.update(type='f', obj=obj, size=len(blob))
                        self.blobs[obj] = blob
                    self.dirs[name].append(entry)

    def json(self, *args):
        if args == ('snapshot', 'list', SOURCE, '--json'):
            return self.snapshots
        verb, path = args
        if verb != 'show' or not path.startswith(ROOT_OBJECT + '/'):
            raise AssertionError('unexpected repository access')
        folder = path[len(ROOT_OBJECT) + 1:]
        self.read_dirs.append(folder)
        return {'entries': self.dirs[folder]}

    def copy(self, obj, target, size):
        self.copied.append(obj)
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = self.blobs[obj]
        if len(blob) != size:
            raise ValueError('snapshot_file_size_changed')
        target.write_bytes(blob)


class KopiaInputsTests(unittest.TestCase):
    def test_selects_latest_complete_snapshot_of_exact_production_source(self):
        now = 1800000000
        good = snapshot(now-20)
        rows = [snapshot(now-200, stats=dict(errorCount=0, ignoredErrorCount=0)), good,
                snapshot(now, incomplete='checkpoint'), snapshot(now, incompleteReason='checkpoint'),
                snapshot(now, stats=dict(errorCount=1)),
                snapshot(now, stats=dict(ignoredErrorCount=1)),
                snapshot(now, stats=dict(errorCount=None)),
                snapshot(now, stats=dict(ignoredErrorCount=False))]
        for changes in (dict(userName='kopia'), dict(host='other'), dict(path='/source/stacks-old')):
            rows.append(snapshot(now, source=dict(good['source'], **changes)))
        expected = (now-20, good['id'], ROOT_OBJECT)
        self.assertEqual(host.select_snapshot(rows, SOURCE, now), expected)
        with self.assertRaisesRegex(ValueError, 'no_complete'):
            host.select_snapshot(rows[2:], SOURCE, now)

    def test_kopia_cli_reads_json_and_copies_binary_verbatim_with_private_permissions(self):
        client = host.Kopia('kopia')
        with patch('subprocess.check_output', return_value=b'{"entries":[]}') as run:
            self.assertEqual(client.json('show', ROOT_OBJECT), dict(entries=[]))
            self.assertEqual(run.call_args.args[0], client.command + ['show', ROOT_OBJECT])
        blob = bytes(range(256))
        def write(command, **kwargs):
            self.assertEqual(command, client.command + ['show', ROOT_OBJECT])
            kwargs['stdout'].write(blob)
        with tempfile.TemporaryDirectory() as tmp, patch('subprocess.run', side_effect=write):
            path = Path(tmp)/'keys'/'sensitive'
            client.copy(ROOT_OBJECT, path, len(blob))
            self.assertEqual(path.read_bytes(), blob)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, 'size_changed'):
                client.copy(ROOT_OBJECT, path.with_name('short'), len(blob)+1)

    def test_end_to_end_preserves_progress_wal_and_relay_cursor_and_avoids_unrelated_files(self):
        fixture = test_restore_drill.RestoreDrillTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        now = int(time.time())
        catalog = fixture.stack.parent/'catalog'
        (fixture.stack/'state/web-auth'/'setup-token').write_text('private bootstrap material')
        (fixture.stack/'state/control'/'session-secret').write_text('unrelated')
        (fixture.relay/'ignored.env').write_text('unrelated')
        # This approval exists only in the WAL when the repository captures the files.
        with closing(sqlite3.connect(fixture.stack/'state/state.sqlite')) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            with db:
                db.execute('INSERT INTO auto_mappings VALUES (?,?,?)', ('wal-approval', 2, '{"id":"wal"}'))
            kopia = SnapshotObjects({MAIN:fixture.stack, RELAY+'/tracking':fixture.relay}, [snapshot(now-50)])
        self.assertEqual(host.refresh(kopia, SOURCE, MAIN, RELAY, catalog), 'published')
        with restore_pair.locked(catalog):
            root, stamp = restore_pair.selected(catalog, 1, now)
            self.assertEqual(stamp, now-50)
            with tarfile.open(root/'main/stack-before.tar.gz') as archive:
                names = archive.getnames()
                self.assertIn('state/state.sqlite-wal', names)
                self.assertFalse(any('setup-token' in n or 'session-secret' in n or '-shm' in n for n in names))
            self.assertFalse((root/'relay'/'ignored.env').exists())
            for private in (b'private bootstrap material', b'unrelated'):
                self.assertNotIn(hashlib.sha256(private).hexdigest(), kopia.copied)
            report = restore_drill.drill(root/'main', root/'relay')
            self.assertTrue(report['ok'], report['error'])
            self.assertEqual(report['counts']['mapping_approvals'], 2)
            self.assertEqual(report['cursor'], fixture.expected_ack)
            for private in ('test-native-key', 'abs-test-secret', 'listener'):
                self.assertNotIn(private, json.dumps(report))
            for item in root.rglob('*'):
                if item.is_file():
                    self.assertEqual(item.stat().st_mode & 0o777, 0o600)
        metadata = (catalog/'current.json').read_bytes()
        kopia.copied.clear()
        kopia.read_dirs.clear()
        for rows in ([snapshot(now-50)], [snapshot(now-60, ident='b'*32)]):
            kopia.snapshots = rows
            self.assertEqual(host.refresh(kopia, SOURCE, MAIN, RELAY, catalog), 'already_current')
        self.assertFalse(kopia.read_dirs or kopia.copied)
        # Failure part way through the relay must preserve the complete current pair.
        kopia.snapshots = [snapshot(now-15, ident='c'*32)]
        kopia.dirs[RELAY+'/tracking/state'] = []
        with self.assertRaisesRegex(ValueError, 'missing_or_invalid_file'):
            host.refresh(kopia, SOURCE, MAIN, RELAY, catalog)
        self.assertEqual((catalog/'current.json').read_bytes(), metadata)
        self.assertEqual(restore_pair.current_capture(catalog), (now-50, 'a'*32))
        self.assertFalse(list(catalog.parent.glob('.capture-*')))

    def test_cli_error_redacts_private_command_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['importer', '--source', SOURCE, '--catalog', tmp]
            error = subprocess.CalledProcessError(1, ['private-key'], stderr='credentials')
            with patch.object(sys, 'argv', args), patch('os.umask'), patch('subprocess.check_output', side_effect=error), \
                    redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_code:
                host.main()
            self.assertEqual(exit_code.exception.code, 1)
            self.assertEqual(output.getvalue(), 'Kopia recovery input unavailable (CalledProcessError).\n')
