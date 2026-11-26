"""Worker command line and heartbeat check. Health probes need only the standard library."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import time


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default='/state')
    parser.add_argument('--config', default='/config/config.json')
    parser.add_argument('--credentials', default='/config/credentials.enc')
    parser.add_argument('--key', default='/config/secret.key')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--settle', type=int, default=30)
    parser.add_argument('--health', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(args.state)
    if args.health:
        status = json.loads((root / 'last-run.json').read_text())
        now = time.time()
        age = now - dt.datetime.fromisoformat(status['at']).timestamp()
        limit = admin_health_age(root, now) if os.environ.get('BOOK_PROGRESS_ADMIN_CONTROL') == '1' else 240
        raise SystemExit(0 if 0 <= age < limit and not status.get('fatal') else 1)

    from sync import run_worker
    root.mkdir(parents=True, exist_ok=True)
    return run_worker(args, root)
