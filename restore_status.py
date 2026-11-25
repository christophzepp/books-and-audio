"""Read the small report published by an offline backup drill for the admin dashboard."""
import json
import os
import stat


CHECKS = ('main_backup', 'accounts', 'approvals_progress', 'relay_backup', 'relay_cursor', 'account_match')
COUNTS = ('profiles', 'active_profiles', 'state_records', 'mapping_approvals', 'podcast_approvals',
          'relay_episodes', 'relay_events', 'confirmed', 'pending_actions')


def read_report(control):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        directory = os.open(control / 'restore-check', flags | os.O_DIRECTORY)
        try:
            fd = os.open('last.json', flags | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('report file')
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('report size')
        report = json.loads(raw)
        at, ok, checks, counts = (report[k] for k in ('checked_at', 'ok', 'checks', 'counts'))
        if (type(report.get('schema')) is not int or report['schema'] != 1
                or type(at) is not int or not 0 < at < 253402300800 or type(ok) is not bool
                or set(checks) != set(CHECKS) or any(type(checks[k]) is not bool for k in CHECKS)
                or ok != all(checks.values()) or set(counts) != set(COUNTS)
                or any(type(counts[k]) is not int or not 0 <= counts[k] < 2**53 for k in COUNTS)):
            raise ValueError('report fields')
        error = report['error']
        if ok and error is not None or not ok and (error not in CHECKS or checks[error]):
            raise ValueError('report outcome')
        return {'state': 'passed' if ok else 'failed', 'checked_at': at, 'error': error,
                'counts': {k: counts[k] for k in COUNTS}}
    except FileNotFoundError:
        return {'state': 'missing'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {'state': 'unreadable'}
