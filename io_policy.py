"""Bounded media-I/O policy; progress polling and deployment freshness stay separate."""
import datetime as dt
import math


DEFAULTS = {
    'ebook_cache_max_bytes': 64 * 1024 * 1024,
    'ebook_verify_interval_seconds': 0,
    'ebook_discovery_interval_seconds': 300,
    'ebook_candidate_stable_seconds': 60,
    'folder_validation_interval_seconds': 0,
    'integrity_window_utc': None,
}


def settings(config):
    policy = config.get('io_policy', {})
    if not isinstance(policy, dict) or set(policy) - set(DEFAULTS):
        raise ValueError('Invalid media I/O policy')
    result = dict(DEFAULTS, **policy)
    limits = {
        'ebook_cache_max_bytes': (1024 * 1024, 512 * 1024 * 1024),
        'ebook_discovery_interval_seconds': (60, 86400),
        'ebook_candidate_stable_seconds': (60, 86400),
    }
    for name, (minimum, maximum) in limits.items():
        value = result[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('Invalid media I/O policy: ' + name)
    folder_interval = result['folder_validation_interval_seconds']
    if type(folder_interval) is not int or folder_interval != 0 and not 60 <= folder_interval <= 86400:
        raise ValueError('Invalid media I/O policy: folder_validation_interval_seconds')
    interval = result['ebook_verify_interval_seconds']
    if type(interval) is not int or (interval != 0 and not 60 <= interval <= 365 * 86400):
        raise ValueError('Invalid media I/O policy: ebook_verify_interval_seconds')
    window = result['integrity_window_utc']
    if window is not None:
        if (not isinstance(window, dict) or set(window) != {'start_hour', 'end_hour'}
                or any(type(window[k]) is not int or not 0 <= window[k] <= 23 for k in window)
                or window['start_hour'] == window['end_hour']):
            raise ValueError('Invalid media I/O policy: integrity_window_utc')
        result['integrity_window_utc'] = dict(window)
    return result


def integrity_due(config, now, last):
    """Optional background audit only; never defers validation needed for a write."""
    policy = settings(config)
    interval = policy['ebook_verify_interval_seconds']
    if not interval:
        return False
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid integrity clock')
    window = policy['integrity_window_utc']
    if window is not None:
        hour = dt.datetime.fromtimestamp(now, dt.timezone.utc).hour
        start, end = window['start_hour'], window['end_hour']
        allowed = start <= hour < end if start < end else hour >= start or hour < end
        if not allowed:
            return False
    if isinstance(last, bool) or not isinstance(last, (int, float)) or not math.isfinite(last):
        return True
    return now < last or now - last >= interval
