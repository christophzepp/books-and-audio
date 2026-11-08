"""Independent podcast media trust, folder selection, and scoped failures."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from folder_manager import FolderManager, default_rows
import watch_deployment as watcher

IMAGE='sha256:'+'a'*64


def config():
    return dict(podcasts=dict(enabled=True,abs_root='/podcasts',jellyfin_root='/podcasts',worker_root='/podcasts'),
        podcast_deployment={role:dict(container=name,image=IMAGE,podcast_root='/podcasts',media_source='/srv/example-media/podcasts')
            for role,name in (('abs','audiobookshelf'),('jf','jellyfin'),('worker','book-progress-sync'))})


def containers():
    return [dict(Name='/'+row['container'],Image=IMAGE,State=dict(Status='running'),
        Mounts=[dict(Type='bind',Source='/srv/example-media/podcasts',Destination='/podcasts',RW=role=='abs'),
                dict(Type='bind',Source='/srv/example-stacks/'+row['container'],Destination='/config',RW=True)])
        for role,row in config()['podcast_deployment'].items()]


class PodcastDeploymentTests(unittest.TestCase):
    def test_complete_podcast_gate_does_not_require_bookorbit_or_bookbridge(self):
        receipt=watcher.validate_podcast_containers(containers(),config())
        self.assertEqual(set(receipt),{'abs','jf','worker'})
        self.assertTrue(receipt['abs']['rw']);self.assertFalse(receipt['jf']['rw']);self.assertFalse(receipt['worker']['rw'])
        self.assertNotIn('/opt',json.dumps(receipt))

    def test_explicit_pinned_policy_required(self):
        for cfg in ({},dict(podcast_deployment={}),dict(podcast_deployment=config()['podcast_deployment']|{'bo':{}})):
            with self.subTest(cfg=cfg),self.assertRaises(watcher.DeploymentError):watcher.podcast_deployment_policy(cfg)
        cfg=config();cfg['podcast_deployment']['jf']['image']='jellyfin:latest'
        with self.assertRaises(watcher.DeploymentError):watcher.podcast_deployment_policy(cfg)

    def test_distinct_host_roots_refused(self):
        cfg=config();cfg['podcast_deployment']['jf']['media_source']='/mnt/other'
        with self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(containers(),cfg)

    def test_noncanonical_policies_refused(self):
        for value in ('/', 'relative', '/podcasts/../config', '/podcasts/', '/podcasts//nested'):
            cfg=config();cfg['podcast_deployment']['worker']['podcast_root']=value
            with self.subTest(value=value),self.assertRaises(watcher.DeploymentError):watcher.podcast_deployment_policy(cfg)

    def test_parent_bind_projects_exact_podcast_subdirectory(self):
        rows=containers();rows[1]['Mounts'][0].update(Source='/srv/example-media',Destination='/data')
        cfg=config();cfg['podcast_deployment']['jf']['podcast_root']='/data/podcasts'
        receipt=watcher.validate_podcast_containers(rows,cfg)
        self.assertEqual(receipt['jf']['destination'],'/data/podcasts');self.assertEqual(receipt['jf']['source'],'/srv/example-media/podcasts')

    def test_unreviewed_source_and_named_volume_refused(self):
        for key,value in (('Source','/mnt/other'),('Type','volume')):
            for index in range(3):
                rows=containers();rows[index]['Mounts'][0][key]=value
                with self.subTest(index=index,key=key),self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_jellyfin_and_worker_must_be_readonly(self):
        for index in (1,2):
            for value in (True,None,'false',0):
                rows=containers();rows[index]['Mounts'][0]['RW']=value
                with self.subTest(index=index,value=value),self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_changed_image_or_stopped_container_refused(self):
        for index in range(3):
            for field,value in (('Image','sha256:'+'f'*64),('State',dict(Status='exited'))):
                rows=containers();rows[index][field]=value
                with self.subTest(index=index,field=field),self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_missing_duplicate_and_extra_containers_refused(self):
        for rows in (containers()[:-1],containers()+[containers()[0]],containers()[1:]+[containers()[1]]):
            with self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_nested_mount_shadow_refused(self):
        for index in range(3):
            rows=containers();rows[index]['Mounts'].append(dict(Type='tmpfs',Destination='/podcasts/Show',RW=True))
            with self.subTest(index=index),self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_duplicate_destinations_refused(self):
        rows=containers();rows[0]['Mounts'].append(copy.deepcopy(rows[0]['Mounts'][0]))
        with self.assertRaises(watcher.DeploymentError):watcher.validate_podcast_containers(rows,config())

    def test_main_podcast_success_is_independent_of_failed_book_checks(self):
        runner=Mock(return_value=SimpleNamespace(stdout=json.dumps(containers())))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir();(root/'state').mkdir();(root/'config/config.json').write_text(json.dumps(config()))
            code=watcher.main(root,runner,clock=lambda:1000);gate=json.loads((root/'state/deployment.json').read_text())
        self.assertFalse(gate['ok']);self.assertTrue(gate['podcasts_ok']);self.assertEqual(code,1)
        self.assertEqual(set(gate['podcast_mounts']),{'abs','jf','worker'})

    def test_optional_pinepods_is_checked_separately(self):
        cfg=config();cfg['podcast_deployment']['pinepods']=dict(container='pinepods',image=IMAGE)
        def inspect(args,**kwargs):
            if args==['docker','inspect','pinepods']:
                return SimpleNamespace(stdout=json.dumps([dict(Name='/pinepods',Image='sha256:'+'f'*64,State=dict(Status='running'))]))
            return SimpleNamespace(stdout=json.dumps(containers()))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir();(root/'state').mkdir();(root/'config/config.json').write_text(json.dumps(cfg))
            watcher.main(root,inspect,clock=lambda:1000);gate=json.loads((root/'state/deployment.json').read_text())
        self.assertTrue(gate['podcasts_ok']);self.assertFalse(gate['pinepods_ok'])
        self.assertIn('image/state',gate['pinepods_reason'])

    def test_pinepods_pin_requires_exact_running_image(self):
        cfg=config();cfg['podcast_deployment']['pinepods']=dict(container='pinepods',image=IMAGE)
        row=dict(Name='/pinepods',Image=IMAGE,State=dict(Status='running'))
        runner=Mock(return_value=SimpleNamespace(stdout=json.dumps([row])))
        self.assertTrue(watcher.check_pinepods_deployment(cfg,runner))
        for field,value in (('Image','other'),('State',dict(Status='exited')),('Name','/other')):
            changed=dict(row);changed[field]=value
            runner.return_value=SimpleNamespace(stdout=json.dumps([changed]))
            with self.subTest(field=field),self.assertRaises(watcher.DeploymentError):watcher.check_pinepods_deployment(cfg,runner)

    def test_failed_main_does_not_reuse_old_receipt_or_process_secrets(self):
        runner=Mock(side_effect=subprocess.CalledProcessError(1,['SECRET'],stderr='PRIVATE'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir();(root/'state').mkdir();(root/'config/config.json').write_text(json.dumps(config()))
            (root/'state/deployment.json').write_text(json.dumps(dict(podcasts_ok=True,podcast_mounts={'stale':True})))
            watcher.main(root,runner,clock=lambda:1000);gate=json.loads((root/'state/deployment.json').read_text())
        self.assertFalse(gate['podcasts_ok']);self.assertNotIn('podcast_mounts',gate)
        self.assertNotIn('PRIVATE',json.dumps(gate));self.assertNotIn('SECRET',json.dumps(gate))


class PodcastAliasTests(unittest.TestCase):
    def setUp(self):
        self.cfg=config();self.gate=dict(checked_at=1000,podcasts_ok=True,podcast_mounts=watcher.validate_podcast_containers(containers(),self.cfg))
    def check(self):return watcher.validate_podcast_aliases(self.cfg,self.gate,now=1000)
    def test_exact_roots_verified(self):self.assertTrue(self.check())
    def test_shared_subfolders_verified(self):
        for field in ('abs_root','worker_root','jellyfin_root'):self.cfg['podcasts'][field]+='/Show'
        self.assertTrue(self.check())
    def test_mismatched_or_outside_alias_refused(self):
        for value in ('/podcasts/other','/etc','/podcasts-other'):
            self.cfg['podcasts']['jellyfin_root']=value
            with self.subTest(value=value),self.assertRaises(watcher.DeploymentError):self.check()
    def test_old_future_or_nonfinite_receipt_refused(self):
        for at in (849,1001,float('nan'),True):
            self.gate['checked_at']=at
            with self.subTest(at=at),self.assertRaises(watcher.DeploymentError):self.check()
    def test_unmounted_or_removed_profile_refused(self):
        self.cfg['podcasts']['folder_error']='missing'
        with self.assertRaises(watcher.DeploymentError):self.check()
        self.cfg['podcasts'].pop('folder_error');self.cfg['folder_mappings']=[]
        with self.assertRaises(watcher.DeploymentError):self.check()
    def test_active_profile_must_match_actual_resolved_host(self):
        self.cfg['folder_mappings']=[dict(kind='podcasts',active=True,paths=dict(server='/srv/example-media/podcasts',worker='/podcasts',abs='/podcasts',jf='/podcasts'))]
        self.assertTrue(self.check())
        self.cfg['folder_mappings'][0]['paths']['server']='/other'
        with self.assertRaises(watcher.DeploymentError):self.check()
    def test_readwrite_jellyfin_receipt_refused(self):
        self.gate['podcast_mounts']['jf']['rw']=True
        with self.assertRaises(watcher.DeploymentError):self.check()


class PodcastFolderManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.pod=self.root/'podcasts';self.pod.mkdir()
        self.config=dict(podcasts=dict(enabled=True,abs_root='/podcasts',worker_root=str(self.pod),jellyfin_root='/podcasts'),
            folder_access_roots=[dict(worker=str(self.pod),server='/srv/example-media/podcasts')],
            folder_mappings=[dict(id='podcasts-default',kind='podcasts',name='Podcasts',active=True,paths=dict(worker=str(self.pod),abs='/podcasts',jf='/podcasts'))])
        self.mount_generation='mount-1'
        self.manager=FolderManager(self.root,self.config,mount_checker=lambda _:True,
            generation_reader=lambda:self.mount_generation);self.manager.seed()
    def test_new_install_defaults_include_configured_podcasts_only(self):
        cfg=dict(self.config);cfg.pop('folder_mappings')
        rows=default_rows(cfg);self.assertEqual(rows[-1]['kind'],'podcasts');self.assertEqual(rows[-1]['paths']['jf'],'/podcasts')
        self.assertEqual(len(default_rows({})),2)
    def test_profile_controls_effective_roots(self):
        cfg=self.manager.effective();self.assertEqual(cfg['podcasts']['worker_root'],str(self.pod))
        self.assertEqual(cfg['podcasts']['abs_root'],'/podcasts');self.assertNotIn('folder_error',cfg['podcasts'])
    def test_missing_profile_clears_fallback_roots(self):
        state,_=self.manager._read();state['rows'][0]['active']=False
        cfg=self.manager._effective(state,self.config)
        self.assertEqual(cfg['podcasts']['folder_error'],'podcast_folder_missing')
        self.assertTrue(cfg['podcasts']['enabled']);self.assertNotIn('worker_root',cfg['podcasts'])
    def test_mount_loss_only_blocks_podcast_scope(self):
        self.manager.mount_checker=lambda _:False
        # A real unmount also changes Linux mountinfo. Model that event on all
        # test hosts instead of depending on whether /proc exists locally.
        self.mount_generation='mount-2'
        cfg=self.manager.effective();self.assertEqual(cfg['podcasts']['folder_error'],'folder_mount_unavailable')
        self.assertNotIn('worker_root',cfg['podcasts']);self.assertIn('folder_mappings',cfg)
    def test_deactivation_requires_candidate_compatibility(self):
        def compatible(old,new):return not new['podcasts'].get('folder_error')
        result=self.manager.mutate('deactivate',dict(expected_revision=0,id='podcasts-default'),compatible)
        self.assertFalse(result['ok']);self.assertTrue(self.manager.public()['rows'][0]['active'])
    def test_subfolder_change_and_roots_are_validated_before_persistence(self):
        sub=self.pod/'Show';sub.mkdir();calls=[]
        def compatible(old,new):calls.append(new);return True
        result=self.manager.mutate('update',dict(expected_revision=0,id='podcasts-default',paths=dict(worker=str(sub),abs='/podcasts/Show',jf='/podcasts/Show')),compatible)
        self.assertTrue(result['ok']);self.assertEqual(calls[0]['podcasts']['worker_root'],str(sub))
        self.assertEqual(self.manager.effective()['folder_mappings'][0]['paths']['server'],'/srv/example-media/podcasts/Show')


class PodcastBookIsolationTests(unittest.TestCase):
    def test_broken_podcast_mount_does_not_remove_valid_book_receipts(self):
        from test_folder_deployment import config as books_config,containers as books_containers
        cfg=books_config();cfg.update(config());cfg['folder_access_roots'].append(dict(worker='/podcasts',server='/srv/example-media/podcasts'))
        rows=books_containers()
        for row in rows:
            row['Mounts'].append(dict(Type='bind',Source='/srv/example-media/podcasts',Destination='/podcasts',RW=row['Name']=='/audiobookshelf'))
            row['Mounts'].append(dict(Type='tmpfs',Destination='/podcasts/Show',RW=True))
        byname={row['Name'][1:]:row for row in rows}
        runner=Mock(side_effect=lambda args,**kwargs:SimpleNamespace(stdout=json.dumps([byname[args[-1]]])))
        receipt,errors=watcher.check_folder_deployment(cfg,runner)
        self.assertFalse(errors);self.assertTrue(receipt['worker']);self.assertNotIn('podcasts',json.dumps(receipt))
    def test_book_alias_gate_ignores_incomplete_podcast_profile(self):
        from test_folder_deployment import FolderAliasGateTests
        setup=FolderAliasGateTests();setup.setUp()
        setup.cfg['folder_mappings'].append(dict(kind='podcasts',active=True,paths=dict(worker='/missing')))
        self.assertTrue(setup.check())


if __name__=='__main__':unittest.main()
