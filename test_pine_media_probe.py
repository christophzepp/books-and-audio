import hashlib
import importlib.util
import io
import tempfile
import copy
import datetime as dt
import json
from pathlib import Path
import socket
import subprocess
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

import pine_media_probe as probe


def frame_prefix():
    header=bytes.fromhex('fffb9000');size=probe.frame(header,0)['frame_bytes']
    first=bytearray(size);first[:4]=header
    first[36:52]=b'Xing'+(3).to_bytes(4,'big')+(100000).to_bytes(4,'big')+(40000000).to_bytes(4,'big')
    return bytes(first)+(header+b'\0'*(size-4))*3


def fake_process(data):
    result=MagicMock();result.returncode=0;result.communicate.return_value=(data,None)
    result.poll.return_value=0;result.wait.return_value=0;result.pid=999999;result.stderr=io.BytesIO()
    return result


class RuntimeProbeTests(unittest.TestCase):
    def evidence(self,url='https://example.test/media?private=x'):
        with patch.object(probe,'fetch_prefix',return_value=(frame_prefix(),{'response_total_bytes':40000000,'bytes_read':1668})):
            return probe.worker_result(url)

    def test_frame_counter_and_stale_counter_guard(self):
        self.assertTrue(probe.header_duration(frame_prefix(),40000000)['known'])
        self.assertFalse(probe.header_duration(frame_prefix(),41000000)['known'])
        self.assertFalse(probe.header_duration(frame_prefix()[:100],40000000)['known'])

    def test_url_and_credentials_never_in_command_or_environment(self):
        url='https://example.test/media?private=x';result=self.evidence(url)
        process=fake_process(json.dumps(result).encode())
        with patch.object(probe.subprocess,'Popen',return_value=process) as run:
            answer=probe.measure(url,10.0)
        self.assertTrue(answer['known'])
        self.assertNotIn('private=x',str(run.call_args.args[0]))
        self.assertEqual(json.loads(process.communicate.call_args.kwargs['input']),{'url':url,'full_probe':False,'directory':None,'previous_evidence':None})
        self.assertEqual(process.communicate.call_args.kwargs['timeout'],10.0)
        self.assertTrue(run.call_args.kwargs['start_new_session'])
        self.assertEqual(set(run.call_args.kwargs['env']),{'PATH','LANG','PYTHONDONTWRITEBYTECODE'})
        self.assertNotIn('private=x',json.dumps(answer))

    def test_wall_timeout_and_failed_process_are_sanitized(self):
        process=fake_process(b'');process.communicate.side_effect=subprocess.TimeoutExpired(['secret'],10);process.poll.return_value=None
        with patch.object(probe.subprocess,'Popen',return_value=process),patch.object(probe.os,'killpg') as kill:
            self.assertEqual(probe.measure('https://example.test/a'),{'known':False,'reason':'probe_timeout'})
        kill.assert_called_once_with(process.pid,probe.signal.SIGKILL)
        with patch.object(probe.subprocess,'Popen',side_effect=OSError('private-url')):
            self.assertEqual(probe.measure('https://example.test/a'),{'known':False,'reason':'probe_process_failed'})

    def test_worker_output_hash_identity_and_shape_must_match(self):
        url='https://example.test/media?private=x'
        variants=[self.evidence(),self.evidence(),self.evidence()]
        variants[0]['duration_seconds']=1
        variants[1]['source_url_sha256']='wrong'
        variants[2]['fetch']['bytes_read']=probe.MAX_PREFIX+1
        for value in variants:
            with self.subTest(value=value),patch.object(probe.subprocess,'Popen',return_value=fake_process(json.dumps(value).encode())):
                self.assertEqual(probe.measure(url),{'known':False,'reason':'invalid_probe_result'})

    def test_invalid_inputs_and_worker_output_do_not_raise_or_expose_errors(self):
        with patch.object(probe.subprocess,'Popen') as run:
            for url,timeout in ((None,10),('http://a/\nsecret',10),('https://a',0),('https://a',13),('https://a',float('nan'))):
                self.assertEqual(probe.measure(url,timeout)['reason'],'invalid_probe_input')
            run.assert_not_called()
        for data in (b'',b'bad-secret-json',b'x'*32769,b'{"known": false, "reason": "probe_failed"}'):
            with self.subTest(data=data[:30]),patch.object(probe.subprocess,'Popen',return_value=fake_process(data)):
                self.assertFalse(probe.measure('https://example.test/a')['known'])

    def test_public_url_rejects_private_mixed_dns_and_credentials(self):
        for url in ('https://127.0.0.1/a','https://[::1]/a','https://10.0.0.1/a','https://user:pass@example.test/a',
                    'https://example.test:1234/a','https://example.test/a#fragment','https://example.test/\t'):
            with self.subTest(url=url),self.assertRaises(ValueError):probe.public_url(url)
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(probe.socket,'getaddrinfo',return_value=rows),self.assertRaises(ValueError):probe.public_url('https://example.test/a')
        with patch.object(probe.socket,'getaddrinfo',return_value=rows[:1]):probe.public_url('https://example.test/a')

    def test_dns_is_pinned_for_current_host_and_restored_on_error(self):
        original=probe.socket.getaddrinfo
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
        with self.assertRaises(RuntimeError):
            with probe.pinned_dns('https://example.test/a',rows):
                self.assertEqual(probe.socket.getaddrinfo('example.test',443,type=socket.SOCK_STREAM),rows)
                with self.assertRaises(ValueError):probe.socket.getaddrinfo('127.0.0.1',443)
                with self.assertRaises(ValueError):probe.socket.getaddrinfo('example.test',80)
                raise RuntimeError()
        self.assertIs(probe.socket.getaddrinfo,original)

    def test_runtime_fetch_bounded_without_following_redirect_automatically(self):
        class Response:
            status_code=200
            headers={'Content-Type':'audio/mpeg','Content-Length':'1000000'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,chunk_size):
                for _ in range(100):yield b'x'*4096
        from unittest.mock import MagicMock
        session=MagicMock();session.get.return_value=Response()
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
        with patch.object(probe.requests,'Session',return_value=session),patch.object(probe,'public_url',return_value=rows):
            data,info=probe.fetch_prefix('https://example.test/a',max_bytes=4096)
        self.assertEqual(len(data),4096)
        self.assertFalse(info['complete_body_read'])
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertFalse(session.trust_env)

    def test_full_packet_duration_is_sum_not_size_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'candidate.mp3';path.write_bytes(b'x'*50000)
            process=fake_process(b'');process.stdout=io.BytesIO(b'duration_time=0.026122|size=417\n'*100)
            with patch.object(probe.shutil,'which',return_value='/usr/bin/ffprobe'),patch.object(probe.subprocess,'Popen',return_value=process) as popen:
                value=probe.packet_measurement(path)
            self.assertEqual(value['duration_seconds'],2.6122)
            self.assertEqual(value['packet_count'],100)
            self.assertEqual(value['demuxed_audio_bytes'],41700)
            self.assertFalse(value['duration_extrapolated'])
            command=popen.call_args.args[0]
            self.assertEqual(command[command.index('-protocol_whitelist')+1],'file')
            self.assertEqual(command[command.index('-f')+1],'mp3')

    def test_bad_packet_measurement_is_not_accepted(self):
        for line in (b'duration_time=N/A|size=417\n',b'duration_time=2.0|size=417\n',b'duration_time=0.02|size=417|url=secret\n',b'x'*4097):
            with self.subTest(line=line[:40]),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'candidate.mp3';path.write_bytes(b'x'*50000)
                process=fake_process(b'');process.stdout=io.BytesIO(line)
                with patch.object(probe.shutil,'which',return_value='/usr/bin/ffprobe'),patch.object(probe.subprocess,'Popen',return_value=process),self.assertRaises(ValueError):
                    probe.packet_measurement(path)

    def test_full_child_timeout_cleans_parent_owned_directory(self):
        process=fake_process(b'');process.poll.return_value=None
        def stalled(**kwargs):
            payload=json.loads(kwargs['input']);(Path(payload['directory'])/'candidate.mp3').write_bytes(b'partial')
            raise subprocess.TimeoutExpired(['worker'],45)
        process.communicate.side_effect=stalled
        with patch.object(probe.subprocess,'Popen',return_value=process),patch.object(probe.os,'killpg'),patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2*probe.MIN_FREE)):
            value=probe.measure('https://example.test/a',45,full_probe=True)
        self.assertEqual(value,{'known':False,'reason':'probe_timeout'})
        payload=json.loads(process.communicate.call_args.kwargs['input'])
        self.assertTrue(payload['full_probe']);self.assertFalse(Path(payload['directory']).exists())

    def test_private_disk_root_is_used_and_cleaned_on_success_timeout_and_failure(self):
        url='https://example.test/media?private=x'
        for outcome in ('success','timeout','failure'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as outer:
                base=Path(outer).resolve();base.chmod(0o700)
                process=fake_process(json.dumps(self.evidence(url)).encode())
                def communicate(**kwargs):
                    payload=json.loads(kwargs['input']);directory=Path(payload['directory'])
                    self.assertEqual(directory.parent,base)
                    self.assertEqual(directory.stat().st_mode & 0o777,0o700)
                    (directory/'candidate.mp3').write_bytes(b'partial')
                    if outcome=='timeout':raise subprocess.TimeoutExpired(['worker'],45)
                    if outcome=='failure':raise OSError('private details')
                    return json.dumps(self.evidence(url)).encode(),None
                process.communicate.side_effect=communicate
                with patch.object(probe.subprocess,'Popen',return_value=process),patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2*probe.MIN_FREE)):
                    result=probe.measure(url,45,full_probe=True,temp_root=base)
                self.assertEqual(list(base.iterdir()),[])
                self.assertEqual(result.get('reason'),{'success':None,'timeout':'probe_timeout','failure':'probe_process_failed'}[outcome])

    def test_unsafe_temp_roots_fail_before_starting_child(self):
        with tempfile.TemporaryDirectory() as outer,patch.object(probe.subprocess,'Popen') as run:
            base=Path(outer).resolve();private=base/'private';private.mkdir(mode=0o700)
            link=base/'link';link.symlink_to(private,target_is_directory=True)
            shared=base/'shared';shared.mkdir(mode=0o755)
            file=base/'file';file.write_text('data')
            for root in (Path('relative'),base/'missing',link,shared,file):
                with self.subTest(root=root):
                    self.assertEqual(probe.measure('https://example.test/a',temp_root=root),{'known':False,'reason':'probe_storage_unavailable'})
            with patch.object(probe.os,'geteuid',return_value=private.stat().st_uid+1):
                self.assertEqual(probe.measure('https://example.test/a',temp_root=private)['reason'],'probe_storage_unavailable')
            run.assert_not_called()

    def test_private_disk_root_low_space_does_not_leave_child_or_tempfiles(self):
        with tempfile.TemporaryDirectory() as outer,patch.object(probe.subprocess,'Popen') as run,patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=1)):
            base=Path(outer).resolve()
            self.assertEqual(probe.measure('https://example.test/a',45,full_probe=True,temp_root=base)['reason'],'probe_storage_unavailable')
            self.assertEqual(list(base.iterdir()),[]);run.assert_not_called()

    def test_full_download_is_exclusive_bounded_and_hashed(self):
        class Response:
            status_code=200;headers={'Content-Type':'audio/mpeg','Content-Length':'7'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,chunk_size):yield b'ID3data'
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
        session=MagicMock();session.get.return_value=Response()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe.requests,'Session',return_value=session),patch.object(probe,'public_url',return_value=rows),patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2*probe.MIN_FREE)):
                path,info,digest=probe.download_full('https://example.test/a',Path(directory).resolve())
                self.assertEqual(path.read_bytes(),b'ID3data');self.assertEqual(info['actual_bytes'],7)
                self.assertEqual(digest,hashlib.sha256(b'ID3data').hexdigest())
                self.assertEqual(info['prefix_sha256'],digest)
                self.assertEqual(info['prefix_bytes'],7)
                self.assertEqual(info['content_length_header'],7)
                with self.assertRaises(FileExistsError):probe.download_full('https://example.test/a',Path(directory).resolve())

    def test_full_download_refuses_size_overrun_incomplete_and_low_disk(self):
        class Response:
            status_code=200
            def __init__(self,length,body):self.headers={'Content-Type':'audio/mpeg','Content-Length':length};self.body=body
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,chunk_size):yield self.body
        rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
        for response in (Response('999999999',b'x'),Response('',b'x'*9),Response('8',b'x'*7)):
            with self.subTest(response=response),tempfile.TemporaryDirectory() as directory:
                session=MagicMock();session.get.return_value=response
                with patch.object(probe.requests,'Session',return_value=session),patch.object(probe,'public_url',return_value=rows),patch.object(probe,'MAX_MEDIA',8),patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2*probe.MIN_FREE)),self.assertRaises(ValueError):
                    probe.download_full('https://example.test/a',Path(directory).resolve())
        with tempfile.TemporaryDirectory() as directory,patch.object(probe.requests,'Session') as session,patch.object(probe.shutil,'disk_usage',return_value=types.SimpleNamespace(free=1)),self.assertRaises(ValueError):
            probe.download_full('https://example.test/a',Path(directory).resolve())
        session.assert_not_called()

    def test_demux_stderr_refuses_result_without_writing_disk_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'candidate.mp3';path.write_bytes(b'x'*50000)
            process=fake_process(b'');process.stdout=io.BytesIO(b'duration_time=0.026122|size=417\n'*100);process.stderr=io.BytesIO(b'decode error')
            with patch.object(probe.shutil,'which',return_value='/usr/bin/ffprobe'),patch.object(probe.subprocess,'Popen',return_value=process) as popen,self.assertRaises(ValueError):
                probe.packet_measurement(path)
            self.assertIs(popen.call_args.kwargs['stderr'],subprocess.PIPE)
            self.assertEqual([p.name for p in Path(directory).iterdir()],['candidate.mp3'])

    def test_full_evidence_validates_and_requires_full_byte_receipt(self):
        url='https://example.test/media?private=x'
        header={'known':True,'source':'full_mp3_packets','duration_seconds':4000,'packet_count':150000,
                'full_demux_completed':True,'duration_extrapolated':False,'full_decode_checked':False}
        fetch={'bytes_read':50000000,'actual_bytes':50000000,'full_download':True,'complete_body_read':True}
        with patch.object(probe,'download_full',return_value=(Path('/tmp/candidate.mp3'),fetch,'a'*64)),patch.object(probe,'packet_measurement',return_value=header):
            result=probe.worker_result(url,full_probe=True,directory=Path('/tmp'))
        self.assertTrue(probe.valid_result(result,url))
        result['fetch']['actual_bytes']-=1
        self.assertFalse(probe.valid_result(result,url))


