"""A flag-only opt-in never clears or silently expands existing episode locks."""
import copy
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import admin_runtime as runtime
from connection_manager import variant_flag_only_enable
from pinepods_client import EpisodeUnavailable
import test_admin_runtime as fixtures

FLAG='pinepods_variant_status_only'
IDS=('_podcast:show:first','_podcast:show:second')


def report():
    rows=[];blocked=[]
    for ident,code in zip(IDS,('media_identity_mismatch','duration_mismatch')):
        reason=EpisodeUnavailable.REASONS[code]
        rows.append(dict(id=ident,title='Episode',duration=1000,progress_status='blocked',pine_status='blocked',
                         pine_reason_code=code,reason=reason))
        blocked.append(dict(id=ident,title='Episode',reason=reason))
    rows.append(dict(id='_podcast:show:new',duration=900,position=0,status='not_started',
                     progress_status='unchanged',pine_status='waiting',states={'abs':{'position':0,'status':'not_started'},
                                                                             'jf':{'position':0,'status':'not_started'}}))
    return dict(enabled=True,mode='dry-run',ok=False,mapped=3,scope_fingerprint='a'*64,
                episodes=rows,blocked=blocked,pine_metadata_orphaned=[])


class ActivationTests(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.config['podcasts']=dict(enabled=True,pinepods_enabled=True,pinepods_optional_enrollment=True,
            pinepods_variant_status_only=False,bookorbit_enabled=False,abs_library_id='podcasts',jellyfin_library_id='podcasts',
            abs_root='/podcasts',jellyfin_root='/podcasts',worker_root='/podcasts',expected_abs_username='reader')
        self.config['pinepods']=dict(enabled=True,url='http://pinepods:8040',username='reader',device_id='sync',
                                    server_version='0.9.0',completion_margin_seconds=60)
        self.credentials.update(pinepods_password='PRIVATE',pinepods_api_key='PRIVATE-KEY')
        self.admin=runtime.Administration(self.root,self.key,self.config,self.credentials,fixtures.CandidateBridge)
        self.gate=dict(checked_at=time.time(),podcasts_ok=True,pinepods_ok=True,
            podcast_mounts={role:dict(source='/srv/example-media/podcasts',destination='/podcasts',rw=role=='abs')
                            for role in ('worker','abs','jf')})
        self.write_gate()
        for ident in IDS:self.put(ident,{'pine_enrolled':True})
        self.before=self.saved_state();self.oldbytes=self.admin.manager.path.read_bytes()
    def write_gate(self):
        (self.root/'deployment.json').write_text(json.dumps(self.gate))
    def activate(self,changes=None,preview=None,kind='podcast.configure'):
        public=self.admin.manager.public();settings=dict(public['podcasts'],**{FLAG:True})
        settings.update(changes or {})
        item=self.admin.store.submit(kind,dict(expected_revision=public['revision'],settings=settings))
        with patch('podcast_sync.preview',side_effect=preview or (lambda bridge:report())):
            self.admin.process_one(None)
        return self.admin.store.command(item['id'])
    def test_exact_flag_activation_preserves_two_locks_state_and_accounts(self):
        seen=[]
        def preview(bridge):
            seen.append((bridge.config['podcasts'][FLAG],bridge.root));return report()
        result=self.activate(preview=preview)
        self.assertEqual(result['status'],'success',result)
        self.assertEqual([value for value,_ in seen],[False,True,False])
        self.assertEqual(len({path for _,path in seen}),3)
        self.assertTrue(all(not path.exists() for _,path in seen))
        self.assertTrue(self.admin.manager.public()['podcasts'][FLAG])
        self.assertEqual(result['result']['preview']['retained_blocked'],2)
        self.assertEqual(self.saved_state(),self.before)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn(IDS[0],json.dumps(result))
    def test_general_configure_remains_strict(self):
        for change in ({'abs_library_id':'another'},{'pinepods_optional_enrollment':False}):
            result=self.activate(change);self.assertEqual(result['status'],'failed')
            self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_unchanged_existing_unresolved_identity_remains_blocked(self):
        def preview(bridge):
            value=report();row=value['episodes'][0]
            row['pine_reason_code']='episode_identity_unresolved'
            row['reason']=EpisodeUnavailable.REASONS['episode_identity_unresolved']
            value['blocked'][0]['reason']=row['reason'];return value
        result=self.activate(preview=preview)
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(result['result']['preview']['retained_blocked'],2)
        self.assertEqual(self.saved_state(),self.before)
    def test_real_watcher_refresh_between_long_previews_preserves_original_receipts(self):
        now=[1000];self.gate['checked_at']=now[0];self.write_gate();observed=[]
        def preview(bridge):
            gate=json.loads((bridge.root/'deployment.json').read_text())
            observed.append(gate['checked_at']);self.assertEqual(gate,self.gate)
            now[0]+=120;self.gate['checked_at']=now[0];self.write_gate();return report()
        with patch.object(runtime.time,'time',side_effect=lambda:now[0]):result=self.activate(preview=preview)
        self.assertEqual(result['status'],'success',result);self.assertEqual(observed,[1000,1120,1240])
        self.assertEqual(json.loads((self.root/'deployment.json').read_text()),self.gate)
        self.assertEqual(self.saved_state(),self.before)
    def test_no_watcher_refresh_never_fakes_a_new_timestamp(self):
        now=[1000];self.gate['checked_at']=now[0];self.write_gate();before=(self.root/'deployment.json').read_bytes();calls=[]
        def preview(bridge):calls.append(1);now[0]+=151;return report()
        with patch.object(runtime.time,'time',side_effect=lambda:now[0]):result=self.activate(preview=preview)
        self.assertEqual(result['status'],'failed');self.assertEqual(len(calls),1)
        self.assertEqual((self.root/'deployment.json').read_bytes(),before)
        self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_changed_host_evidence_or_final_expiry_rejects_even_with_same_scope(self):
        original=copy.deepcopy(self.gate)
        for failure_phase,kind in ((1,'mount'),(1,'pine'),(3,'mount'),(3,'expired')):
            self.gate=copy.deepcopy(original);self.write_gate();calls=[0]
            def preview(bridge):
                calls[0]+=1
                if calls[0]==failure_phase:
                    if kind=='mount':
                        for row in self.gate['podcast_mounts'].values():row['source']='/mnt/replaced/podcasts'
                    elif kind=='pine':self.gate['pinepods_ok']=False
                    else:self.gate['checked_at']=time.time()-151
                    self.write_gate()
                return report()
            result=self.activate(preview=preview)
            self.assertEqual(result['status'],'failed',(failure_phase,kind,result))
            self.assertEqual(calls[0],failure_phase)
            self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_private_snapshot_connections_are_closed_after_callback(self):
        opened=[];connect=sqlite3.connect
        def tracked(database,*args,**kwargs):
            db=connect(database,*args,**kwargs)
            if 'podcast-variant-check-' in str(database):opened.append(db)
            return db
        with patch.object(runtime.sqlite3,'connect',side_effect=tracked):result=self.activate()
        self.assertEqual(result['status'],'success',result);self.assertGreaterEqual(len(opened),7)
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
    def test_preview_stays_strict_and_never_enables(self):
        result=self.activate(kind='podcast.preview');self.assertEqual(result['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_new_changed_or_disappearing_block_rejects_activation(self):
        for mutation in ('new','reason','removed','code'):
            calls=0
            def preview(bridge):
                nonlocal calls
                calls+=1;value=report()
                if calls==2:
                    if mutation=='new':
                        value['episodes'][-1].update(progress_status='blocked',reason='new conflict')
                        value['blocked'].append(dict(id='_podcast:show:new',reason='new conflict'))
                    elif mutation=='reason':value['blocked'][0]['reason']='Changed failure'
                    elif mutation=='removed':value['blocked'].pop();value['episodes'][1]['progress_status']='unchanged'
                    else:value['episodes'][0]['pine_reason_code']='episode_identity_unresolved'
                return value
            self.assertEqual(self.activate(preview=preview)['status'],'failed')
            self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_unenrolled_block_never_qualifies(self):
        self.put(IDS[0],{});self.assertEqual(self.activate()['status'],'failed')
    def test_systemic_or_mount_account_failure_never_qualifies(self):
        variants=[dict(report(),scope_fingerprint=None),dict(report(),fatal=True),
                  dict(report(),blocked=[{'reason':'Account changed'}]),
                  dict(report(),pine_metadata_orphaned=['_podcast:gone'])]
        for value in variants:
            self.assertEqual(self.activate(preview=lambda bridge:copy.deepcopy(value))['status'],'failed')
        def fail(bridge):raise RuntimeError('Deployment check failed')
        self.assertEqual(self.activate(preview=fail)['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_scope_or_core_status_change_between_checks_rejects(self):
        for field in ('scope','position','third'):
            calls=0
            def preview(bridge):
                nonlocal calls
                calls+=1;value=report()
                if calls==(3 if field=='third' else 2):
                    if field=='scope':value['scope_fingerprint']='b'*64
                    else:value['episodes'][-1]['position']=12
                return value
            self.assertEqual(self.activate(preview=preview)['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_candidate_private_mutation_is_rejected(self):
        def preview(bridge):
            bridge.db.execute("INSERT INTO state VALUES('unexpected','{}')");bridge.db.commit();return report()
        self.assertEqual(self.activate(preview=preview)['status'],'failed')
        self.assertEqual(self.saved_state(),self.before)
    def test_any_pending_operation_blocks_opt_in(self):
        self.put('_pine_metadata:any',{'pending':{'stage':'sending'}})
        self.assertEqual(self.activate()['status'],'failed')
        self.assertEqual(self.admin.manager.path.read_bytes(),self.oldbytes)
    def test_scope_predicate_accepts_only_exact_false_to_true(self):
        new=copy.deepcopy(self.config);new['podcasts'][FLAG]=True
        self.assertTrue(variant_flag_only_enable(self.config,new))
        for name in ('abs_url','bo_url'):
            changed=copy.deepcopy(new);changed[name]='http://changed'
            self.assertFalse(variant_flag_only_enable(self.config,changed))
        self.assertFalse(variant_flag_only_enable(new,self.config))
        self.assertFalse(variant_flag_only_enable(new,new))
        for name in ('enabled','pinepods_enabled','pinepods_optional_enrollment'):
            old=copy.deepcopy(self.config);old['podcasts'][name]=False
            changed=copy.deepcopy(old);changed['podcasts'][FLAG]=True
            self.assertFalse(variant_flag_only_enable(old,changed))

if __name__=='__main__':unittest.main()
