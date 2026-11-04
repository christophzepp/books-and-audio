import concurrent.futures
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import worker_witness as w

JOB = '00000000-0000-4000-8000-000000000044'

class Queue:
    def __init__(self): self.pending = []
    def submit(self, fn):
        f = concurrent.futures.Future(); self.pending.append((fn,f)); return f
    def run(self):
        while self.pending:
            fn,f = self.pending.pop(0); f.set_result(fn())

class Upstream:
    def __init__(self):
        self.worker = None; self.calls=[]; self.attempt=0; self.seq=0; self.lose=False
    def call(self, method,path,body=None):
        self.calls.append((method,path,copy.deepcopy(body)))
        if method=='GET' and path=='/health': return 200,{'status':'ok','protocol':1}
        if method=='GET': return (200,copy.deepcopy(self.worker)) if self.worker else (404,{'error':'missing'})
        if method=='DELETE':
            if self.worker: self.worker['status']='cancelled'; return 200,copy.deepcopy(self.worker)
            return 404,{'error':'missing'}
        assert method=='POST'
        self.attempt+=1; self.seq+=10
        event={'seq':self.seq,'at':w.now(),'code':'job_accepted','data':{'attempt':self.attempt}}
        self.worker={'jobId':body['jobId'],'status':'running','outputBytes':None,'events':[event]}
        if self.lose: self.lose=False; raise w.Blocked('upstream_unreachable')
        return 202,copy.deepcopy(self.worker)
    @property
    def posts(self): return sum(m=='POST' for m,_,_ in self.calls)