class CacheEvidenceTests(unittest.TestCase):
    url='https://example.test/media?private=x'

    def setup_evidence(self):
        prefix=b'x'*probe.MAX_PREFIX;prefix_hash=hashlib.sha256(prefix).hexdigest()
        now=dt.datetime.now(dt.timezone.utc)
        etag=probe.etag_evidence('"strong-version-1"')
        source_hash=hashlib.sha256(b'https://cdn.example.test/audio').hexdigest()
        old={'bytes_read':2*probe.MAX_PREFIX,'actual_bytes':2*probe.MAX_PREFIX,'response_total_bytes':2*probe.MAX_PREFIX,
             'content_length_header':2*probe.MAX_PREFIX,'prefix_bytes':probe.MAX_PREFIX,'prefix_sha256':prefix_hash,
             'full_download':True,'complete_body_read':True,'fetched_at':(now-dt.timedelta(seconds=10)).isoformat(),
             'source_url_sha256':source_hash,**etag}
        header={'known':True,'source':'full_mp3_packets','duration_seconds':60,'packet_count':2300,
                'full_demux_completed':True,'duration_extrapolated':False,'full_decode_checked':False}
        with patch.object(probe,'download_full',return_value=(Path('/tmp/candidate.mp3'),old,'a'*64)),patch.object(probe,'packet_measurement',return_value=header):
            proof=probe.worker_result(self.url,full_probe=True,directory=Path('/tmp'))
        fresh={'bytes_read':probe.MAX_PREFIX,'response_total_bytes':2*probe.MAX_PREFIX,'content_length_header':probe.MAX_PREFIX,
               'range_honoured':True,'source_url_sha256':source_hash,'fetched_at':now.isoformat(),**etag}
        return prefix,proof,fresh

    def reuse(self,prefix,proof,fresh):
        with patch.object(probe,'fetch_prefix',return_value=(prefix,fresh)):
            return probe.worker_result(self.url,previous_evidence=proof)

    def test_only_fresh_strong_validator_full_length_prefix_and_source_enable_cache(self):
        prefix,proof,fresh=self.setup_evidence()
        result=self.reuse(prefix,proof,fresh)
        self.assertTrue(result['known'])
        self.assertEqual(result['header']['source'],'validated_full_mp3_cache')
        self.assertEqual(result['duration_seconds'],60)
        self.assertEqual(result['full_evidence'],proof)
        self.assertEqual(result['previous_evidence_sha256'],proof['evidence_sha256'])
        self.assertTrue(probe.valid_result(result,self.url))
        self.assertNotIn('strong-version-1',json.dumps(result))
        self.assertNotIn('private=x',json.dumps(result))

    def test_weak_missing_or_changed_etag_keeps_original_unknown_reason(self):
        for etag in ('W/"strong-version-1"',None,'"changed"','unquoted'):
            prefix,proof,fresh=self.setup_evidence();fresh.update(probe.etag_evidence(etag))
            result=self.reuse(prefix,proof,fresh)
            self.assertFalse(result['known'])
            self.assertEqual(result['reason'],'no_valid_frame_count_header')
            self.assertIn(result['cache_check'],('strong_validator_unavailable','validator_changed'))

    def test_changed_prefix_total_length_or_final_source_refuses_reuse(self):
        for kind in ('prefix','length','declared_length','source'):
            prefix,proof,fresh=self.setup_evidence()
            if kind=='prefix':prefix=b'y'+prefix[1:]
            elif kind=='length':fresh['response_total_bytes']+=1
            elif kind=='declared_length':fresh['content_length_header']+=1
            else:fresh['source_url_sha256']='b'*64
            result=self.reuse(prefix,proof,fresh)
            self.assertFalse(result['known'])
            self.assertNotEqual(result['cache_check'],'verified')

    def test_changed_original_source_or_altered_full_proof_refuses_reuse(self):
        for kind in ('source','duration','unknown_header'):
            prefix,proof,fresh=self.setup_evidence()
            if kind=='source':proof['source_url_sha256']='b'*64
            elif kind=='duration':proof['duration_seconds']+=1
            else:proof['header']=[]
            result=self.reuse(prefix,proof,fresh)
            self.assertFalse(result['known'])
            self.assertEqual(result['cache_check'],'invalid_previous_full_evidence')

    def test_expired_full_proof_and_nonfresh_validation_refuse_cache(self):
        for kind in ('old_full','old_fresh'):
            prefix,proof,fresh=self.setup_evidence()
            if kind=='old_full':proof['fetch']['fetched_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=31)).isoformat()
            else:
                proof['fetch']['fetched_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=2)).isoformat()
                fresh['fetched_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)).isoformat()
            proof['evidence_sha256']=probe.evidence_hash(proof)
            result=self.reuse(prefix,proof,fresh)
            self.assertFalse(result['known'])
            self.assertIn(result['cache_check'],('full_evidence_expired','fresh_validation_expired'))

    def test_cached_result_cannot_change_duration_or_receipt_even_after_rehash(self):
        prefix,proof,fresh=self.setup_evidence();original=self.reuse(prefix,proof,fresh)
        for kind in ('duration','receipt','nested'):
            result=copy.deepcopy(original)
            if kind=='duration':result['duration_seconds']+=1;result['header']['duration_seconds']+=1
            elif kind=='receipt':result['cache_validation']['representation_bytes']+=1
            else:result['full_evidence']=copy.deepcopy(original)
            result['evidence_sha256']=probe.evidence_hash(result)
            self.assertFalse(probe.valid_result(result,self.url))

    def test_etag_parser_never_accepts_weak_malformed_or_control_values(self):
        for value in ('W/"tag"','w/"tag"','unquoted','"sp ace"','"bad\n"','"quoted"inside"',None):
            self.assertIsNone(probe.etag_evidence(value)['strong_etag_sha256'])
        self.assertEqual(probe.etag_evidence('""')['etag_kind'],'strong')

    def test_measure_refuses_a_structurally_valid_but_stale_cache_receipt(self):
        prefix,proof,fresh=self.setup_evidence();result=self.reuse(prefix,proof,fresh)
        now=dt.datetime.now(dt.timezone.utc)
        proof['fetch']['fetched_at']=(now-dt.timedelta(seconds=100)).isoformat()
        proof['evidence_sha256']=probe.evidence_hash(proof)
        fresh['fetched_at']=(now-dt.timedelta(seconds=70)).isoformat()
        outcome,receipt=probe.cache_gate(proof,self.url,fresh,hashlib.sha256(prefix).hexdigest(),require_fresh=False)
        self.assertEqual(outcome,'verified')
        result.update(full_evidence=proof,fetch=fresh,previous_evidence_sha256=proof['evidence_sha256'],cache_validation=receipt)
        result['evidence_sha256']=probe.evidence_hash(result)
        self.assertTrue(probe.valid_result(result,self.url))
        with patch.object(probe.subprocess,'Popen',return_value=fake_process(json.dumps(result).encode())):
            self.assertEqual(probe.measure(self.url,previous_evidence=proof),{'known':False,'reason':'stale_cached_probe_result'})


if __name__=='__main__':unittest.main()
