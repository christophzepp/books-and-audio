import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from cryptography.fernet import Fernet
from admin_store import AdminStore
from delivery_monitor import configure, reconcile, report, send_pending
from floppy_evidence import scope
from gpodder_relay import Relay
from test_native_tracking_relay import BASE, ExactResolver, NativeSource, Upstream, native
from tracking_delivery import read_episode_delivery


class OpenDeliveriesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        key = self.root / 'key'
        key.write_bytes(Fernet.generate_key())
        self.store = AdminStore(self.root, key)
        self.profiles = [
            dict(id='source-1', kind='pinepods', active=True, settings={'url':'https://pine.test','username':'reader'}),
            dict(id='notice-1', kind='ntfy', active=True, settings={'url':'https://ntfy.test/topic'})]
        self.view = {'profiles': self.profiles}
        self.store.list_connections = lambda: self.view
        self.admin = SimpleNamespace(store=self.store, manager=SimpleNamespace(public=self.store.list_connections))
        self.config = dict(ntfy_url='https://ntfy.test/topic',pinepods={'enabled':True})
        self.post = Mock(return_value=SimpleNamespace(status_code=200))
        self.now = BASE+120
        self.item = native('2:13:32', title='Folge eins', show='Podcast')
        self.source = NativeSource([self.item])
        self.relay = Relay(dict(upstream='https://pine.test', username='reader', baseline=BASE,
                           native_source={'user_id':2}),self.root,Upstream(),source=self.source,
                           resolver=ExactResolver(),clock=lambda:self.now)
        self.env = {'TRACKING_DELIVERY_DB':str(self.root/'relay.sqlite'),'TRACKING_PINE_USER_ID':'2',
                    'FLOPPY_EVIDENCE_FILE':str(self.root/'proof.json'),'FLOPPY_EVIDENCE_USER':'target',
                    'FLOPPY_EVIDENCE_RELAY_URL':'https://relay.test'}
        self.cursor = None
        self.poll()
        self.assertEqual(self.page()['total'],0)

    def page(self):
        return report(self.store, env=self.env, now=self.now)

    def configure(self, enabled):
        page = self.page()
        return configure(self.store,dict(enabled=enabled,scope=page['scope'],guard=page['guard']),
                         env=self.env,now=self.now)

    def poll(self, *, floppy=True):
        self.now += 1
        self.cursor = self.relay.actions(self.cursor, '', 'Basic dummy')['timestamp']
        if floppy:
            self.relay.record_floppy_poll(self.cursor)

    def send(self):
        return send_pending(self.admin,self.config,{'ntfy_authorization':'dummy'},now=self.now,post=self.post)

    def block(self):
        self.item['position']=28
        def missing(_):
            raise ValueError('tracking_rss_identity_ambiguous')
        self.relay.resolver.resolve=missing

    def snapshot(self, *, verified, account='target'):
        info=read_episode_delivery(self.root/'relay.sqlite',episode_id=self.item['id'])
        identity=info['delivery_match']['wire_hash']
        entry=dict(completed=False,seconds=self.item['position'],updated_at=self.now,end_at=None)
        data=dict(version=1,truncated=False,excluded=[],cursor=self.cursor,observed_at=self.now,synced_at=self.now,
                  scope=scope('https://pine.test','reader',account,'https://relay.test'),
                  episodes={identity:entry} if verified else {})
        (self.root/'proof.json').write_text(json.dumps(data))

    def test_old_open_event_uses_current_episode_labels_and_preserves_original_event(self):
        self.item.pop('title')
        self.item['show'] = 'Ursprüngliche Sendung'
        self.block()
        self.poll()
        with self.relay.db() as db:
            event_before = tuple(db.execute("SELECT event_id,snapshot,payload FROM native_tracking_events "
                                            "WHERE kind='play'").fetchone())
        self.item['title'] = '#371 – Aktueller Titel'
        self.item.pop('show')
        self.poll()
        rows = self.page()['rows']
        self.assertEqual([(row['episode_id'],row['title'],row['show']) for row in rows],
                         [(self.item['id'],self.item['title'],'Ursprüngliche Sendung')])
        self.assertEqual(rows[0]['code'],'tracking_rss_identity_ambiguous')
        with self.relay.db() as db:
            events = db.execute("SELECT event_id,snapshot,payload FROM native_tracking_events WHERE kind='play'").fetchall()
        self.assertEqual([tuple(event) for event in events], [event_before])

    def test_open_ambiguous_case_counts_only_stock_polls_after_opt_in_and_notifies_once(self):
        self.block()
        self.poll()
        page=self.page()
        self.assertEqual(page['rows'][0]['code'],'tracking_rss_identity_ambiguous')
        self.assertEqual(page['rows'][0]['last_attempt_at'],self.now)
        self.assertEqual(page['rows'][0]['poll_count'],1)
        self.assertNotIn('media.example.test',str(page))
        self.assertFalse(page['notification_enabled'])
        self.assertEqual(self.send(),0)
        self.configure(True)
        self.poll(floppy=False)
        self.page()
        self.assertEqual(self.send(),0)
        self.poll()
        self.relay.record_floppy_poll(self.cursor)
        self.assertEqual(self.page()['rows'][0]['poll_count'],2)
        self.assertEqual(self.send(),0)
        self.poll()
        self.page()
        self.assertEqual(self.send(),1)
        self.assertNotIn('Folge eins',self.post.call_args.kwargs['data'].decode())
        self.poll()
        self.page()
        self.assertEqual(self.send(),0)
        self.assertEqual(self.post.call_count,1)

    def test_acknowledged_target_waits_for_current_scoped_proof_then_closes(self):
        self.configure(True)
        self.item['position']=30
        self.poll()
        self.assertEqual(self.page()['rows'][0]['code'],'offered')
        self.poll()
        self.assertEqual(self.page()['rows'][0]['code'],'proof_wait')
        self.assertEqual(self.send(),0)
        self.snapshot(verified=True,account='another')
        self.assertEqual(self.page()['rows'][0]['code'],'proof_wait')
        self.assertEqual(self.send(),0)
        self.snapshot(verified=False)
        self.assertEqual(self.page()['rows'][0]['code'],'target_unconfirmed')
        path = self.root/'proof.json'
        snapshot = json.loads(path.read_text())
        for flags in (dict(truncated=True), dict(excluded=None),
                      dict(excluded=[read_episode_delivery(self.root/'relay.sqlite',episode_id=self.item['id'])['delivery_match']['wire_hash']])):
            path.write_text(json.dumps(dict(snapshot, **flags)))
            self.assertEqual(self.page()['rows'][0]['code'],'proof_wait')
            self.assertEqual(self.send(),0)
        self.snapshot(verified=True)
        self.assertEqual(self.page()['rows'],[])
        (self.root/'proof.json').unlink()
        self.poll()
        self.assertEqual(self.page()['rows'],[])
        self.assertEqual(self.send(),0)

    def test_disabled_changed_destination_and_unavailable_relay_cannot_notify(self):
        self.configure(True)
        self.block()
        self.poll()
        self.poll()
        self.page()
        self.profiles[1]['settings']['url']='https://other.test'
        self.assertEqual(self.send(),0)
        self.profiles[1]['settings']['url']=self.config['ntfy_url']
        self.configure(False)
        self.assertEqual(self.send(),0)
        self.configure(True)
        self.poll()
        self.poll()
        self.page()
        (self.root/'relay.sqlite').rename(self.root/'gone')
        self.assertEqual(self.page()['state'],'unavailable')
        self.assertEqual(self.send(),0)

    def test_events_acknowledged_before_monitor_baseline_and_other_clients_do_not_reappear(self):
        self.item['position']=28
        self.poll()
        self.poll()
        with self.store.db() as db:
            db.execute('DELETE FROM floppy_monitor')
        self.assertEqual(self.page()['total'],0)
        self.item['position']=44
        self.poll(floppy=False)
        self.assertEqual(self.page()['rows'][0]['code'],'offered')
        self.poll()
        self.assertEqual(self.page()['total'],0)

    def test_no_historical_target_cases_without_an_export_and_delivery_data_is_scoped(self):
        self.env['FLOPPY_EVIDENCE_USER']=''
        self.item['position']=40
        self.poll()
        self.page()
        self.poll()
        self.assertEqual(self.page()['rows'],[])
        page=self.page()
        self.profiles[0]['settings']['username']='another'
        self.assertEqual(self.page(),{'state':'scope_mismatch'})
        with self.assertRaises(RuntimeError):
            configure(self.store,dict(enabled=True,scope=page['scope'],guard=page['guard']),env=self.env,now=self.now)
