"""Durable negative results for verified Listenarr target collisions."""
import hashlib
import json
from pathlib import Path
import time

from admin_store import atomic
from maintenance_settings import podcast_window


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def signature(row, library, target_for, error):
    # A related unimported book can reserve a destination. Include exactly the
    # same series / intersecting paths as reconciliation, never stat the paths.
    from series_listenarr import identity
    from pathlib import PurePosixPath
    source = PurePosixPath(identity(row['basePath']))
    target = PurePosixPath(identity(str(target_for(row))))
    peers = []
    for entry in library:
        if entry['id'] == row['id'] or not entry.get('basePath'):
            continue
        location = PurePosixPath(identity(entry['basePath']))
        projected = None
        try:
            projected = PurePosixPath(identity(str(target_for(entry))))
        except error:
            pass
        if (location.parent.name == target.parent.name or projected in (source, target)
                or any(location == p or location in p.parents or p in location.parents for p in (source, target))):
            peers.append([entry['id'], str(location), str(projected), entry.get('series'),
                          entry.get('seriesAsin'), entry.get('seriesMemberships')])
    fields = ('id', 'basePath', 'title', 'subtitle', 'series', 'seriesNumber', 'asin',
              'seriesAsin', 'seriesMemberships', 'authors', 'fileCount', 'files', 'updatedAt', 'lastModified')
    target_row = {key:row[key] for key in fields if key in row}
    data = json.dumps([target_row, sorted(peers, key=lambda p:p[0])], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


class ListenarrConflicts:
    def __init__(self, state, control):
        self.path = Path(state) / 'listenarr-conflicts.json'
        self.rows = read(self.path)
        self.requests = read(Path(control) / 'listenarr-retries.json')
        config = read(Path(control) / 'maintenance-window.json')
        self.active, self.window = podcast_window(config, time.time())

    def held(self, key, stamp):
        row = self.rows.get(key)
        if not row or row['signature'] != stamp or row.get('request') != self.requests.get(key):
            return False
        if self.active and row.get('window') != self.window:
            row['window'] = self.window
            atomic(self.path, self.rows)
            return False
        return True

    def record(self, key, stamp, title, reason):
        self.rows[key] = dict(signature=stamp, title=str(title)[:200], reason=str(reason),
                              request=self.requests.get(key), window=self.window if self.active else None)
        atomic(self.path, self.rows)

    def cleanup(self, keep):
        stale = self.rows.keys() - keep
        if stale:
            for key in stale:
                del self.rows[key]
            atomic(self.path, self.rows)

    def public(self):
        return [dict(id=key, title=row['title'], reason=row['reason']) for key, row in self.rows.items()]
