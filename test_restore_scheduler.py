import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from restore_pair import PairError, current_capture, publish
from restore_schedule import DEFAULTS, GRACE, inspect_schedule, publish_policy, scheduled_after
from restore_scheduler import notify_failure, run_due
from restore_status import read_report
import test_restore_drill as drill_fixtures


class ScheduledRestoreTests(unittest.TestCase):
    def setUp(self):
        fixture = drill_fixtures.RestoreDrillTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.main, self.relay, self.fixture = fixture.main, fixture.relay, fixture
        self.root = self.main.parent
        self.catalog = self.root/'backups'
        self.control = self.root/'control'
        self.control.mkdir()
        self.result = self.control/'restore-check'
        self.now = int(time.time())
        self.settings(enabled=True)
        self.capture('nightly-snapshot-001', self.now-300)

    def settings(self, **changes):
        values={'restore_'+k:v for k,v in dict(DEFAULTS,**changes).items()}
        publish_policy(self.control, values, self.now)

    def capture(self, key, at):
        manifest=self.main/'manifest.json'
        data=json.loads(manifest.read_text())
        manifest.write_text(json.dumps(dict(data,snapshot_id=key,captured_at=at)))
        (self.relay/'image.json').write_text(json.dumps(dict(snapshot_id=key,captured_at=at+30)))

    def stage(self):
        return publish(self.catalog,self.main,self.relay,self.now)

    def tick(self, delta=0):
        return run_due(self.catalog,self.result,now=self.now+delta)

    def test_real_restore_first_immediately_then_no_read_until_next_local_slot_even_on_restart(self):
        self.stage()
        self.assertEqual(current_capture(self.catalog),(self.now-300,'nightly-snapshot-001'))
        self.assertEqual(self.tick(),'passed')
        result=read_report(self.control,now=self.now)
        self.assertEqual(result['state'],'passed')
        self.assertEqual(result['counts']['podcast_approvals'],1)
        self.assertEqual(result['schedule']['backup_at'],self.now-300)
        when=result['schedule']['next_at']
        with patch('restore_scheduler.locked',side_effect=AssertionError('backup accessed between slots')):
            self.assertIsNone(self.tick(when-self.now-1))
        self.assertEqual(self.tick(when-self.now),'passed')
        self.assertEqual(result['counts']['confirmed'],1)

    def test_stale_or_corrupt_pair_fails_without_falling_back_to_a_previous_success(self):
        self.stage()
        self.assertEqual(self.tick(),'passed')
        self.settings(enabled=True,max_age_days=1,interval_days=2)
        self.assertEqual(self.tick(3*86400),'backup_stale')
        self.assertEqual(read_report(self.control,now=self.now+3*86400)['state'],'failed')
        self.assertEqual(read_report(self.control,now=self.now+3*86400)['error'],'backup_stale')
        self.assertNotIn('counts',read_report(self.control,now=self.now+3*86400))
        self.assertEqual(read_report(self.control,now=self.now+3*86400)['schedule']['backup_at'],self.now-300)
        self.settings(enabled=True,max_age_days=365,interval_days=1)
        hashed=json.loads((self.catalog/'current.json').read_text())['pair']
        (self.catalog/hashed/'relay/pinepods-api.key').write_text('changedsecret')
        self.assertEqual(self.tick(6*86400),'backup_pair')

    def test_same_rollout_and_generation_hashes_prevent_accidental_cross_backup(self):
        pair=self.stage()
        self.capture('newer-snapshot-002', self.now-100)
        (self.relay/'image.json').write_text(json.dumps(dict(snapshot_id='wrong-snapshot-002',captured_at=self.now)))
        with self.assertRaises(PairError):
            self.stage()
        self.assertEqual(current_capture(self.catalog)[1],'nightly-snapshot-001')
        self.capture('newer-snapshot-002', self.now-100)
        next_pair=self.stage()
        self.assertNotEqual(pair,next_pair)
        self.assertFalse((self.catalog/pair).exists())
        self.assertTrue((self.main/'stack-before.tar.gz').exists())
        self.capture('older-snapshot-003', self.now-200)
        with self.assertRaises(PairError):
            self.stage()
        self.assertEqual(current_capture(self.catalog)[1],'newer-snapshot-002')

    def test_backup_symlinks_and_cross_generation_mix_never_replace_active_pair(self):
        self.stage()
        state=self.relay/'state/relay.sqlite'
        original=state.read_bytes()
        state.unlink()
        other=self.root/'another.sqlite'
        other.write_bytes(original)
        state.symlink_to(other)
        with self.assertRaises((ValueError,OSError)):
            self.stage()
        self.assertEqual(current_capture(self.catalog)[1],'nightly-snapshot-001')
        self.assertEqual(self.tick(),'passed')

    def test_enable_disable_within_one_second_runs_fresh_and_competing_checker_skips(self):
        self.stage()
        self.assertEqual(self.tick(),'passed')
        self.settings(enabled=False)
        self.settings(enabled=True)
        import fcntl
        with (self.result/'scheduler.lock').open('rb') as fd:
            fcntl.flock(fd,fcntl.LOCK_EX)
            self.assertIsNone(self.tick())
        self.assertEqual(self.tick(),'passed')

    def test_rollout_manifest_pair_and_modified_archive(self):
        main_file=self.main/'manifest.json'
        main=json.loads(main_file.read_text());[main.pop(k) for k in ('snapshot_id','captured_at')]
        main_file.write_text(json.dumps(dict(main,version='test',revision='a'*40)))
        image=self.relay/'image.json'
        image.write_text(json.dumps(dict(commit='a'*40,version='test')))
        os.utime(main_file,(self.now,self.now));os.utime(image,(self.now,self.now))
        self.stage()
        self.assertEqual(current_capture(self.catalog),(self.now,None))
        with (self.main/'stack-before.tar.gz').open('ab') as f: f.write(b'incomplete')
        with self.assertRaises(PairError):
            self.stage()

    def test_disabled_and_overdue_and_clock_change(self):
        self.assertEqual(inspect_schedule(self.result,self.now+GRACE)['state'],'not_running')
        with patch('restore_scheduler.locked',side_effect=AssertionError('disabled probe touched backups')):
            self.settings(enabled=False)
            self.assertIsNone(self.tick())
        self.assertEqual(inspect_schedule(self.result,self.now)['state'],'disabled')
        from datetime import datetime
        start=datetime.fromisoformat('2026-10-24T07:00:00+02:00').timestamp()
        when=scheduled_after(start,dict(DEFAULTS,interval_days=1))
        self.assertEqual(when-start,25*3600)

    def test_failure_notifications_deduplicate_after_restarts_and_only_new_failure_after_success(self):
        store=Mock(control=self.control)
        url='https://ntfy.example/topic'
        store.list_connections.return_value={'profiles':[dict(id='ntfy-profile',kind='ntfy',active=True,settings=dict(url=url))]}
        def notify(now):
            return notify_failure(store,{'ntfy_url':url},{'ntfy_authorization':'test'},now=now,post=post)
        post=Mock(return_value=Mock(status_code=200))
        self.assertEqual(self.tick(),'backup_missing')
        self.assertTrue(notify(self.now))
        self.assertFalse(notify(self.now+1))
        self.assertIsNone(self.tick(1))
        self.assertEqual(post.call_count,1)
        self.stage()
        self.settings(enabled=True,interval_days=1)
        self.assertEqual(self.tick(2*86400),'passed')
        self.assertFalse(notify(self.now+2*86400))
        (self.catalog/'current.json').unlink()
        self.assertEqual(self.tick(4*86400),'backup_missing')
        self.assertTrue(notify(self.now+4*86400))
        self.assertEqual(post.call_count,2)
        self.assertNotIn('test-native-key', str(post.call_args))
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_different_active_profile_cannot_receive_notice_and_overdue_notifier_retries_http(self):
        store=Mock(control=self.control)
        url='https://ntfy.example/topic'
        post=Mock(side_effect=[Mock(status_code=503),Mock(status_code=200)])
        cfg={'ntfy_url':url};cred={'ntfy_authorization':'testing'}
        def notify(delta):
            return notify_failure(store,cfg,cred,now=self.now+delta,post=post)
        store.list_connections.return_value={'profiles':[dict(id='n',kind='ntfy',active=True,settings={'url':'https://elsewhere.example/t'})]}
        self.assertFalse(notify(GRACE));post.assert_not_called()
        store.list_connections.return_value['profiles'][0]['settings']['url']=url
        self.assertFalse(notify(GRACE));self.assertFalse(notify(GRACE+3599))
        self.assertTrue(notify(GRACE+3600))
        self.assertFalse(notify(GRACE+7200))
        self.assertEqual(post.call_count,2)
