"""Clearing the activity view must preserve active reviews and later work."""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from admin_runtime import Administration
from admin_store import AdminStore


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / 'key'
        self.key.write_bytes(Fernet.generate_key())
        self.store = AdminStore(self.root, self.key)
        self.admin = SimpleNamespace(store=self.store)

    def clear(self):
        ident = self.store.submit('events.clear', {'confirm': True})['id']
        Administration.process_one(self.admin, None)
        self.assertEqual(self.store.command(ident)['status'], 'success')

    def test_clear_removes_old_activity_but_preserves_reviews_new_events_and_operations(self):
        preview = self.store.submit('episode.preview', {})['id']
        self.store.claim()
        self.store.finish(preview, {}, private={'episode_id': '258'})
        command = self.store.submit('events.clear', {'confirm': True})
        cutoff = self.store.command(command['id'])['created_at']
        past, future = [dt.datetime.fromtimestamp(cutoff + delta, dt.timezone.utc).isoformat()
                        for delta in (-10, 10)]
        cache = [{'at': past, 'kind': 'old'}, {'at': future, 'kind': 'new'}]
        (self.store.control / 'sync-events.json').write_text(json.dumps(cache))
        with sqlite3.connect(self.root/'state.sqlite') as db:
            db.executescript('CREATE TABLE events(at TEXT); CREATE TABLE operations(id TEXT);')
            db.executemany('INSERT INTO events VALUES (?)', [(past,), (future,)])
            db.execute("INSERT INTO operations VALUES ('active-transfer')")
        pending = self.store.submit('sync.pause', {})['id']
        Administration.process_one(self.admin, None)
        reopened = AdminStore(self.root, self.key)
        self.assertEqual(reopened.command(command['id'])['status'], 'success')
        self.assertEqual(reopened.review(preview, 'episode.preview'), {'episode_id':'258'})
        events = reopened.events()
        self.assertEqual(events['events'], [cache[1]])
        self.assertEqual([c['id'] for c in events['commands']], [pending])
        with sqlite3.connect(self.root/'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT at FROM events').fetchall(), [(future,)])
            self.assertEqual(db.execute('SELECT id FROM operations').fetchall(), [('active-transfer',)])
        self.assertEqual(reopened.claim()['id'], pending)
        reopened.finish(pending, {})
        self.assertEqual(reopened.events()['commands'][0]['status'], 'success')

    def test_empty_log_can_be_cleared_twice_before_setup(self):
        self.clear()
        self.clear()
        self.assertEqual(self.store.events(), {'events': [], 'commands': []})
        self.assertFalse((self.root/'state.sqlite').exists())

    def test_worker_refuses_unconfirmed_clear(self):
        ident = self.store.submit('events.clear', {'confirm':1})['id']
        Administration.process_one(self.admin, None)
        self.assertEqual(self.store.command(ident)['status'], 'failed')
        self.assertEqual(self.store.events()['commands'][0]['id'], ident)
