import base64
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from admin_store import AdminStore
from connection_manager import ConnectionManager
from folder_manager import FolderManager
from gpodder_relay import Relay
from restore_drill import CHECKS, DrillError, drill, save_report
from restore_status import read_report
from sync import Bridge
from test_native_tracking_relay import BASE, ExactResolver, NativeSource, native


def write_json(path, data):
    path.write_text(json.dumps(data))


class RestoreDrillTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stack, self.main, self.relay = [root/name for name in ('stack', 'cold-main', 'cold-relay')]
        self.main.mkdir()
        self.relay.mkdir()
        config, state = self.stack/'config', self.stack/'state'
        config.mkdir(parents=True)
        auth = state/'web-auth'
        auth.mkdir(parents=True)
        write_json(auth/'credentials.json', dict(version=1, username='viewer', hash='f'*128,
                                                 salt=base64.b64encode(bytes(16)).decode()))
        pine = dict(url='http://pinepods:8055', username='listener', server_version='0.9.0',
                    device_id='reader', enabled=True)
        cfg = dict(abs_url='http://abs:80', abs_version='2', pinepods=pine,
                   auto_discovery=dict(abs_library_id='audio'))
        credentials = dict(abs_token='abs-test-secret', pinepods_password='test-secret')
        key = Fernet.generate_key()
        (config/'secret.key').write_bytes(key)
        (config/'credentials.enc').write_bytes(Fernet(key).encrypt(json.dumps(credentials).encode()))
        write_json(config/'config.json', cfg)
        admin = AdminStore(state, config/'secret.key')
        admin.submit('review.preview', dict(test='held command'))
        FolderManager(state, cfg, access_roots=[]).seed()
        ConnectionManager(state, cfg, credentials, key).seed()
        bridge = Bridge(state, cfg, credentials)
        bridge.save('test-book', dict(position=123))
        bridge.save('_pine_alias:episode', dict(approved=True))
        with bridge.db:
            bridge.db.execute('INSERT INTO auto_mappings VALUES (?,?,?)', ('b', 1, '{"id":"b"}'))
        bridge.db.close()
        self.archive()
        self.tracking = dict(upstream=pine['url'], username=pine['username'], baseline=BASE,
            native_source=dict(api_key_file='/tracking/pinepods-api.key', user_id=2, server_version='0.9.0'))
        write_json(self.relay/'config.json', self.tracking)
        (self.relay/'pinepods-api.key').write_text('test-native-key')
        rows, clock = NativeSource([native(), native('u2:p3:e5')]), [BASE + 100]
        relay = Relay(self.tracking, self.relay/'state', source=rows, resolver=ExactResolver(), clock=lambda: clock[0])
        response = relay.actions(None, '', None)
        rows.rows[0]['completed'] = True
        clock[0] += 1
        response = relay.actions(response['timestamp'], '', None)
        rows.rows[1]['completed'] = True
        clock[0] += 1
        relay.actions(response['timestamp'], '', None)
        self.expected_ack = response['timestamp']

    def archive(self, volume_names=False,extra=None):
        path = self.main/'stack-before.tar.gz'
        with tarfile.open(path,'w:gz') as out:
            for item in sorted(self.stack.rglob('*')):
                if not item.is_file():
                    continue
                name = str(item.relative_to(self.stack))
                if volume_names:
                    name = name.replace('config/', 'config/current/').replace('state/control/', 'control/').replace('state/web-auth/', 'web/web-auth/')
                    if name == 'config/current/secret.key':
                        out.add(item, 'web_key/secret.key')
                out.add(item,name)
            if extra:
                member = tarfile.TarInfo(extra)
                member.size = 1
                out.addfile(member,io.BytesIO(b'x'))
        write_json(self.main/'manifest.json',dict(schema=1,archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),protected={}))

    def result(self, aliases=None):
        with patch('socket.getaddrinfo', side_effect=AssertionError('restore used DNS')), \
                patch('socket.socket.connect', side_effect=AssertionError('restore used network')):
            return drill(self.main,self.relay,aliases)

    def test_cold_restore_uses_application_schemas_compares_history_and_never_replays(self):
        data = {p:p.read_bytes() for d in (self.relay,self.main) for p in d.rglob('*') if p.is_file()}
        report = self.result()
        self.assertEqual(report['error'],None)
        self.assertEqual(report['checks'],dict.fromkeys(CHECKS,True))
        self.assertEqual(report['counts'],dict(profiles=2,active_profiles=2,state_records=2,
            mapping_approvals=1,podcast_approvals=1,relay_episodes=2,relay_events=6,confirmed=1,pending_actions=1))
        self.assertEqual(report['cursor'],self.expected_ack)
        self.assertEqual(data,{p:p.read_bytes() for d in (self.relay,self.main) for p in d.rglob('*') if p.is_file()})
        for private in ('viewer','listener','test-secret','test-native-key','episode.mp3','pinepods:'):
            self.assertNotIn(private,json.dumps(report))

    def test_pending_alias_request_and_applied_receipt_survive_restore(self):
        cursor=self.relay/'state/relay.sqlite'
        with closing(sqlite3.connect(cursor)) as db, db:
            event_id,=db.execute("SELECT event_id FROM native_tracking_events WHERE kind='play' LIMIT 1").fetchone()
            db.execute('CREATE TABLE IF NOT EXISTS native_address_approvals(event_id TEXT PRIMARY KEY,token TEXT,approved_at INTEGER)')
            db.execute('INSERT INTO native_address_approvals VALUES(?,?,?)',(event_id,'a'*64,1700000001))
        spool=self.stack/'state/control/tracking-alias'
        spool.mkdir()
        item=spool/(event_id+'.json')
        request=dict(version=1,event_id=event_id,token='a'*64,approved_at=1700000000)
        write_json(item,request)
        self.archive()
        self.assertTrue(self.result()['ok'])
        write_json(item,dict(request,token='b'*64))
        self.archive()
        self.assertEqual(self.result()['error'],'relay_cursor')

    def test_public_volume_layout(self):
        self.archive(volume_names=True)
        self.assertTrue(self.result()['ok'])

    def test_pre_delivery_ledger_backup_can_be_reopened_with_existing_cursor(self):
        with closing(sqlite3.connect(self.relay/'state/relay.sqlite')) as db, db:
            db.execute("DELETE FROM state WHERE id='delivery_mode'")
        self.assertEqual(self.result()['cursor'],self.expected_ack)
        self.assertTrue(self.result()['ok'])

    def test_missing_or_wrong_decryption_key_fails_accounts(self):
        file = self.stack/'config/secret.key'
        file.write_bytes(Fernet.generate_key())
        self.archive()
        self.assertEqual(self.result()['error'],'accounts')
        file.unlink()
        self.archive()
        self.assertEqual(self.result()['error'],'accounts')

    def test_corrupt_book_state_fails_before_opening_relay(self):
        path = self.stack/'state/state.sqlite'
        path.write_bytes(b'damaged')
        self.archive()
        self.assertEqual(self.result()['error'],'approvals_progress')

    def test_missing_relay_or_api_key_fails_before_reporting_cursor(self):
        (self.relay/'pinepods-api.key').unlink()
        report = self.result()
        self.assertEqual(report['error'],'relay_backup')
        self.assertIsNone(report['cursor'])

    def test_missing_baseline_and_orphan_or_duplicate_ack_are_rejected(self):
        path = self.relay/'state/relay.sqlite'
        original = path.read_bytes()
        for sql in ["DELETE FROM native_tracking_events WHERE kind='baseline'",
                    "INSERT INTO seen VALUES('native:missing-event')",
                    "UPDATE state SET value='true' WHERE id='ack'",
                    "UPDATE state SET value='{}' WHERE id='pending'"]:
            with self.subTest(sql=sql):
                path.write_bytes(original)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(sql)
                self.assertEqual(self.result()['error'],'relay_cursor')

    def test_changed_origin_needs_explicit_pair_and_accounts_must_still_match(self):
        new = 'https://pine.example.test'
        old = self.tracking['upstream']
        path = self.relay/'state/relay.sqlite'
        def replace(before, after):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE state SET value=replace(value,?,?) WHERE id IN ('scope','native_scope')",(before,after))
        self.tracking['upstream'] = new
        write_json(self.relay/'config.json',self.tracking)
        replace(old,new)
        self.assertEqual(self.result()['error'],'account_match')
        self.assertTrue(self.result([old,new])['ok'])
        self.tracking['username']='another'
        write_json(self.relay/'config.json',self.tracking)
        replace('listener','another')
        self.assertEqual(self.result([old,new])['error'],'account_match')

    def test_archived_traversal_and_unmatched_digest_are_rejected(self):
        self.archive(extra='../outside')
        self.assertEqual(self.result()['error'],'main_backup')
        self.archive()
        with (self.main/'stack-before.tar.gz').open('ab') as out:
            out.write(b'modified')
        self.assertEqual(self.result()['error'],'main_backup')

    def test_failures_can_be_published_without_touching_backups(self):
        missing = self.main.parent/'absent'
        report = drill(missing,self.relay)
        directory = self.main.parent/'restore-check'
        directory.mkdir()
        path = directory/'last.json'
        save_report(path,report,[missing,self.relay])
        self.assertEqual(read_report(directory.parent)['error'],'main_backup')
        save_report(path,self.result(),[self.main,self.relay])
        self.assertEqual(read_report(directory.parent)['state'],'passed')
        with self.assertRaises(DrillError):
            save_report(self.relay/'last.json',report,[missing,self.relay])
        link = directory/'link'
        link.symlink_to(path)
        with self.assertRaises(DrillError):
            save_report(link,report,[missing,self.relay])
