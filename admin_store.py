"""Private encrypted command queue. HTTP never mutates the sync database."""
from pathlib import Path
from contextlib import contextmanager
import datetime as dt
import json
import os
import sqlite3
import tempfile
import time
import uuid

from cryptography.fernet import Fernet

KINDS = frozenset('connection.create connection.update connection.delete connection.test connection.activate connection.deactivate folder.create folder.update folder.delete folder.test folder.activate folder.deactivate provider.preview provider.activate podcast.preview podcast.configure episode.preview episode.approve episode.status_preview episode.status_apply review.preview review.approve ebook.retry sync.pause sync.resume events.clear'.split())


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)


class AdminStore:
    def __init__(self, root, key_file):
        self.root = Path(root)
        self.control = self.root / 'control'
        self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.control.is_symlink(): raise ValueError('Unsafe control directory')
        self.cipher = Fernet(Path(key_file).read_bytes())
        self.path = self.control / 'admin.sqlite'
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL,
                status TEXT NOT NULL, result TEXT, private_result BLOB, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, detail TEXT);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: conn.close()

    def encode(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def decode(self, value):
        return json.loads(self.cipher.decrypt(value))

    def submit(self, kind, payload):
        if kind not in KINDS or not isinstance(payload, dict):
            raise ValueError('Unbekannter Verwaltungsauftrag')
        if len(json.dumps(payload, ensure_ascii=False)) > 60000:
            raise ValueError('Auftrag ist zu groß')
        ident, now = str(uuid.uuid4()), time.time()
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM commands WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError('Zu viele ausstehende Aufträge')
            db.execute('INSERT INTO commands VALUES (?,?,?,\'queued\',NULL,NULL,NULL,?,?)',
                       (ident, kind, self.encode(payload), now, now))
        return {'id': ident, 'status': 'queued'}

    @staticmethod
    def public_command(row):
        return {k: row[k] for k in ('id', 'kind', 'status', 'error', 'created_at', 'updated_at')} | {
            'result': json.loads(row['result']) if row['result'] else None}

    def command(self, ident):
        try:
            if str(uuid.UUID(ident)) != ident: raise ValueError()
        except (ValueError, TypeError, AttributeError): raise ValueError('Ungültige Auftragskennung') from None
        with self.db() as db:
            row = db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()
        if not row: raise ValueError('Auftrag nicht gefunden')
        return self.public_command(row)

    def claim(self, preview_only=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A read-only book preview may pass independent episode requests.
            # Never pass changes to accounts, folders, pause or another book.
            queued=db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY created_at,id LIMIT 20").fetchall()
            if not queued:return None
            row=queued[0]
            for item in queued:
                if item['kind']=='review.preview':
                    row=item
                    break
                if not item['kind'].startswith('episode.'):break
            if preview_only and row['kind'] not in ('review.preview','episode.status_preview'):return None
            db.execute("UPDATE commands SET status='running',updated_at=? WHERE id=?", (time.time(), row['id']))
            return {'id': row['id'], 'kind': row['kind'], 'payload': self.decode(row['payload'])}

    def finish(self, ident, result=None, private=None, error=None):
        with self.db() as db:
            db.execute('UPDATE commands SET status=?,result=?,private_result=?,error=?,updated_at=? WHERE id=? AND status=\'running\'',
                       ('failed' if error else 'success', json.dumps(result, ensure_ascii=False) if result is not None else None,
                        self.encode(private) if private is not None else None, error, time.time(), ident))
            # Never put a credential-bearing command payload in history.
            db.execute('INSERT INTO audit(at,kind,detail) VALUES (?,?,?)',
                       (stamp(), 'command.failed' if error else 'command.completed', json.dumps({'command_id': ident, 'error': error})))

    def review(self, ident, kind='review.preview'):
        with self.db() as db:
            row = db.execute("SELECT private_result,created_at FROM commands WHERE id=? AND kind=? AND status='success'", (ident,kind)).fetchone()
        if not row or not row['private_result'] or time.time() - row['created_at'] > 1800:
            raise ValueError('Prüfung fehlt oder ist abgelaufen; bitte erneut prüfen')
        return self.decode(row['private_result'])

    def recover(self):
        # A crash cannot replay a potentially applied approval or connection change.
        with self.db() as db:
            db.execute("UPDATE commands SET status='failed',error='Dienst wurde während des Auftrags neu gestartet. Zustand prüfen und bei Bedarf neu beauftragen.',updated_at=? WHERE status='running'", (time.time(),))

    def pause(self, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('paused', json.dumps(bool(value))))

    def paused(self):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=\'paused\'').fetchone()
        return bool(row and json.loads(row[0]))

    def publish(self, status, connections):
        atomic(self.control / 'status.json', dict(status, published_at=time.time()))
        atomic(self.control / 'connections-public.json', connections)

    def status(self):
        try: value = json.loads((self.control / 'status.json').read_text())
        except (OSError, ValueError):
            value = {'summary': {'healthy': False}, 'books': [], 'blocked': [], 'message': 'Warte auf den Sync-Dienst'}
        age = max(0, time.time() - value.get('published_at', 0))
        value['stale'] = age > 180
        value['age_seconds'] = round(age)
        if value['stale']: value.setdefault('summary', {})['healthy'] = False
        return value

    def list_connections(self):
        try: return json.loads((self.control / 'connections-public.json').read_text())
        except (OSError, ValueError): return {'revision': 0, 'profiles': [], 'schemas': {}, 'disabled_reason': 'Warte auf den Sync-Dienst'}

    def list_folders(self):
        try: return json.loads((self.control / 'folders-public.json').read_text())
        except (OSError, ValueError): return {'revision': 0, 'rows': [], 'access_roots': [], 'unavailable': True}

    def events(self):
        with self.db() as db:
            db.execute('BEGIN')
            marker = db.execute("SELECT value FROM settings WHERE key='events_cleared_at'").fetchone()
            cutoff = float(marker[0]) if marker else 0
            rows = db.execute('SELECT at,kind,detail FROM audit ORDER BY id DESC LIMIT 40').fetchall()
            commands = db.execute("""SELECT * FROM commands WHERE
                (updated_at > ? OR status IN ('queued','running'))
                AND (kind != 'events.clear' OR status = 'failed')
                ORDER BY updated_at DESC LIMIT 30""", (cutoff,)).fetchall()
        events = [{'at': r['at'], 'kind': r['kind'], 'detail': json.loads(r['detail'])} for r in rows]
        boundary = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).isoformat()
        try: events.extend(row for row in json.loads((self.control / 'sync-events.json').read_text())
                           if row['at'] > boundary)
        except (OSError, ValueError): pass
        events.sort(key=lambda value: value.get('at', ''), reverse=True)
        return {'events': events, 'commands': [self.public_command(r) for r in commands]}

    def clear_event_history(self, ident):
        """Worker only. Clear activities up to the request, keeping operational receipts."""
        with self.db() as db:
            sync_db = self.root / 'state.sqlite'
            has_sync_db = sync_db.is_file()
            if has_sync_db:
                db.execute('ATTACH DATABASE ? AS sync_log', (str(sync_db),))
            db.execute('BEGIN IMMEDIATE')
            request = db.execute("""SELECT created_at FROM commands WHERE id=?
                AND kind='events.clear' AND status='running'""", (ident,)).fetchone()
            if not request:
                raise ValueError('Kein laufender Auftrag zum Leeren des Protokolls.')
            cutoff = request['created_at']
            boundary = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).isoformat()
            if has_sync_db:
                db.execute('DELETE FROM sync_log.events WHERE at <= ?', (boundary,))
            db.execute('DELETE FROM audit WHERE at <= ?', (boundary,))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('events_cleared_at',?)", (str(cutoff),))
            db.execute("""UPDATE commands SET status='success', updated_at=?, result='{}'
                          WHERE id=?""", (time.time(), ident))
