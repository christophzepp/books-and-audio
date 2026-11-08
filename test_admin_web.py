import contextlib
import http.cookies
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import admin_web as web
ID='00000000-0000-4000-8000-000000000043'
class Store:
    def __init__(self): self.calls=[]; self.fail=False
    def status(self):
        if self.fail: raise ValueError('token=never-display-this')
        return {'paused':False,'summary':{'mapped':55}}
    def list_connections(self): return {'revision':3,'profiles':[],'schemas':{}}
    def list_folders(self): return {'revision':2,'rows':[],'access_roots':[]}
    def events(self): return {'events':[]}
    def command(self,id): return {'id':id,'status':'success','result':{'message':'Erledigt'}}
    def submit(self,kind,payload): self.calls.append((kind,payload)); return {'id':ID,'status':'queued','must_not_echo':'never-display-this'}
class Socket:
    def __init__(self,raw): self.input=io.BytesIO(raw); self.output=bytearray()
    def makefile(self,*args): return self.input
    def sendall(self,raw): self.output.extend(raw)
    def settimeout(self,value): pass
class WebTests(unittest.TestCase):
    def test_folders_require_auth_and_mutations_require_csrf(self):
        self.assertEqual(self.request('GET','/api/folders')[0],401)
        self.bootstrap()
        self.assertEqual(json.loads(self.request('GET','/api/folders')[1])['revision'],2)
        for kind in ('folder.create', 'provider.activate'):
            self.assertEqual(self.request('POST','/api/commands',{'kind':kind,'payload':{}},csrf=False)[0],403)
            self.assertEqual(self.request('POST','/api/commands',{'kind':kind,'payload':{'expected_revision':0}})[0],202)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve(); self.auth=web.Auth(self.root); self.store=Store(); self.jar={}
        self.server=types.SimpleNamespace(auth=self.auth,store=self.store,cookie_secure=False,allowed=web.allowed_hosts('127.0.0.1,100.64.0.2,192.0.2.42:8790'),static=Path(web.__file__).parent/'admin_static')
        self.clock=[1000.0]; self.auth.clock=lambda:self.clock[0]
    def tearDown(self): self.tmp.cleanup()
    def request(self,method,path,payload=None,*,origin=True,csrf=True,host='127.0.0.1:8790',extra=(),raw=None):
        if raw is None: raw=web.json_bytes(payload) if payload is not None else b''
        headers=[('Host',host),('Content-Length',str(len(raw)))]
        if self.jar: headers.append(('Cookie','; '.join(k+'='+v for k,v in self.jar.items())))
        if method=='POST':
            headers.append(('Content-Type','application/json'))
            if origin: headers.append(('Origin',('http://'+host) if origin is True else origin))
            if csrf: headers.append(('X-CSRF-Token',self.jar.get(web.CSRF_COOKIE,'')))
        headers.extend(extra)
        data=(method+' '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers)+'\r\n').encode()+raw
        socket=Socket(data)
        with contextlib.redirect_stderr(io.StringIO()) as log: web.AdminHandler(socket,('127.0.0.1',1000),self.server)
        self.assertEqual(log.getvalue(),'')
        head,body=bytes(socket.output).split(b'\r\n\r\n',1); lines=head.decode().split('\r\n'); code=int(lines[0].split()[1]); response=[]
        for line in lines[1:]:
            key,value=line.split(':',1); response.append((key.lower(),value.strip()))
            if key.lower()=='set-cookie':
                cookies=http.cookies.SimpleCookie(); cookies.load(value)
                for name,item in cookies.items():
                    if item['max-age']=='0': self.jar.pop(name,None)
                    else: self.jar[name]=item.value
        return code,body,response
    def bootstrap(self):
        code,body,_=self.request('GET','/api/session'); self.assertEqual(code,200); self.assertEqual(json.loads(body),{'initialized':False,'authenticated':False})
        token=self.auth.setup_file.read_text().strip()
        code,body,headers=self.request('POST','/api/setup',{'token':token,'username':'Testuser','password':'valid-password-long','confirmation':'valid-password-long'})
        self.assertEqual(code,200); self.assertEqual(json.loads(body),{'ok':True}); return headers
    def test_bootstrap_private_hash_only_and_token_invalidated(self):
        headers=self.bootstrap(); self.assertFalse(self.auth.setup_file.exists()); raw=self.auth.credentials.read_text(); self.assertNotIn('valid-password-long',raw)
        self.assertEqual(len(json.loads(raw)['hash']),128); self.assertEqual(self.auth.credentials.stat().st_mode & 0o077,0); self.assertEqual(self.auth.directory.stat().st_mode & 0o077,0)
        self.assertTrue(any('sync_admin=' in value and 'HttpOnly' in value and 'SameSite=Strict' in value for key,value in headers if key=='set-cookie'))
    def test_login_logout_and_rotation(self):
        self.bootstrap(); old=self.jar[web.COOKIE]; self.assertEqual(self.request('GET','/api/status')[0],200)
        self.assertEqual(self.request('POST','/api/logout',{})[0],200); self.assertEqual(self.request('GET','/api/status')[0],401); self.request('GET','/api/session')
        self.assertEqual(self.request('POST','/api/login',{'username':'testuser','password':'valid-password-long'})[0],200)
        self.assertNotEqual(old,self.jar[web.COOKIE]); self.assertIsNone(self.auth.session(old))
    def test_setup_token_not_exposed(self):
        _,body,_=self.request('GET','/api/session'); token=self.auth.setup_file.read_text().strip(); self.assertNotIn(token.encode(),body)
        code,body,_=self.request('POST','/api/setup',{'token':'wrong','username':'user','password':'012345678901','confirmation':'012345678901'})
        self.assertEqual(code,401); self.assertNotIn(token.encode(),body)
    def test_password_confirmation_and_minimum(self):
        self.request('GET','/api/session'); token=self.auth.setup_file.read_text().strip()
        for password,confirmation in [('short','short'),('long-password-here','wrong')]:
            self.assertEqual(self.request('POST','/api/setup',{'token':token,'username':'user','password':password,'confirmation':confirmation})[0],400)
        self.assertFalse(self.auth.credentials.exists())
    def test_second_setup_rejected(self):
        self.bootstrap(); self.assertEqual(self.request('POST','/api/setup',{'token':'x','username':'user','password':'valid-password-long','confirmation':'valid-password-long'})[0],409)
    def test_restart_no_session_or_setup(self):
        self.bootstrap(); old=self.jar[web.COOKIE]; restarted=web.Auth(self.root)
        self.assertTrue(restarted.initialized); self.assertIsNone(restarted.session(old)); self.assertFalse(restarted.setup_file.exists())
    def test_crash_after_auth_commit_invalidates_setup(self):
        self.bootstrap(); self.auth.setup_file.write_text('A'*43+'\n'); os.chmod(self.auth.setup_file,0o600)
        restarted=web.Auth(self.root); self.assertTrue(restarted.initialized); self.assertFalse(restarted.setup_file.exists())
    def test_permissions_fail_closed(self):
        self.bootstrap(); os.chmod(self.auth.credentials,0o644)
        with self.assertRaises(ValueError): web.Auth(self.root)
        os.chmod(self.auth.credentials,0o600); os.chmod(self.auth.directory,0o755)
        with self.assertRaises(ValueError): web.Auth(self.root)
    def test_symlink_auth_rejected(self):
        self.bootstrap(); actual=self.root/'actual'; self.auth.credentials.rename(actual); self.auth.credentials.symlink_to(actual)
        with self.assertRaises(OSError): web.Auth(self.root)
    def test_scrypt_matches_record(self):
        import base64
        self.bootstrap(); record=json.loads(self.auth.credentials.read_text())
        self.assertEqual(record['hash'],web.Auth.password_hash('valid-password-long',base64.b64decode(record['salt'])))
        self.assertNotEqual(record['hash'],web.Auth.password_hash('other-password-long',base64.b64decode(record['salt'])))
    def test_rate_limit_expires(self):
        self.bootstrap(); self.request('POST','/api/logout',{}); self.request('GET','/api/session')
        for _ in range(5): self.assertEqual(self.request('POST','/api/login',{'username':'testuser','password':'wrong-password'})[0],401)
        self.assertEqual(self.request('POST','/api/login',{'username':'testuser','password':'valid-password-long'})[0],429)
        self.clock[0]+=601; self.assertEqual(self.request('POST','/api/login',{'username':'testuser','password':'valid-password-long'})[0],200)
    def test_session_and_nonce_expiry(self):
        self.request('GET','/api/session'); self.clock[0]+=web.CSRF_SECONDS+1
        self.assertEqual(self.request('POST','/api/login',{'username':'a','password':'b'})[0],403)
        self.bootstrap(); self.clock[0]+=web.SESSION_SECONDS+1; self.assertEqual(self.request('GET','/api/status')[0],401)
    def test_origin_host_csrf_required(self):
        self.bootstrap(); command={'kind':'sync.pause','payload':{}}
        for kwargs in [{'origin':False},{'origin':'https://attacker.invalid'},{'csrf':False},{'host':'attacker.invalid'}]:
            self.assertEqual(self.request('POST','/api/commands',command,**kwargs)[0],403)
        self.assertEqual(self.store.calls,[])
    def test_csrf_bound_to_session(self):
        self.bootstrap(); self.jar[web.CSRF_COOKIE]='A'*43
        self.assertEqual(self.request('POST','/api/commands',{'kind':'sync.pause','payload':{}})[0],403)
    def test_login_csrf_and_origin(self):
        self.request('GET','/api/session')
        for kwargs in [{'origin':False},{'csrf':False}]: self.assertEqual(self.request('POST','/api/login',{'username':'a','password':'b'},**kwargs)[0],403)
    def test_health_only_ok_and_host_guarded(self):
        code,body,_=self.request('GET','/health'); self.assertEqual(code,200); self.assertEqual(json.loads(body),{'ok':True})
        self.assertEqual(self.request('GET','/health',host='public.invalid')[0],403)
    def test_private_api_requires_login(self):
        for path in ['/api/status','/api/connections','/api/events','/api/commands/'+ID]: self.assertEqual(self.request('GET',path)[0],401)
    def test_queued_response_never_echoes_input(self):
        self.bootstrap(); self.assertEqual(self.request('GET','/api/connections')[0],200)
        body={'kind':'connection.create','payload':{'expected_revision':3,'kind':'audiobookshelf','name':'Main','settings':{},'secrets':{'token':'never-display-this'}}}
        code,raw,_=self.request('POST','/api/commands',body)
        self.assertEqual(code,202); self.assertEqual(json.loads(raw),{'id':ID,'status':'queued'}); self.assertNotIn(b'never-display-this',raw)
        self.assertEqual(self.store.calls[0][1]['secrets']['token'],'never-display-this')
    def test_store_exception_sanitized(self):
        self.bootstrap(); self.store.fail=True; code,body,_=self.request('GET','/api/status')
        self.assertEqual(code,503); self.assertNotIn(b'never-display-this',body)
    def test_command_schema_revision_and_approval(self):
        self.bootstrap()
        cases=[{'kind':'unknown','payload':{}},{'kind':'connection.update','payload':{}},{'kind':'connection.update','payload':{'expected_revision':True}},{'kind':'review.approve','payload':{'review_id':ID,'digest':'a'*64,'confirm':False}},{'kind':'review.approve','payload':{'review_id':ID,'digest':'wrong','confirm':True}}]
        for body in cases: self.assertEqual(self.request('POST','/api/commands',body)[0],400)
        self.assertEqual(self.request('POST','/api/commands',{'kind':'review.approve','payload':{'review_id':ID,'digest':'a'*64,'confirm':True}})[0],202)
    def test_ebook_retry_uses_existing_auth_csrf_and_strict_target_schema(self):
        self.bootstrap()
        command={'kind':'ebook.retry','payload':{'file_id':7}}
        self.assertEqual(self.request('POST','/api/commands',command,csrf=False)[0],403)
        self.assertEqual(self.request('POST','/api/commands',command,origin=False)[0],403)
        self.assertEqual(self.request('POST','/api/commands',command)[0],202)
        self.assertEqual(self.request('POST','/api/commands',{'kind':'ebook.retry','payload':{}})[0],202)
        for value in (True,0,-1,'7',None):
            self.assertEqual(self.request('POST','/api/commands',{'kind':'ebook.retry','payload':{'file_id':value}})[0],400)
        self.assertEqual(self.request('POST','/api/commands',{'kind':'ebook.retry','payload':{'path':'/untrusted'}})[0],400)
    def test_static_csp_nosniff_and_no_store(self):
        for path in ['/','/app.js','/style.css']:
            code,_,headers=self.request('GET',path); self.assertEqual(code,200); headers=dict(headers)
            self.assertIn("default-src 'self'",headers['content-security-policy']); self.assertNotIn('unsafe-inline',headers['content-security-policy']); self.assertEqual(headers['cache-control'],'no-store'); self.assertEqual(headers['x-content-type-options'],'nosniff')
        self.assertEqual(self.request('GET','/%2e%2e/credentials.json')[0],404)
    def test_duplicate_json_and_nonfinite(self):
        self.bootstrap()
        for raw in [b'{"kind":"sync.pause","kind":"sync.resume","payload":{}}',b'{"kind":NaN}']:
            self.assertEqual(self.request('POST','/api/commands',raw=raw)[0],400)
    def test_duplicate_headers_rejected(self):
        self.bootstrap(); body={'kind':'sync.pause','payload':{}}
        for extra in [[('Host','127.0.0.1')],[('Origin','http://127.0.0.1:8790')],[('X-CSRF-Token',self.jar[web.CSRF_COOKIE])]]:
            self.assertEqual(self.request('POST','/api/commands',body,extra=extra)[0],403)
    def test_duplicate_length_and_oversize(self):
        self.bootstrap(); self.assertEqual(self.request('POST','/api/commands',{'kind':'sync.pause','payload':{}},extra=[('Content-Length','1')])[0],413)
        self.assertEqual(self.request('POST','/api/commands',raw=b'a'*(web.BODY_LIMIT+1))[0],413)
    def test_static_no_user_data_or_dom_html_injection(self):
        _,body,_=self.request('GET','/'); html=body.decode(); self.assertNotIn('valid-password-long',html); self.assertNotIn('<script>',html); self.assertNotIn('onclick=',html)
        app=(self.server.static/'app.js').read_text()
        for word in ['innerHTML','eval(','localStorage']: self.assertNotIn(word,app)
