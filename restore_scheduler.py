"""Restore cold snapshots offline on a persisted local-time schedule."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfoNotFoundError

from admin_store import atomic
from restore_drill import drill, save_report
from restore_pair import PairError, locked, selected
from restore_schedule import inspect_schedule, load_record


def run_due(catalog, result, origins=None, *, now=None):
    now = int(time.time()) if now is None else now
    # Holding a local process lock until the finalized report is durable also recovers
    # interrupted runs on restart. The live applications are never locked.
    fd = os.open(result/'scheduler.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        status = inspect_schedule(result, now)
        if not status or status['next_at'] is None or now < status['next_at']:
            return
        record = load_record(result/'policy.json')
        if not record.get('settings',{}).get('enabled'):
            return
        enabled = record['enabled_at']
        error, backup_at = None, None
        try:
            with locked(catalog):
                root, backup_at = selected(catalog,record['settings']['max_age_days'],now)
                report = drill(root/'main', root/'relay', origins)
                save_report(result/'last.json', report, [catalog])
                error = report['error']
        except FileNotFoundError:
            error = 'backup_missing'
        except PairError as exc:
            backup_at = exc.captured_at
            error = 'backup_stale' if str(exc)=='backup_stale' else 'backup_pair'
        except (OSError, ValueError, KeyError, TypeError):
            error = 'backup_pair'
        incident = status.get('incident') or now
        atomic(result/'scheduled.json', dict(enabled_at=enabled, generation=record.get('generation'), checked_at=now, backup_at=backup_at,
                                             error=error, incident=incident if error else None))
        return error or 'passed'


def notify_failure(store, cfg, creds, *, now=None, post=None):
    """Only the original worker uses the active ntfy connection; the checker has no network."""
    from delivery_monitor import profile
    import requests
    now = int(time.time()) if now is None else now
    directory = store.control/'restore-check'
    try:
        view = inspect_schedule(directory, now)
        policy = load_record(directory/'policy.json').get('settings', {})
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False
    if not view or view['state'] not in ('failed','not_running') or not policy.get('notify_failures'):
        return False
    active = profile(store.list_connections(), 'ntfy')
    url = cfg.get('ntfy_url')
    auth = creds.get('ntfy_authorization')
    if not url or not auth or not active.get('id') or active.get('settings',{}).get('url') != url:
        return False
    token = hashlib.sha256(json.dumps([active['id'],url,view['incident']]).encode()).hexdigest()
    target = directory/'notice.json'
    # The worker already has exclusive sync.lock. Record the attempt before the external
    # request so a crash or ambiguous timeout cannot emit the same incident twice.
    last = load_record(target)
    if last.get('token') == token and (not last.get('retry_at') or now < last['retry_at']):
        return False
    atomic(target, {'token':token})
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = (post or session.post)(url, headers={'Authorization':auth,'Title':'Books & Audio',
                'Tags':'warning','Priority':'3'}, timeout=(2,4), allow_redirects=False,
                data='Die geplante Wiederherstellungsprobe erfordert Prüfung. Details stehen in der Übersicht von Books & Audio.'.encode())
    except requests.ConnectTimeout:
        atomic(target, {'token':token, 'retry_at':now+3600})
        return False
    except requests.RequestException:
        return False
    if not 200 <= response.status_code < 300:
        atomic(target, {'token':token, 'retry_at':now+3600})
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--catalog', type=Path, default=Path('/backups'))
    parser.add_argument('--result', type=Path, default=Path('/result'))
    args = parser.parse_args()
    aliases = [os.environ.get(v,'') for v in ('RESTORE_PRIMARY_ORIGIN','RESTORE_RELAY_ORIGIN')]
    if bool(aliases[0]) != bool(aliases[1]):
        parser.exit(2,'Für die Instanzprüfung beide Adressen setzen.\n')
    while True:
        try:
            outcome = run_due(args.catalog, args.result, aliases if all(aliases) else None)
            if outcome:
                print('Wiederherstellungsprobe:',outcome,flush=True)
        except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
            if not args.loop:
                parser.exit(1, 'Zeitplan oder Ergebnisverzeichnis unlesbar.\n')
            print('Zeitplan oder Ergebnisverzeichnis unlesbar.',flush=True)
        if not args.loop:
            return int(outcome not in (None,'passed'))
        time.sleep(60)


if __name__ == '__main__':
    raise SystemExit(main())
