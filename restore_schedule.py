"""Small shared NVMe control records for the offline restore checker and the GUI."""
from datetime import datetime, time as local_time, timedelta
import json
import os
import stat
import time
import uuid
from zoneinfo import ZoneInfo

from restore_status import CHECKS


DEFAULTS = {'enabled': False, 'interval_days': 7, 'hour': 7, 'timezone': 'Europe/Berlin',
            'max_age_days': 30, 'notify_failures': True}
ERRORS = (*CHECKS, 'backup_missing', 'backup_pair', 'backup_stale', 'not_running')
GRACE = 2 * 3600


def load_record(path):
    if path.parent.is_symlink():
        raise ValueError('invalid control path')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd,'rb') as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid control file')
        raw = f.read(8193)
    if len(raw) > 8192:
        raise ValueError('oversized control record')
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError('invalid record')
    return record


def timestamp(value):
    return type(value) is int and 0 < value < 253402300800


def validate(policy):
    for field in ('enabled','notify_failures'):
        if type(policy.get(field)) is not bool:
            raise ValueError('invalid restore setting')
    for field, low, high in [('interval_days',1,90),('hour',0,23),('max_age_days',1,365)]:
        if type(policy.get(field)) is not int or not low <= policy[field] <= high:
            raise ValueError('invalid restore interval')
    if not isinstance(policy.get('timezone'),str) or not 1 <= len(policy['timezone']) <= 100:
        raise ValueError('invalid restore timezone')
    ZoneInfo(policy['timezone'])


def publish_policy(control, values, now=None):
    from admin_store import atomic
    directory = control/'restore-check'
    directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink():
        raise ValueError('invalid control directory')
    policy = {k: values['restore_'+k] for k in DEFAULTS}
    validate(policy)
    path = directory/'policy.json'
    try:
        old = load_record(path)
    except (ValueError, OSError):
        # The worker owns the authoritative maintenance schedule. Rebuild its derivative.
        old = {}
    if not isinstance(old.get('settings'), dict):
        old = {}
    if policy == old.get('settings'):
        return
    # Re-enabling starts a new generation and requires a fresh result.
    start = old.get('enabled_at') if old.get('settings',{}).get('enabled') else None
    if policy['enabled'] and not timestamp(start):
        start = int(time.time()) if now is None else now
    generation = old.get('generation') if policy['enabled'] and old.get('settings',{}).get('enabled') else None
    if policy['enabled'] and not generation:
        generation = uuid.uuid4().hex
    atomic(path, {'settings': policy, 'enabled_at': start if policy['enabled'] else None, 'generation': generation})


def scheduled_after(at, policy):
    tz = ZoneInfo(policy['timezone'])
    local = datetime.fromtimestamp(at, tz)
    day = local.date() + timedelta(days=policy['interval_days'])
    return int(datetime.combine(day, local_time(hour=policy['hour']), tzinfo=tz).timestamp())


def inspect_schedule(directory, now=None):
    now = int(time.time()) if now is None else now
    config = load_record(directory/'policy.json')
    if not config:
        return None
    policy = config['settings']
    validate(policy)
    view = dict(state='disabled',checked_at=None,next_at=None,backup_at=None,error=None,incident=None)
    if not policy['enabled']:
        return view
    enabled = config['enabled_at']
    if not timestamp(enabled):
        raise ValueError('invalid activation')
    last = load_record(directory/'scheduled.json')
    view.update(state='waiting', next_at=enabled)
    if last and last.get('enabled_at') == enabled and last.get('generation') == config.get('generation'):
        at, error, backup, incident = (last.get(f) for f in ('checked_at','error','backup_at','incident'))
        if not timestamp(at) or (error is not None and (error not in ERRORS or not timestamp(incident))) or (backup is not None and not timestamp(backup)):
            raise ValueError('invalid scheduled result')
        view.update(state='failed' if error else 'passed', checked_at=at, next_at=scheduled_after(at,policy),
                    backup_at=backup, error=error, incident=incident if error else None)
    if now >= view['next_at'] + GRACE:
        view.update(state='not_running', error='not_running', incident=view['incident'] or view['next_at'])
    return view
