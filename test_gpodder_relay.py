import base64
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from gpodder_relay import Relay

BASE=1700000000

def action(n=20, **kw):
    return dict(podcast='https://example.test/feed', episode='https://example.test/old.mp3',
                timestamp=datetime.fromtimestamp(BASE+n,timezone.utc).isoformat(),
                device='phone',action='play',position=100,total=100,**kw)


class Upstream:
    def __init__(self): self.rows=[];self.cursor=BASE+30;self.calls=[];self.fail=False
    def __call__(self,method,url,**kw):
        self.calls.append((method,url,kw))
        if self.fail:raise OSError('offline')
        data={'actions':self.rows,'timestamp':self.cursor}
        return type('Response',(),dict(status_code=200,content=b'{}',json=lambda s:copy.deepcopy(data)))()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.up=Upstream();self.cfg=dict(upstream='https://pine.example.test',username='user',baseline=BASE,
                                       exclude_devices=['book-progress-sync'])
        self.relay=Relay(self.cfg,self.tmp.name,self.up)
    def poll(self,since=None):return self.relay.actions(since,'','Basic test')
    def test_daily_full_request_never_starts_at_zero(self):
        self.poll();self.assertEqual(self.up.calls[-1][2]['params']['since'],BASE)
        self.poll();self.assertEqual(len(self.up.calls),1)
    def test_retry_replays_identical_batch_until_ack(self):
        self.up.rows=[action()];first=self.poll()
        restarted=Relay(self.cfg,self.tmp.name,self.up)
        self.assertEqual(first,restarted.actions(None,'','Basic test'))
        self.assertEqual(len(self.up.calls),1)
    def test_acknowledged_actions_are_not_reimported_even_on_boundary_overlap(self):
        self.up.rows=[action()];first=self.poll();self.up.cursor+=10
        self.assertEqual(self.poll(first['timestamp'])['actions'],[])
        self.assertEqual(self.relay.status()['seen'],1)
    def test_old_episode_heard_today_is_retained_but_old_play_is_not(self):
        self.up.rows=[action(-1),action(1)]
        self.assertEqual(self.poll()['actions'],[action(1)])
    def test_own_sync_events_and_zero_positions_are_not_plays(self):
        own=action();own['device']='book-progress-sync';zero=action();zero['position']=0
        self.up.rows=[own,zero,action()]
        self.assertEqual(self.poll()['actions'],[action()])
    def test_upstream_outage_keeps_ack_and_retries(self):
        self.up.rows=[action()];first=self.poll();self.up.fail=True
        with self.assertRaises(OSError):self.poll(first['timestamp'])
        self.assertEqual(self.relay.status()['acknowledged'],first['timestamp'])
        self.up.fail=False;self.up.cursor+=10
        self.assertEqual(self.poll(first['timestamp'])['actions'],[])
    def test_future_and_regressing_cursors_are_rejected(self):
        with self.assertRaises(ValueError):self.poll(BASE+999)
        self.up.cursor=BASE-1
        with self.assertRaises(ValueError):self.poll()
    def test_scope_cannot_be_changed_or_reset_on_restart(self):
        changed=dict(self.cfg,baseline=BASE+1)
        with self.assertRaises(ValueError):Relay(changed,self.tmp.name,self.up)
    def test_only_explicit_verified_aliases_rewrite_urls(self):
        self.cfg['aliases']=[dict(feed=action()['podcast'],old=action()['episode'],new='https://example.test/current.mp3')]
        self.relay=Relay(self.cfg,self.tmp.name,self.up);self.up.rows=[action()]
        self.assertEqual(self.poll()['actions'][0]['episode'],'https://example.test/current.mp3')
    def test_auth_is_forwarded_only_to_pinned_account_without_redirects(self):
        auth='Basic '+base64.b64encode(b'user:secret').decode();self.relay.authenticate(auth)
        self.assertEqual(self.up.calls[-1][2]['headers']['Authorization'],auth)
        self.assertFalse(self.up.calls[-1][2]['allow_redirects'])
        with self.assertRaises(PermissionError):self.relay.authenticate('Basic '+base64.b64encode(b'other:secret').decode())
    def test_invalid_timestamp_does_not_become_a_fabricated_play(self):
        a=action();a.pop('timestamp');self.up.rows=[a]
        with self.assertRaises(ValueError):self.poll()
        self.assertIsNone(self.relay.status()['last_poll'])

if __name__=='__main__':unittest.main()
