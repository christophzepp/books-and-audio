import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from bo_client import BOClient, RequestError
from bookorbit_instance import verify_api_instance
from test_bo_client import response
import test_watch_deployment as legacy
import watch_deployment as watcher


def enrollment():
    return dict(repository=watcher.BOOKORBIT_REPOSITORY,compose_project='fixture',compose_service='bookorbit',
                data_source='/srv/bookorbit/data',api_origin='http://bookorbit.test',user_id=1,
                libraries=[{'id':7,'roots':['/audiobooks','/hoerspiele']},{'id':9,'roots':['/ebooks']}])


def configuration():
    cfg=legacy.config(False)
    return dict(cfg,bookorbit_instance=enrollment(),auto_discovery={'bo_library_ids':[7]},
                bo_url=enrollment()['api_origin'],jellyfin={'auto_ebooks':{'enabled':True,'bo_library_id':9}})


def runtime():
    row=legacy.containers(False)[1]
    row.update(Id='1'*64,Image='sha256:'+'8'*64)
    row['State'].update(Health={'Status':'healthy'},StartedAt='2026-11-25T01:00:00.123Z')
    row['Config'].update(Image=watcher.BOOKORBIT_REPOSITORY+':latest',Env=['APP_VERSION=v4.1.2'],
                         Labels={'com.docker.compose.project':'fixture','com.docker.compose.service':'bookorbit'})
    row['Mounts'].append(dict(Type='bind',Source='/srv/bookorbit/data',Destination='/data',RW=True))
    return row


def runner(row=None,digests=None):
    row=row or runtime()
    image=dict(Id=row['Image'],RepoDigests=[watcher.BOOKORBIT_REPOSITORY+'@sha256:'+'7'*64] if digests is None else digests)
    return Mock(side_effect=lambda command,**kwargs:SimpleNamespace(stdout=json.dumps([image if command[1]=='image' else row])))


class HostTests(unittest.TestCase):
    def test_update_of_official_image_preserves_existing_policy_and_checks_other_pins(self):
        cfg=configuration()
        cfg['folder_deployment']={'bo':{'container':'bookorbit','image':'sha256:'+'b'*64}}
        original=copy.deepcopy(cfg)
        expected,proof=watcher.resolve_bookorbit_instance(cfg,runner())
        self.assertEqual(cfg,original)
        self.assertEqual(expected['deployment']['bookorbit']['version'],'v4.1.2')
        self.assertEqual(expected['folder_deployment']['bo']['image'],runtime()['Image'])
        containers=[legacy.containers(False)[0],runtime()]
        watcher.validate_containers(containers,expected)
        containers[0]['Image']='sha256:'+'0'*64
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_containers(containers,expected)
        self.assertNotIn('api_origin',proof)
        self.assertNotIn('user_id',proof)

    def test_reject_foreign_image_mount_health_label_or_process_replacement(self):
        for path,value in [(('Config','Image'),'other/bookorbit:latest'),(('State','Health','Status'),'starting'),
                           (('Config','Labels','com.docker.compose.project'),'other'),
                           (('Mounts',1,'Source'),'/another/data')]:
            container=runtime()
            parent=container
            for key in path[:-1]:
                parent=parent[key]
            parent[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(watcher.DeploymentError):
                watcher.resolve_bookorbit_instance(configuration(),runner(container))
        for bad in ([],['other/bookorbit@sha256:'+'0'*64]):
            with self.assertRaises(watcher.DeploymentError):
                watcher.resolve_bookorbit_instance(configuration(),runner(digests=bad))
        cfg,_=watcher.resolve_bookorbit_instance(configuration(),runner())
        replacement=dict(runtime(),Id='2'*64)
        with self.assertRaisesRegex(watcher.DeploymentError,'changed during'):
            watcher.validate_containers([legacy.containers(False)[0],replacement],cfg)

    def test_failure_does_not_fall_back_or_stop_podcasts_and_unrelated_folder_endpoints(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'config').mkdir()
            cfg=configuration()
            cfg.update(folder_deployment={'bo':{'container':'bookorbit'},'worker':{'container':'worker'}},podcast_deployment={})
            (root/'config/config.json').write_text(json.dumps(cfg))
            with patch.object(watcher,'check_deployment') as check,patch.object(watcher,'check_podcast_deployment',return_value={'abs':'ok'}),patch.object(watcher,'check_folder_deployment',return_value=({'worker':[]},{})) as folder:
                gate=watcher._collect_gate(root,runner(digests=[]),time.time)
            check.assert_not_called()
            self.assertNotIn('bo',folder.call_args.args[0]['folder_deployment'])
            self.assertFalse(gate['ok'])
            self.assertNotIn('bookorbit_instance',gate)
            self.assertTrue(gate['podcasts_ok'])
            self.assertTrue(gate['folder_gate_ok'])
            self.assertIn('bo',gate['folder_errors'])


def fake_client(cfg):
    client=Mock(base_url=cfg['bo_url'],user={'id':1},_authenticated=True)
    client._request.return_value={'id':1}
    client.library.side_effect=lambda i:dict(id=i,folders=[{'id':j+1,'libraryId':i,'path':p} for j,p in enumerate(
        next(row['roots'] for row in cfg['bookorbit_instance']['libraries'] if row['id']==i))])
    return client


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.cfg=configuration()
        self.client=fake_client(self.cfg)
        self.bridge=SimpleNamespace(config=self.cfg,bo=self.client)
        self.gate={'bookorbit_instance':watcher.bookorbit_runtime(runtime(),self.cfg['bookorbit_instance']),
                   'checked_at':100}

    def test_only_one_api_check_per_fresh_receipt_and_after_container_change(self):
        self.assertTrue(verify_api_instance(self.bridge,self.gate))
        self.assertFalse(verify_api_instance(self.bridge,self.gate))
        self.assertEqual(self.client.library.call_count,2)
        self.assertTrue(verify_api_instance(self.bridge,dict(self.gate,checked_at=160)))
        self.assertEqual(self.client.library.call_count,4)
        self.gate['bookorbit_instance']['container_id']='4'*64
        self.assertTrue(verify_api_instance(self.bridge,self.gate))

    def test_each_wrong_account_root_origin_or_library_scope_rejects(self):
        changes=[(lambda:self.client._request.configure_mock(return_value={'id':2})),
                 (lambda:self.client.library.configure_mock(side_effect=lambda i:dict(id=i,folders=[dict(id=1,libraryId=i,path='/wrong')]))),
                 (lambda:setattr(self.client,'base_url','http://other.test')),
                 (lambda:self.cfg['auto_discovery'].update(bo_library_ids=[5]))]
        for change in changes:
            self.setUp()
            change()
            with self.assertRaises(watcher.DeploymentError):
                verify_api_instance(self.bridge,self.gate)

    def test_reauth_to_another_user_never_issues_followup_request(self):
        client=BOClient('http://bookorbit.test','name','test-password')
        self.addCleanup(client.close)
        client.expected_user_id='1'
        with patch.object(client.session,'request',return_value=response(data={'accessToken':'fixture','user':{'id':2}})) as call:
            with self.assertRaisesRegex(RequestError,'different account'):
                client.library(7)
        self.assertEqual(call.call_count,1)
        self.assertNotIn('Authorization',client.session.headers)
