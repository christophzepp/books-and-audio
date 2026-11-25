"""Worker-owned scheduling preferences on the state volume."""
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from admin_store import atomic
from io_policy import DEFAULTS as IO_DEFAULTS, settings as io_settings


class ScheduleError(ValueError):
    pass


# Public names map to existing config fields. Only changed fields are persisted,
# so deployment-specific polling intervals keep taking effect after upgrades.
FIELDS = {
    'interval_seconds': (None, 'interval_seconds', 60, 1, 86400),
    'settle_seconds': (None, 'settle_seconds', 30, 0, 86400),
    'book_discovery_seconds': ('auto_discovery', 'interval_seconds', 300, 60, 86400),
    'book_stable_seconds': ('auto_discovery', 'stable_seconds', 300, 300, 86400),
    'readalong_stable_seconds': ('auto_native_readalong', 'stable_seconds', 300, 30, 86400),
    'podcast_start_hour': ('podcast_maintenance', 'start_hour', 22, 0, 23),
    'podcast_end_hour': ('podcast_maintenance', 'end_hour', 0, 0, 23),
    'podcast_timezone': ('podcast_maintenance', 'timezone', 'Europe/Berlin', None, None),
    'series_enabled': ('series_organizer', 'enabled', False, None, None),
    'series_interval_seconds': ('series_organizer', 'interval_seconds', 3600, 300, 604800),
}
for key in ('ebook_discovery_interval_seconds', 'ebook_candidate_stable_seconds',
            'ebook_retry_initial_seconds', 'ebook_retry_max_seconds', 'ebook_verify_interval_seconds',
            'folder_validation_interval_seconds', 'integrity_window_utc'):
    FIELDS[key] = ('io_policy', key, IO_DEFAULTS[key], None, None)


def _zone(name):
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ScheduleError('Bitte eine gültige IANA-Zeitzone für die Podcast-Wartung angeben.')
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ScheduleError('Die Zeitzone für die Podcast-Wartung ist unbekannt.') from None


def podcast_window(config, now):
    """Return active and local start date; an overnight window has the same id after midnight."""
    window = config.get('podcast_maintenance', {})
    start, end = window.get('start_hour', 22), window.get('end_hour', 0)
    if any(type(h) is not int or h not in range(24) for h in (start, end)) or start == end:
        raise ScheduleError('Beginn und Ende der Podcast-Wartung müssen unterschiedliche volle Stunden sein.')
    local = datetime.fromtimestamp(now, _zone(window.get('timezone', 'Europe/Berlin')))
    if start < end:
        return start <= local.hour < end, local.date().isoformat()
    date = local.date() - timedelta(days=1) if local.hour < end else local.date()
    return local.hour >= start or local.hour < end, date.isoformat()


def config_values(config):
    return {name: (config if group is None else config.get(group, {})).get(key, default)
            for name, (group, key, default, _, _) in FIELDS.items()}


def with_overrides(config, overrides):
    result = dict(config)
    for name, value in overrides.items():
        group, key, _, _, _ = FIELDS[name]
        if group is None:
            result[key] = value
        else:
            result[group] = dict(result.get(group, {}), **{key: value})
    return result


def validate(base, overrides):
    if not isinstance(overrides, dict) or overrides.keys() - FIELDS.keys():
        raise ScheduleError('Unbekannte Zeitplaneinstellung.')
    for name, value in overrides.items():
        _, _, _, low, high = FIELDS[name]
        if low is not None and (type(value) is not int or not low <= value <= high):
            raise ScheduleError(f'Der Wert für {name} muss zwischen {low} und {high} liegen.')
        if name == 'series_enabled' and type(value) is not bool:
            raise ScheduleError('Für Hörspielserien ist Ein oder Aus erforderlich.')
    result = with_overrides(base, overrides)
    podcast_window(result, 0)
    try:
        io_settings(result)
    except ValueError as exc:
        raise ScheduleError('Die E-Book- und Ordnerintervalle prüfen. Die maximale Wiederholungsfrist muss mindestens der ersten Frist entsprechen.') from exc


def prepare_series_directories(root):
    control, status = Path(root) / 'series-control', Path(root) / 'series-status'
    for directory in (control, status):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError('Unsafe series state directory')
    return control, status


class MaintenanceSchedule:
    """The sync process holds sync.lock, so updates and revision checks have one writer."""
    def __init__(self, root, config):
        self.root = Path(root)
        self.base = config
        self.path = self.root / 'maintenance-schedule.json'
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            saved = {'revision': 0, 'overrides': {}}
        validate(config, saved['overrides'])
        self.revision, self.overrides = saved['revision'], saved['overrides']
        self.control, self.status_dir = prepare_series_directories(self.root)
        stamp = self._record(self.revision, self.overrides)
        if saved != stamp:
            atomic(self.path, stamp)
        self.publish_organizer()

    def _record(self, revision, overrides):
        return {'revision': revision, 'overrides': overrides, 'interval_seconds':
                overrides.get('interval_seconds', self.base.get('interval_seconds', 60))}

    def effective(self, config):
        result = dict(config)
        for field in ('settle_seconds', 'podcast_maintenance', 'series_organizer'):
            if field in self.base:
                result[field] = self.base[field]
        return with_overrides(result, self.overrides)

    def public(self):
        return {'revision': self.revision, 'values': config_values(self.effective(self.base))}

    def update(self, payload):
        if (set(payload) != {'expected_revision', 'changes'} or type(payload['expected_revision']) is not int
                or not isinstance(payload['changes'], dict) or not payload['changes']):
            raise ScheduleError('Bitte mindestens eine geänderte Zeitplaneinstellung senden.')
        if payload['expected_revision'] != self.revision:
            raise ScheduleError('Der Zeitplan wurde inzwischen geändert. Bitte neu laden.')
        updated = dict(self.overrides, **payload['changes'])
        validate(self.base, updated)
        if updated != self.overrides:
            atomic(self.path, self._record(self.revision + 1, updated))
            self.overrides = updated
            self.revision += 1
        self.publish_organizer()
        return self.public()

    def publish_organizer(self):
        values = self.public()['values']
        data = {'enabled': values['series_enabled'], 'interval_seconds': values['series_interval_seconds']}
        path = self.control / 'settings.json'
        try:
            if json.loads(path.read_text()) == data:
                return
        except (FileNotFoundError, ValueError):
            pass
        atomic(path, data)

    def organizer_status(self):
        try:
            raw = json.loads((self.root / 'series-status' / 'status.json').read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get('status') not in ('idle', 'completed', 'error', 'disabled', 'waiting'):
            return {}
        public = {'status': raw['status']}
        if type(raw.get('checked_at')) in (float, int) and math.isfinite(raw['checked_at']):
            public['checked_at'] = raw['checked_at']
        for key in ('moved', 'empty_dirs_removed', 'skipped', 'conflicts'):
            if type(raw.get(key)) is int and raw[key] >= 0:
                public[key] = raw[key]
        return public


def admin_health_age(root, now):
    """Keep the worker heartbeat bounded even when the configured poll is infrequent."""
    try:
        interval = json.loads((root / 'maintenance-schedule.json').read_text())['interval_seconds']
        heartbeat = (root / 'control' / 'status.json').stat().st_mtime
    except FileNotFoundError:
        return 240
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        return 0
    return max(240, interval + 180) if 0 <= now - heartbeat < 240 else 0
