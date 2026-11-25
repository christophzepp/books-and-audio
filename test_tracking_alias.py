import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gpodder_relay import Relay
from test_native_tracking_relay import BASE, FEED, NativeSource, Upstream, FeedServer, native
from tracking_rss import TrackingResolver
import tracking_alias as alias
import tracking_guid_host as exporter


class AddressApprovalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.exchange=self.root/'exchange'
        self.exchange.mkdir()
        self.proof=self.root/'source.json'
        self.db=self.root/'relay.sqlite'
        self.ident='2:3:4'
        self.old='https://podcast.example/show/4.mp3?v=secret-old'
        self.new='https://podcast.example/show/4.mp3?v=secret-new'
        self.guid='private-rss-guid'
        self.source=NativeSource()
        self.set_source(False)
        self.rss=FeedServer([dict(episode=self.new,guid=self.guid)])
        self.resolver=TrackingResolver(request=self.rss)
        self.now=BASE+100
        self.scope=dict(upstream='https://pine.example',username='alice',user_id=2)
        self.config=dict(upstream=self.scope['upstream'],username='alice',baseline=BASE,
                         native_source=dict(user_id=2,api_key_file='/unused'))
        self.relay=self.restart()
        self.addCleanup(patch.stopall)
        patch.dict('os.environ',TRACKING_ALIAS_DIR=str(self.exchange),TRACKING_GUID_PROOF_FILE=str(self.proof)).start()
        first=self.poll()
        self.set_source(True)
        self.empty=self.poll(first['timestamp'])
        self.assertEqual(self.empty['actions'],[])
        with self.relay.db() as db:
            self.event=db.execute('SELECT event_id FROM native_outbox').fetchone()[0]

    def restart(self):
        return Relay(self.config,self.root,Upstream(),source=self.source,resolver=self.resolver,clock=lambda:self.now)

    def set_source(self,completed,**kw):
        self.source.rows=[native(self.ident,episode=self.old,guid=None,completed=completed,**kw)]

    def poll(self,cursor=None):
        self.now+=1
        return self.relay.actions(cursor,'','basic')

    def review(self,token=None,scope=None):
        opts=dict(exchange_dir=self.exchange,proof_file=self.proof,now=self.now)
        args=self.db,self.scope if scope is None else scope,self.ident,self.event
        return alias.preview(*args,**opts) if token is None else alias.approve(*args,token,**opts)

    def proof_from(self,**changes):
        data=dict(uid=2,pid=3,eid=4,username='alice',feed=FEED,old=self.old,guid=self.guid)
        data.update(changes)
        with patch('tracking_guid_host.time.time',return_value=self.now):
            return exporter.refresh(self.db,'pinepods-db',self.scope['upstream'],'alice',self.proof,
                query=lambda container,batch:[list(data.values())])

    def test_only_exact_scoped_source_and_consent_offer_existing_listen_once(self):
        self.assertEqual(self.review()['waiting_reason'],'source_guid')
        self.assertEqual(self.proof_from(),1)
        self.assertNotIn(self.guid,self.proof.read_text())
        self.assertNotIn(self.old,self.proof.read_text())
        proposal=self.review()
        self.assertEqual(proposal['status'],'completed')
        self.assertEqual(proposal['state'],'ready')
        self.assertEqual(proposal['changed_query_keys'],['v'])
        self.assertNotIn('secret-',str(proposal))
        self.assertNotIn(self.guid,str(proposal))
        self.assertEqual(self.review('0'*64)['state'],'stale')
        self.assertEqual(self.review(scope=dict(self.scope,username='bob'))['state'],'scope_mismatch')
        self.assertEqual(self.review(scope=dict(self.scope,user_id=5))['state'],'scope_mismatch')
        self.assertEqual(self.review(proposal['token'])['state'],'approved')
        self.assertEqual(self.review(proposal['token'])['state'],'approved')
        self.assertNotIn(self.new,next(self.exchange.glob('*.json')).read_text())
        self.relay=self.restart()
        self.assertEqual(self.relay.actions(None,'','basic'),self.empty)
        offered=self.poll(self.empty['timestamp'])
        self.assertEqual(len(offered['actions']),1)
        action=offered['actions'][0]
        self.assertEqual((action['guid'],action['episode'],action['position']),(self.guid,self.new,600))
        self.assertEqual(self.poll(offered['timestamp'])['actions'],[])
        with self.relay.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM native_address_approvals').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM native_tracking_events WHERE kind='play'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM native_outbox').fetchone()[0],0)
            self.assertIn(self.old,db.execute('SELECT payload FROM native_tracking_events WHERE event_id=?',(self.event,)).fetchone()[0])
        self.assertEqual(self.review()['state'],'waiting')
        self.assertEqual(self.review(proposal['token'])['state'],'approved')

    def test_change_after_approval_does_not_redirect_to_another_target(self):
        self.proof_from()
        proposal=self.review()
        self.review(proposal['token'])
        self.rss.rows[0]['episode']=self.new+'&redirect=secret'
        again=self.poll(self.empty['timestamp'])
        self.assertEqual(again['actions'],[])
        self.assertEqual(self.review(proposal['token'])['state'],'stale')
        self.assertEqual(self.review()['state'],'ready')

    def test_duplicate_guid_or_target_url_prevents_delivery(self):
        self.proof_from()
        self.review(self.review()['token'])
        self.rss.rows.append(dict(episode=self.new,guid='another-item'))
        round2=self.poll(self.empty['timestamp'])
        self.assertEqual(round2['actions'],[])
        self.assertEqual(self.review()['state'],'waiting')
        self.rss.rows[-1]=dict(episode='https://else.example/ep',guid=self.guid)
        self.assertEqual(self.poll(round2['timestamp'])['actions'],[])

    def test_old_approval_does_not_deliver_after_reset_missing_or_reassigned_source(self):
        self.proof_from()
        self.review(self.review()['token'])
        self.set_source(False)
        self.assertEqual(self.poll(self.empty['timestamp'])['actions'],[])
        self.assertEqual(self.review()['state'],'waiting')

    def test_older_approval_or_candidate_not_replayed_to_another_account(self):
        self.proof_from()
        self.review(self.review()['token'])
        data=json.loads(self.proof.read_text())
        data['scope']='another-account'
        self.proof.write_text(json.dumps(data))
        self.assertEqual(self.review()['waiting_reason'],'source_guid')
        previous=self.poll(self.empty['timestamp'])
        self.assertEqual(previous['actions'],[])
        self.proof_from()
        self.now+=alias.MAX_AGE+1
        self.assertEqual(self.review()['state'],'waiting')
        self.assertEqual(self.poll(previous['timestamp'])['actions'],[])

    def test_external_proof_allows_real_hostname_and_path_changes(self):
        self.proof_from()
        self.rss.rows=[dict(episode='https://other-cdn.example/new.mp3',guid=self.guid)]
        self.assertEqual(self.poll(self.empty['timestamp'])['actions'],[])
        review=self.review()
        self.assertEqual((review['state'],review['old_host'],review['new_host']),('ready','podcast.example','other-cdn.example'))

    def test_exporter_never_confuses_users_ids_or_exact_old_urls(self):
        for changes in (dict(uid=4),dict(username='bob'),dict(eid=9),dict(feed='https://other.example'),
                        dict(old=self.new),dict(guid='')):
            self.assertEqual(self.proof_from(**changes),0)
            self.assertEqual(self.review()['waiting_reason'],'source_guid')
        with self.assertRaises(ValueError):
            exporter.required(self.db,self.scope['upstream'],'wrong')
        with self.assertRaises(ValueError):
            exporter.query_db('pg',['2:123:1;select 42'])
        self.assertEqual(self.review(scope=dict(self.scope,upstream='https://other.example'))['state'],'scope_mismatch')

    def test_missing_approval_volume_does_not_accept_preview(self):
        self.proof_from()
        self.exchange.rmdir()
        self.assertEqual(self.review()['state'],'unavailable')

    def test_container_candidate_reader_emits_only_scoped_ids_and_hashes(self):
        def run_in_fixture(command, **kwargs):
            self.assertEqual(command[:8], ['docker','exec','-i','relay','python','-I','-B','-'])
            self.assertEqual(command[8:], [self.scope['upstream'], 'alice'])
            script = kwargs['input'].replace(b'/tracking/state/relay.sqlite', str(self.db).encode())
            process = subprocess.run([sys.executable, '-I', '-B', '-', *command[8:]],
                                     input=script, capture_output=True, check=True)
            self.assertNotIn(self.old.encode(), process.stdout)
            self.assertNotIn(FEED.encode(), process.stdout)
            return process
        direct = exporter.required(self.db, self.scope['upstream'], 'alice')
        self.assertEqual(exporter.required_from_container('relay', self.scope['upstream'], 'alice',
                                                         run=run_in_fixture), direct)
        for invalid in ('--privileged','bad/name',''):
            with self.assertRaises(ValueError):
                exporter.required_from_container(invalid,self.scope['upstream'],'alice',
                                                 run=lambda *a, **kw: self.fail('docker called'))

    def test_source_options_are_exclusive_and_relay_response_is_validated(self):
        for options in ([], ['--relay-db','db','--relay-container','relay']):
            command = [sys.executable, exporter.__file__, '--db-container','db','--upstream','https://pine.example',
                       '--pine-user','alice','--output',str(self.proof), *options]
            self.assertEqual(subprocess.run(command,capture_output=True).returncode, 2)
        bad = subprocess.CompletedProcess([],0,stdout=b'{"2:3:4":"https://private.test/url"}')
        with self.assertRaises(ValueError):
            exporter.required_from_container('relay','https://pine.example','alice',run=lambda *a,**kw:bad)
