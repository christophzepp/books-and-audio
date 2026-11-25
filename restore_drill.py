"""Verify a pair of cold Books & Audio backups offline, in a disposable directory."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from types import SimpleNamespace

from cryptography.fernet import Fernet

from admin_web import Auth
import bookorbit_scope
from connection_manager import ConnectionManager
from folder_manager import FolderManager
from gpodder_relay import Relay, encoded
from restore_status import CHECKS, COUNTS


SIDE = r'(?:-wal|-journal)?'
SCOPE_DB = r'state/bookorbit-scopes/[0-9a-f]{64}\.sqlite'
MAIN_FILES = re.compile(
    r'(?:config/(?:current/)?(?:config\.json|credentials\.enc|secret\.key)|web_key/secret\.key|'
    r'(?:state|web)/web-auth/credentials\.json|(?:state/)?control/(?:folders\.json|connections\.enc|admin\.sqlite' +
    SIDE + r')|(?:state/state\.sqlite|' + SCOPE_DB + ')' + SIDE + r')\Z')
MAX_RESTORED = 8 * 1024**3


class DrillError(ValueError):
    pass


def require(condition):
    if not condition:
        raise DrillError()


def load(path):
    return json.loads(path.read_bytes())


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_stream(source, destination):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with destination.open('xb') as target:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(source, target, length=1024 * 1024)


def main_name(name):
    if not MAIN_FILES.fullmatch(name):
        return None
    if name.startswith('config/current/'):
        return 'config/' + Path(name).name
    if name.startswith('control/'):
        return 'state/' + name
    if name.startswith('web/web-auth/'):
        return name.replace('web/', 'state/', 1)
    return name


def restore_main(source, root):
    manifest = None
    if source.is_dir():
        # A deployment backup has a separately recorded digest of the cold archive.
        manifest_path = source / 'manifest.json'
        require(not source.is_symlink() and not manifest_path.is_symlink())
        manifest = load(manifest_path)
        source = source / 'stack-before.tar.gz'
    require(source.is_file() and not source.is_symlink())
    if manifest is not None:
        require(manifest['schema'] == 1 and checksum(source) == manifest['archive_sha256'])
    total = 0
    volume_layout = False
    with tarfile.open(source, 'r|*') as archive:
        for item in archive:
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and '..' not in path.parts)
            name = str(path)
            target = main_name(name)
            if target is None:
                continue
            require(item.isfile() and not item.issparse())
            total += item.size
            require(total <= MAX_RESTORED)
            with archive.extractfile(item) as stream:
                copy_stream(stream, root / target)
            volume_layout |= name.startswith('config/current/')
    if volume_layout:
        require((root/'web_key/secret.key').read_bytes() == (root/'config/secret.key').read_bytes())
    if manifest is not None:
        # An absent account-specific DB would otherwise be indistinguishable from an empty scope.
        for key, expected in manifest['protected'].items():
            if key.startswith('scope:'):
                name = key.split(':')[1]
                require(re.fullmatch(SCOPE_DB, name) and (root/name).is_file())
            elif (name := main_name(key)) is not None:
                require(checksum(root/name) == expected)


def open_db(path):
    db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        require(not db.execute('PRAGMA foreign_key_check').fetchone())
    except BaseException:
        db.close()
        raise
    return db


def check_accounts(root, counts):
    cfg = load(root/'config/config.json')
    key = (root/'config/secret.key').read_bytes()
    cipher = Fernet(key)
    base = json.loads(cipher.decrypt((root/'config/credentials.enc').read_bytes()))
    state_dir = root/'state'
    # Assert inputs exist first: several normal app entry points initialize missing stores.
    require((state_dir/'web-auth/credentials.json').is_file())
    require(Auth(state_dir).initialized)
    manager = ConnectionManager(state_dir, cfg, base, key)
    restored, encrypted = manager._read()
    require(encrypted is not None)
    with closing(open_db(state_dir/'control/admin.sqlite')) as db:
        for payload, private in db.execute('SELECT payload,private_result FROM commands'):
            require(isinstance(json.loads(cipher.decrypt(payload)), dict))
            if private is not None:
                json.loads(cipher.decrypt(private))
        db.execute('SELECT key,value FROM settings LIMIT 1')
        db.execute('SELECT kind,detail FROM audit LIMIT 1')
    active = [p for p in restored['profiles'] if p['active']]
    counts.update(profiles=len(restored['profiles']), active_profiles=len(active))
    return cfg, restored['effective_config'], active


def check_progress(root, cfg, effective, counts):
    state = root/'state'
    folders = FolderManager(state, cfg, access_roots=[])
    _, raw = folders._read()
    require(raw is not None)
    runtime = bookorbit_scope.overlay(SimpleNamespace(base_config=cfg), effective)
    require(bookorbit_scope.database(state, runtime).is_file())
    for path in [state/'state.sqlite', *sorted((state/'bookorbit-scopes').glob('*.sqlite'))]:
        with closing(open_db(path)) as db:
            for key, raw in db.execute('SELECT id,value FROM state'):
                require(isinstance(json.loads(raw), dict))
                counts['state_records'] += 1
                if key.startswith(('_pine_transition:', '_pine_alias:')):
                    counts['podcast_approvals'] += 1
            for (raw,) in db.execute('SELECT value FROM operations'):
                require(isinstance(json.loads(raw), dict))
            for table, column in [('auto_mappings','value'), ('mapping_revision_overrides','value'),
                                  ('native_readalong_enrollments','proof')]:
                for (raw,) in db.execute('SELECT ' + column + ' FROM ' + table):
                    require(isinstance(json.loads(raw), dict))
                    counts['mapping_approvals'] += 1


def copy_regular(source, dest):
    require(source.is_file() and not source.is_symlink() and source.stat().st_size <= MAX_RESTORED)
    with source.open('rb') as stream:
        copy_stream(stream, dest)


def restore_relay(source, root):
    require(source.is_dir() and not source.is_symlink())
    copy_regular(source/'config.json', root/'config.json')
    cfg = load(root/'config.json')
    require((source/'state').is_dir() and not (source/'state').is_symlink())
    for suffix in ('', '-wal', '-journal'):
        name = 'relay.sqlite' + suffix
        path = source/'state'/name
        if not suffix or path.exists() or path.is_symlink():
            copy_regular(path, root/name)
    if cfg.get('native_source') is not None:
        # Configured paths in the backup refer to another machine. Only a single
        # key directly inside the supplied backup is accepted.
        name = PurePosixPath(cfg['native_source']['api_key_file']).name
        require(name not in ('', '.', '..', 'config.json', 'state', 'relay.sqlite'))
        copy_regular(source/name, root/'pine.key')
        with (root/'pine.key').open('rb') as f:
            raw = f.read(4097)
        require(0 < len(raw) <= 4096 and raw.decode().strip() and
                all(ord(c) >= 32 and ord(c) != 127 for c in raw.decode().strip()))
    return cfg


def check_cursor(root, cfg, counts):
    with closing(open_db(root/'relay.sqlite')) as db:
        settings = {key:json.loads(value) for key,value in db.execute('SELECT id,value FROM state')}
        origin = cfg['upstream'].rstrip('/')
        expected = [origin, cfg['username'], cfg['baseline'], cfg.get('exclude_devices', [])]
        require(type(cfg['baseline']) is int and 0 < cfg['baseline'] <= time.time())
        require(settings['scope'] == encoded(expected))
        ack, batch = settings['ack'], settings['pending']
        require(type(ack) is int and ack >= cfg['baseline'])
        (counts['confirmed'],) = db.execute('SELECT count(*) FROM seen').fetchone()
        native = cfg.get('native_source')
        if native is not None:
            # Before the delivery ledger was introduced, only native_scope and
            # the native baseline identified this mode in a cold backup.
            require(settings.get('delivery_mode', 'native-state') == 'native-state')
            require(settings['native_scope'] == encoded([origin, cfg['username'], native.get('user_id'),
                                                       native.get('server_version','0.9.0')]))
            baseline, = db.execute("SELECT snapshot FROM native_tracking_events WHERE kind='baseline' "
                                   "AND event_id='native-projection:initialized:v1'").fetchone()
            (counts['relay_episodes'],) = db.execute('SELECT count(*) FROM native_tracking_episodes').fetchone()
            (counts['relay_events'],) = db.execute('SELECT count(*) FROM native_tracking_events').fetchone()
            require(0 <= json.loads(baseline)['inventory_count'] <= counts['relay_episodes'])
            for ident, snapshot in db.execute('SELECT id,snapshot FROM native_tracking_episodes'):
                require(json.loads(snapshot)['id'] == ident)
            for kind, ident, snapshot, payload in db.execute(
                    'SELECT kind,event_id,snapshot,payload FROM native_tracking_events'):
                require(isinstance(json.loads(snapshot), dict))
                payload = json.loads(payload)
                require(isinstance(payload, dict))
                if kind == 'play':
                    require(payload['_tracking_event_id'] == ident and payload['action'] == 'play')
            missing = db.execute("""SELECT 1 FROM seen s WHERE substr(s.hash,1,7)='native:' AND NOT EXISTS
                    (SELECT 1 FROM native_tracking_events e WHERE e.event_id=substr(s.hash,8) AND e.kind='play')
                    LIMIT 1""").fetchone()
            require(not missing)
        else:
            require(settings.get('delivery_mode','legacy-actions') == 'legacy-actions')
        if batch is not None:
            ids, actions = batch['fingerprints'], batch['actions']
            require(isinstance(ids,list) and isinstance(actions,list) and len(ids) == len(actions) == len(set(ids)))
            require(type(batch['timestamp']) is int and (batch['timestamp'] > ack or
                    (native is None and not actions and batch['timestamp'] == ack)))
            for ident, action in zip(ids, actions):
                require(isinstance(action,dict) and action.get('action') == 'play'
                        and not db.execute('SELECT 1 FROM seen WHERE hash=?',(ident,)).fetchone())
                if native is not None:
                    require(ident.startswith('native:'))
                    payload, = db.execute("SELECT payload FROM native_tracking_events WHERE event_id=? AND kind='play'",
                                          (ident[7:],)).fetchone()
                    recorded = json.loads(payload)
                    require(all(recorded[k] == action[k] for k in ('podcast','timestamp','position','total')))
            counts['pending_actions'] = len(actions)
        return ack


def delivery_history(root, native):
    """Comparable data across schema migrations. Read only the copied databases."""
    queries = [
        "SELECT id,value FROM state WHERE id IN ('scope','native_scope','ack','pending','native_inventory_scan') ORDER BY id",
        "SELECT hash FROM seen ORDER BY hash",
    ]
    if native:
        queries += [
            'SELECT id,snapshot,generation,sequence,observed_at FROM native_tracking_episodes ORDER BY id',
            'SELECT event_id,kind,episode_id,generation,sequence,reason,observed_at,snapshot,payload FROM native_tracking_events ORDER BY event_id',
        ]
    digest = hashlib.sha256()
    with closing(open_db(root/'relay.sqlite')) as db:
        for query in queries:
            digest.update(b'\0')
            for row in db.execute(query):
                digest.update((encoded(row) + '\n').encode())
    return digest.digest()


def restart_relay(root, cfg):
    """Perform a real local relay bootstrap without constructing any remote clients."""
    native = cfg.get('native_source') is not None
    original = delivery_history(root, native)
    def offline(*args, **kwargs):
        raise DrillError()
    Relay(cfg, root, request=offline, source=object(), resolver=object())
    require(delivery_history(root, native) == original)


def drill(main_backup, relay_backup, aliases=None):
    report = dict(schema=1, checked_at=int(time.time()), ok=False, checks=dict.fromkeys(CHECKS,False),
                  counts=dict.fromkeys(COUNTS,0), cursor=None, error=None)
    checks = report['checks']
    step = 'main_backup'
    try:
        with tempfile.TemporaryDirectory(prefix='books-audio-restore-') as temp:
            root = Path(temp)
            restore_main(main_backup, root)
            checks[step] = True
            step = 'accounts'
            cfg, effective, profiles = check_accounts(root, report['counts'])
            checks[step] = True
            step = 'approvals_progress'
            check_progress(root, cfg, effective, report['counts'])
            checks[step] = True
            step = 'relay_backup'
            relay = root/'relay'
            tracking_cfg = restore_relay(relay_backup,relay)
            checks[step] = True
            step = 'relay_cursor'
            report['cursor'] = check_cursor(relay, tracking_cfg, report['counts'])
            restart_relay(relay, tracking_cfg)
            checks[step] = True
            step = 'account_match'
            p, = [p for p in profiles if p['kind']=='pinepods']
            main_origin = p['settings']['url'].rstrip('/')
            relay_origin = tracking_cfg['upstream'].rstrip('/')
            require(main_origin == relay_origin or (aliases is not None and
                    [main_origin, relay_origin] == [v.rstrip('/') for v in aliases]))
            require(p['settings']['username']==tracking_cfg['username'])
            checks[step] = True
        report['ok'] = True
    except Exception:
        # No user IDs, query strings, decrypted secrets or exception traces in publishable results.
        report['error'] = step
    return report


def save_report(path, report, sources):
    dest = path.resolve(strict=False)
    for item in sources:
        source = item.resolve(strict=False)
        require(dest != source and not dest.is_relative_to(source))
    require(not path.is_symlink())
    if path.exists():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 8192)
        previous = load(path)
        require(isinstance(previous,dict) and set(previous)==set(report) and previous['schema']==1)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as tmp:
        scratch = Path(tmp.name)
        try:
            tmp.write((json.dumps(report,separators=(',',':'))+'\n').encode())
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(scratch,path)
        finally:
            scratch.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Konten, Freigaben, Fortschritte und Relay-Abrufstand aus Sicherungskopien prüfen.')
    parser.add_argument('--main',type=Path,required=True,help='Sicherungsordner mit Manifest oder separates kaltes TAR-Archiv.')
    parser.add_argument('--relay',type=Path,required=True,help='Kalter Sicherungsordner des Tracking-Relays.')
    parser.add_argument('--pinepods-origins',nargs=2,metavar=('HAUPTDIENST','RELAY'),help='Bereits geprüfte Adressen derselben PinePods-Instanz.')
    parser.add_argument('--json',action='store_true',help='Maschinenlesbare Ausgabe.')
    parser.add_argument('--report-json',type=Path,help='Letztes Ergebnis atomar in einem separaten Ergebnisverzeichnis ersetzen.')
    args = parser.parse_args()
    report = drill(args.main,args.relay,args.pinepods_origins)
    if args.json:
        print(json.dumps(report,separators=(',',':')))
    else:
        print('Wiederherstellungsprobe ' + ('bestanden.' if report['ok'] else 'fehlgeschlagen. Prüfschritt: '+report['error']))
        print('Profile: {profiles}, Zustandsdatensätze: {state_records}, Podcast-Freigaben: {podcast_approvals}, '
              'Relay-Folgen: {relay_episodes}, Bestätigungen: {confirmed}, offene Aktionen: {pending_actions}.'.format(**report['counts']))
        print('Bestätigter gPodder-Cursor:', report['cursor'])
    if args.report_json:
        try:
            save_report(args.report_json,report,[args.main,args.relay])
        except (OSError,ValueError):
            parser.exit(2,'Ergebnisdatei konnte nicht sicher veröffentlicht werden.\n')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