class Fixture(unittest.TestCase):
    def setUp(self):
        self.log_patch=patch.object(w,'print',create=True); self.log=self.log_patch.start()
        self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name).resolve()
        self.media=self.base/'media'; self.media.mkdir()
        self.source=self.media/'source.epub'; self.source.write_bytes(b'original-epub')
        self.audio=self.media/'audio.mp3'; self.audio.write_bytes(b'original-audio')
        self.work=self.base/'jobs'; self.workspace=self.work/JOB; self.workspace.mkdir(parents=True)
        self.state_dir=self.base/'state'; self.q=Queue(); self.up=Upstream(); self.service=self.restart()
        self.request={'jobId':JOB,'epubPath':str(self.source),'audioPaths':[str(self.audio)],'workDir':str(self.workspace),'language':'en','granularity':'sentence','model':'quartznet15x5','sourceBytes':27,'audioDurationSeconds':322.0,'maxOutputBytes':100000,'decisions':{'chapters':{},'audio':[]},'resumeFrom':None}
    def tearDown(self): self.tmp.cleanup(); self.log_patch.stop()
    def restart(self): return w.Witness(self.state_dir,[str(self.media)],str(self.work),self.up,self.q)
    def begin(self):
        self.assertEqual(self.service.post(self.request)[0],503); self.assertEqual(self.up.posts,0); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'input_ready'); self.assertEqual(self.up.posts,0)
    def start(self):
        self.begin(); self.assertEqual(self.service.post(self.request)[0],202); self.assertEqual(self.up.posts,1)
    def output(self):
        output=self.workspace/'output.epub'; output.write_bytes(b'generated-with-readalong')
        (self.workspace/'report.json').write_text('{"chapters":[]}')
        state=self.service.load(JOB)
        for stage,inputs in w.marker_inputs(self.request,state['input']).items():
            marker={'stage':stage,'inputs':inputs,'fingerprint':w.digest(w.marker_canonical(inputs).encode()),'finishedAt':w.now(),'durationMs':0}
            (self.workspace/('.done-'+stage)).write_bytes(w.canonical(marker))
        self.up.worker['status']='completed'; self.up.worker['outputBytes']=output.stat().st_size
    def complete(self):
        self.start(); self.output(); self.assertEqual(self.service.get(JOB)[0],503)
        self.assertFalse(self.receipt_path().exists()); self.q.run(); self.assertEqual(self.service.get(JOB)[0],200)
        return json.loads(self.receipt_path().read_bytes())
    def receipt_path(self): return self.state_dir/'receipts'/(JOB+'.json')
    def test_async_never_dispatches(self): self.begin(); self.assertEqual(self.up.posts,0)
    def test_barrier_archive_and_request_digest(self):
        r=self.complete(); raw=self.receipt_path().read_bytes()
        self.assertEqual(r['source']['sha256'],w.digest(self.source.read_bytes())); self.assertEqual(r['request_sha256'],w.digest(w.canonical(self.request)))
        self.assertEqual(raw,w.canonical(r)+b'\n'); self.assertTrue((self.state_dir/'receipts'/'archive'/(JOB+'-'+w.digest(raw)+'.json')).exists())
        self.assertLessEqual(w.timestamp(r['input_captured_at']),w.timestamp(r['completed_witness_at']))
    def test_source_change_before_dispatch(self):
        self.begin(); self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(w.Blocked,'input_identity_changed'): self.service.post(self.request)
        self.assertEqual(self.up.posts,0)
    def test_audio_change_during_generation(self):
        self.start(); self.output(); self.audio.write_bytes(b'changed'); self.service.get(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'blocked'); self.assertEqual(self.service.get(JOB)[0],503); self.assertFalse(self.receipt_path().exists())
    def test_epub_change_during_generation(self):
        self.start(); self.output(); self.source.write_bytes(b'changed'); self.service.get(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['error'],'input_identity_changed')
    def test_output_size(self):
        self.start(); self.output(); self.up.worker['outputBytes']+=1; self.service.get(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['error'],'output_bytes_mismatch')
    def marker_mutation(self,field):
        self.start(); self.output(); path=self.workspace/'.done-aligning'; m=json.loads(path.read_text())
        if field=='fingerprint': m[field]='a'*64
        else: m['inputs']['epub']['mtimeMs']+=1
        path.write_bytes(w.canonical(m)); self.service.get(JOB); self.q.run()
    def test_marker_input_mismatch(self): self.marker_mutation('inputs'); self.assertEqual(self.service.load(JOB)['error'],'stage_marker_mismatch')
    def test_marker_fingerprint_mismatch(self): self.marker_mutation('fingerprint'); self.assertEqual(self.service.load(JOB)['error'],'stage_marker_fingerprint_mismatch')
    def test_cleanup_after_durable_receipt(self):
        self.complete(); shutil.rmtree(self.workspace); self.source.unlink(); self.audio.unlink(); self.service=self.restart()
        self.assertEqual(self.service.get(JOB)[0],200); self.assertEqual(self.up.posts,1)
    def test_cleanup_without_capture_blocks(self):
        self.start(); self.output(); shutil.rmtree(self.workspace); self.service.get(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'blocked'); self.assertEqual(self.service.get(JOB)[0],503)
    def test_cancel_input(self):
        self.service.post(self.request); self.service.cancel(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'cancelled'); self.assertEqual(self.up.posts,0)
    def test_cancel_output(self):
        self.start(); self.output(); self.service.get(JOB); self.service.cancel(JOB); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'cancelled'); self.assertFalse(self.receipt_path().exists())
    def test_restart_before_dispatch(self):
        self.begin(); self.service=self.restart(); self.assertEqual(self.service.post(self.request)[0],202); self.assertEqual(self.up.posts,1)
    def test_lost_post_response_recovered_without_retry(self):
        self.begin(); self.up.lose=True
        with self.assertRaises(w.Blocked): self.service.post(self.request)
        self.assertEqual(self.service.load(JOB)['phase'],'dispatching'); self.service=self.restart()
        self.assertEqual(self.service.post(self.request)[0],200); self.assertEqual(self.up.posts,1)
    def test_restart_during_output_capture(self):
        self.start(); self.output(); self.service.get(JOB); self.q.pending.clear(); self.service=self.restart()
        self.assertEqual(self.service.get(JOB)[0],503); self.q.run(); self.assertEqual(self.service.get(JOB)[0],200)
    def test_post_pending_completion_never_repeats_generation(self):
        self.start(); self.output(); self.assertEqual(self.service.post(self.request)[0],503); self.q.run()
        self.assertEqual(self.service.post(self.request)[0],200); self.assertEqual(self.up.posts,1)
    def test_same_uuid_body_terminal_post_new_attempt(self):
        old=self.complete(); self.assertEqual(self.service.post(self.request)[0],503); self.q.run(); self.assertEqual(self.service.post(self.request)[0],202)
        self.output(); self.service.get(JOB); self.q.run(); self.assertEqual(self.service.get(JOB)[0],200)
        new=json.loads(self.receipt_path().read_bytes()); self.assertNotEqual(old['generation_id'],new['generation_id']); self.assertEqual(new['worker']['accepted']['attempt'],2)
        self.assertEqual(len(list((self.state_dir/'receipts'/'archive').iterdir())),2)
    def test_resync_changed_decisions(self):
        self.complete(); self.request['decisions']['chapters']['chapter.xhtml']='approved'; self.request['resumeFrom']='aligning'
        self.assertEqual(self.service.post(self.request)[0],503); self.q.run(); self.assertEqual(self.service.post(self.request)[0],202)
        self.output(); self.service.get(JOB); self.q.run(); self.assertEqual(self.service.get(JOB)[0],200)
    def test_request_changed_while_active(self):
        self.start(); changed=dict(self.request,granularity='word')
        self.assertEqual(self.service.post(changed)[1]['errorCode'],'request_changed_while_active'); self.assertEqual(self.up.posts,1)
    def test_unknown_running_not_claimed(self):
        self.up.call('POST','/v1/jobs',self.request)
        self.assertEqual(self.service.post(self.request)[1]['errorCode'],'unknown_running_attempt'); self.assertIsNone(self.service.load(JOB))
    def test_unknown_completed_held(self):
        self.up.call('POST','/v1/jobs',self.request); self.up.worker['status']='completed'; self.up.worker['outputBytes']=9
        self.assertEqual(self.service.get(JOB)[0],503); self.assertIsNone(self.service.load(JOB))
    def test_worker_restart_requires_fresh_capture(self):
        self.start(); self.up.worker=None; self.assertEqual(self.service.post(self.request)[0],503); self.assertEqual(self.up.posts,1); self.q.run()
        self.assertEqual(self.up.posts,1); self.assertEqual(self.service.post(self.request)[0],202)
    def test_pruned_accepted_event_afterseq(self):
        self.start(); self.output(); self.up.worker['events']=[{'seq':1001,'code':'done','at':w.now()}]
        self.assertEqual(self.service.get(JOB,999)[0],503); self.q.run(); code,body=self.service.get(JOB,999)
        self.assertEqual(code,200); self.assertEqual([e['seq'] for e in body['events']],[1001])
    def test_other_attempt_blocked(self):
        self.start(); self.up.call('POST','/v1/jobs',self.request)
        with self.assertRaisesRegex(w.Blocked,'worker_attempt_changed'): self.service.get(JOB)
    def test_dispatch_intent_before_upstream(self):
        self.begin(); original=self.up.call
        def call(method,path,body=None):
            if method=='POST': self.assertEqual(self.service.load(JOB)['phase'],'dispatching')
            return original(method,path,body)
        self.up.call=call; self.service.post(self.request)
    def test_disk_failure_not_released_or_logged(self):
        self.start(); self.output(); self.service.get(JOB); original=w.atomic
        def fail(path,*args,**kwargs):
            if '/receipts/' in str(path): raise OSError('disk full secret text')
            return original(path,*args,**kwargs)
        with patch.object(w,'atomic',fail): self.q.run()
        self.assertEqual(self.service.load(JOB)['error'],'capture_failed'); self.assertEqual(self.service.get(JOB)[0],503)
        self.assertEqual(self.log.call_count,1); self.assertNotIn('secret text',self.log.call_args.args[0])
    def test_tampered_receipt_blocked(self):
        self.complete(); self.receipt_path().write_text('{}')
        with self.assertRaisesRegex(w.Blocked,'receipt_binding_mismatch'): self.service.get(JOB)
    def test_symlink_file_and_parent_rejected(self):
        link=self.media/'link.epub'; link.symlink_to(self.source)
        with self.assertRaises(OSError): w.file_stat(str(link))
        alias=self.base/'alias'; alias.symlink_to(self.media,target_is_directory=True)
        with self.assertRaises(OSError): w.file_stat(str(alias/'source.epub'))
    def test_traversal_roots_and_bad_shape(self):
        for value in ['/etc/passwd',str(self.media)+'/../source.epub',str(self.media)+'//source.epub']:
            with self.assertRaises(w.Blocked): w.validate_request(dict(self.request,epubPath=value),[str(self.media)],str(self.work))
        for changed in [dict(self.request,extra=True),dict(self.request,audioPaths=[{}])]:
            with self.assertRaises(w.Blocked): w.validate_request(changed,[str(self.media)],str(self.work))
    def test_post_lost_before_new_acceptance_retries_old_terminal_safely(self):
        self.complete()
        self.service.post(self.request); self.q.run()
        original = self.up.call
        def lose_before(method, path, body=None):
            if method == 'POST': raise w.Blocked('upstream_unreachable')
            return original(method,path,body)
        self.up.call = lose_before
        with self.assertRaises(w.Blocked): self.service.post(self.request)
        self.up.call = original
        self.assertEqual(self.service.post(self.request)[0],503)
        self.q.run(); self.assertEqual(self.service.post(self.request)[0],202)
        self.assertEqual(self.up.posts,2)
    def test_delete_completed_cannot_bypass_barrier(self):
        self.start(); self.output(); original=self.up.call
        def completed_delete(method,path,body=None):
            if method=='DELETE': return 200,copy.deepcopy(self.up.worker)
            return original(method,path,body)
        self.up.call=completed_delete
        self.assertEqual(self.service.cancel(JOB)[0],503)
        self.assertFalse(self.receipt_path().exists()); self.assertEqual(self.service.load(JOB)['phase'],'cancelled')
    def test_blocked_job_does_not_disable_health_or_another_job(self):
        self.assertEqual(self.service.health()[0],200)
        self.request['sourceBytes']+=1; self.service.post(self.request); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'blocked')
        self.assertEqual(self.service.health()[0],200)
        self.assertEqual(self.service.post(self.request)[0],503)
        self.assertEqual(self.log.call_count,1)
        other='00000000-0000-4000-8000-000000000045'
        request=dict(self.request,jobId=other,workDir=str(self.work/other),sourceBytes=27)
        self.assertEqual(self.service.post(request)[0],503); self.q.run()
        self.assertEqual(self.service.post(request)[0],202)
        self.assertEqual(self.service.load(other)['phase'],'active')
    def test_shutdown_keeps_pending_capture_restartable(self):
        self.service.post(self.request)
        w.STOPPING.set()
        try: self.q.run()
        finally: w.STOPPING.clear()
        self.assertEqual(self.service.load(JOB)['phase'],'input_pending')
        self.service=self.restart(); self.service.post(self.request); self.q.run()
        self.assertEqual(self.service.load(JOB)['phase'],'input_ready'); self.assertEqual(self.up.posts,0)
    def test_hash_change_even_with_mocked_stable_metadata(self):
        self.start(); self.output(); original=w.read_file
        def altered(path,*args,**kwargs):
            meta,raw=original(path,*args,**kwargs)
            if path==str(self.source): meta['sha256']='b'*64
            return meta,raw
        self.service.get(JOB)
        with patch.object(w,'read_file',altered): self.q.run()
        self.assertEqual(self.service.load(JOB)['error'],'source_bytes_changed')
    def test_atomic_receipt_fsync_before_completion_release(self):
        self.start(); self.output(); self.service.get(JOB)
        with patch.object(w.os,'fsync',wraps=w.os.fsync) as fsync: self.q.run()
        self.assertGreaterEqual(fsync.call_count,6)
        self.assertEqual(self.service.get(JOB)[0],200)
    def test_size_bounds(self):
        with self.assertRaisesRegex(w.Blocked,'file_size_limit'): w.read_file(str(self.source),2,hashed=True)
        self.request['sourceBytes']+=1; self.service.post(self.request); self.q.run(); self.assertEqual(self.service.load(JOB)['error'],'source_bytes_mismatch')
    def test_permissions_and_immutable_archive(self):
        self.complete()
        for path in [self.state_dir,self.state_dir/'receipts',self.receipt_path()]: self.assertEqual(path.stat().st_mode & 0o077,0)
        path=self.state_dir/'immutable'; w.atomic(path,b'one',immutable=True); w.atomic(path,b'one',immutable=True)
        with self.assertRaises(w.Blocked): w.atomic(path,b'two',immutable=True)
        self.assertEqual(path.read_bytes(),b'one')