class MainStartupTests(unittest.TestCase):
    def test_normal_main_starts_and_closes_http_server(self):
        server=MagicMock()
        store=MagicMock()
        module=types.ModuleType('admin_store'); module.AdminStore=store
        env={'ADMIN_STATE_DIR':'/fixture-state','ADMIN_KEY_FILE':'/fixture-key','ADMIN_PORT':'18790','ADMIN_HOST':'127.0.0.1','ADMIN_ALLOWED_HOSTS':'127.0.0.1','ADMIN_COOKIE_SECURE':'0'}
        with patch.dict(os.environ,env), patch('sys.argv',['admin_web.py']), patch.dict('sys.modules',{'admin_store':module}), patch.object(web,'Auth') as auth, patch.object(web.http.server,'ThreadingHTTPServer',return_value=server) as listener, patch.object(web.signal,'signal'), patch('builtins.print'):
            self.assertEqual(web.main(),0)
        auth.assert_called_once_with('/fixture-state')
        store.assert_called_once_with('/fixture-state','/fixture-key')
        listener.assert_called_once_with(('127.0.0.1',18790),web.AdminHandler)
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertEqual(server.allowed,{('127.0.0.1',None)})
    def test_health_main_only_checks_local_http(self):
        connection=MagicMock(); response=connection.getresponse.return_value
        response.status=200;response.read.return_value=b'{"ok":true}'
        with patch.dict(os.environ,{'ADMIN_PORT':'18790'}), patch('sys.argv',['admin_web.py','--health']), patch('http.client.HTTPConnection',return_value=connection) as client, patch.object(web,'Auth') as auth, patch.object(web.http.server,'ThreadingHTTPServer') as listener:
            self.assertEqual(web.main(),0)
        client.assert_called_once_with('127.0.0.1',18790,timeout=5)
        connection.request.assert_called_once_with('GET','/health')
        connection.close.assert_called_once_with()
        auth.assert_not_called();listener.assert_not_called()
    def test_health_failure_returns_nonzero_without_starting_listener(self):
        with patch('sys.argv',['admin_web.py','--health']), patch('http.client.HTTPConnection',side_effect=OSError('fixture unavailable')), patch.object(web.http.server,'ThreadingHTTPServer') as listener:
            self.assertEqual(web.main(),1)
        listener.assert_not_called()

if __name__=='__main__': unittest.main()
