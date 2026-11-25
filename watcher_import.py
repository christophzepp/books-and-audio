"""Project existing das-access-audit diskstats onto the NVMe activity timeline."""
import argparse
from datetime import datetime
import fcntl
import json
import math
from pathlib import Path
import sys
import time

from admin_store import atomic


def timestamp(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Zeitstempel ohne Zeitzone')
        value = parsed.timestamp()
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError('Ungültiger Zeitstempel')
    return int(value)


def minutes(stream, now):
    """Keys identify disks internally. Only combined read/write counts leave the filter."""
    previous = {}
    samples = {}
    for line in stream:
        if 'diskstats' not in line:
            continue
        try:
            row = json.loads(line)
            if row.get('kind') != 'diskstats':
                continue
            at = timestamp(row.get('utc') or row['received_utc'])
            if not now-7*86400 <= at <= now or not isinstance(row['disks'], list):
                continue
        except (TypeError, KeyError, ValueError, AttributeError):
            continue
        for disk in row['disks']:
            if not isinstance(disk, dict) or disk.get('role') not in ('data','parity'):
                continue
            device = disk.get('kernel_dev')
            if type(device) not in (str,int):
                continue
            try:
                v = disk['values']
                if isinstance(v, str):
                    v = v.split()
                reads, writes = int(v[0]), int(v[4])
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            old = previous.get(device)
            if min(reads, writes) < 0 or old and at <= old[0]:
                continue
            previous[device] = at, reads, writes
            if not old or at-old[0] > 120 or reads < old[1] or writes < old[2]:
                continue
            n = samples.setdefault(at//60*60, [0, 0])
            n[0] += reads-old[1]
            n[1] += writes-old[2]
    return samples


def update(stream, directory, now):
    new = minutes(stream, now)
    if not new:
        return 0
    directory = Path(directory)
    path = directory/'disk-observations.json'
    with (directory/'disk-observations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = {}
        if path.exists():
            existing = json.loads(path.read_text())
            if existing['schema'] != 1:
                raise ValueError('Unbekanntes Format der Wächterdaten')
            saved = {at: [reads,writes] for at,reads,writes in existing['minutes'] if now-7*86400 <= at <= now}
        # One observation per minute, including repeat imports of the same capture.
        saved.update(new)
        atomic(path, {'schema':1,'minutes':[[at,*saved[at]] for at in sorted(saved)]})
    return len(new)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', type=Path, default=Path('/state/control'))
    args = parser.parse_args()
    print(f'{update(sys.stdin, args.control, int(time.time()))} Messminuten übernommen.')