class Protocol(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(w.route('GET','/health'),('health',None,0)); self.assertEqual(w.route('GET','/v1/jobs/'+JOB+'?afterSeq=12'),('status',JOB,12))
        for method,path in [('GET','/health?x=1'),('PUT','/v1/jobs/'+JOB),('GET','/v1/jobs/'+JOB+'?afterSeq=-1'),('GET','/v1/jobs/'+JOB+'?afterSeq=1&afterSeq=2'),('GET','/v1/jobs/%2e%2e'),('DELETE','/v1/jobs/'+JOB+'?afterSeq=1'),('GET','https://evil/health')]:
            with self.assertRaises(w.Blocked): w.route(method,path)
    def test_json_rejects_duplicates_and_nonfinite(self):
        for raw in [b'{"a":1,"a":2}',b'{"a":NaN}',b'bad']:
            with self.assertRaises(w.Blocked): w.parse_json(raw)
    def test_node_numbers(self): self.assertEqual(w.marker_canonical([1.0,1.5,1e-7,1e-6,1e21,-0.0]),'[1,1.5,1e-7,0.000001,1e+21,0]')
    def test_no_credentials_in_upstream_url(self):
        for url in ['file:///etc/passwd','http://user:password@worker','http://worker/private','http://worker?token=secret']:
            with self.assertRaises(w.Blocked): w.Upstream(url,'secret')

class HttpTests(Fixture):
    # Exercise the actual HTTP parser/handler through a memory transport; no
    # network permission or production endpoint is used by these unit tests.
    def send(self,method,path,body=None,headers=None):
        import io
        import types
        class Socket:
            def __init__(self,raw): self.input=io.BytesIO(raw); self.output=bytearray()
            def makefile(self,*args): return self.input
            def sendall(self,raw): self.output.extend(raw)
            def settimeout(self,value): pass
        body=body or b''; headers=dict(headers or {})
        headers.setdefault('Content-Length',str(len(body)))
        raw=(method+' '+path+' HTTP/1.1\r\nHost: localhost\r\n'+''.join(key+': '+value+'\r\n' for key,value in headers.items())+'\r\n').encode()+body
        connection=Socket(raw)
        w.Handler(connection,('127.0.0.1',1000),types.SimpleNamespace(witness=self.service,token='fixture-secret'))
        head,raw=bytes(connection.output).split(b'\r\n\r\n',1)
        return int(head.split(b' ',2)[1]),raw
    def test_http_auth_required_even_health(self):
        code,raw=self.send('GET','/health'); self.assertEqual(code,401); self.assertNotIn(b'fixture-secret',raw)
        code,raw=self.send('GET','/health',headers={'Authorization':'Bearer fixture-secret'})
        self.assertEqual(code,200); self.assertEqual(json.loads(raw)['status'],'ok')
    def test_http_authenticated_post_only_prepares(self):
        code,raw=self.send('POST','/v1/jobs',w.canonical(self.request),{'Authorization':'Bearer fixture-secret','Content-Type':'application/json'})
        self.assertEqual(code,503); self.q.run(); self.assertEqual(self.up.posts,0)
        self.assertNotIn(b'fixture-secret',b''.join(p.read_bytes() for p in (self.state_dir/'jobs').glob('*.json')))
    def test_http_unknown_path_or_oversize_body(self):
        auth={'Authorization':'Bearer fixture-secret'}
        self.assertEqual(self.send('GET','/v1/jobs/%2e%2e',headers=auth)[0],503)
        self.assertEqual(self.send('POST','/v1/jobs',b'',dict(auth,**{'Content-Length':str(w.BODY_LIMIT+1)}))[0],503)

# Share setUp/helpers without rerunning the non-HTTP fixture test methods.
for _name in list(Fixture.__dict__):
    if _name.startswith('test_') and _name not in HttpTests.__dict__:
        setattr(HttpTests, _name, None)

if __name__=='__main__': unittest.main()
