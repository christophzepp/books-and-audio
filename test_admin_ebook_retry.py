"""Manual import checks use the queue without granting progress permissions."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from admin_runtime import Administration, public_ebook_imports
import test_admin_runtime as fixtures


class EbookRetryAdminTests(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.admin=Administration(self.root,self.key,self.config,self.credentials,fixtures.CandidateBridge)
        self.bridge=SimpleNamespace(config=dict(mapping_provider='bookorbit',
            jellyfin=dict(auto_ebooks=dict(enabled=True))))

    def command(self,payload,bridge=None):
        queued=self.admin.store.submit('ebook.retry',payload)
        self.assertTrue(self.admin.process_one(bridge or self.bridge))
        return self.admin.store.command(queued['id'])

    def test_targeted_manual_retry_is_allowed_while_paused_without_progress_or_pause_changes(self):
        before=self.saved_state();self.admin.store.pause(True)
        with patch('ebook_discovery.refresh',return_value=([],dict(added=0,waiting=0,blocked=[]))) as refresh:
            result=self.command({'file_id':7})
        self.assertEqual(result['status'],'success')
        refresh.assert_called_once_with(self.bridge,True,force=True,retry_file_ids=[7])
        self.assertTrue(self.admin.store.paused())
        self.assertEqual(self.saved_state(),before)

    def test_explicit_all_import_retry_returns_only_counts_and_fixed_message(self):
        with patch('ebook_discovery.refresh',return_value=([],dict(added=2,waiting=1,
            blocked=[dict(reason='PRIVATE-TRANSPORT-TOKEN',url='https://example.test/private')]))) as refresh:
            result=self.command({})
        refresh.assert_called_once_with(self.bridge,True,force=True,retry_file_ids=None)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['result']['blocked'],1)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('example.test',json.dumps(result))

    def test_invalid_selection_never_reaches_discovery(self):
        with patch('ebook_discovery.refresh') as refresh:
            for value in (True,0,-1,'7',None):
                with self.subTest(value=value):self.assertEqual(self.command({'file_id':value})['status'],'failed')
            self.assertEqual(self.command({'file_id':7,'path':'/untrusted'})['status'],'failed')
        refresh.assert_not_called()

    def test_inactive_or_other_provider_does_not_run_a_retry(self):
        with patch('ebook_discovery.refresh') as refresh:
            self.bridge.config['mapping_provider']='bookbridge'
            self.assertEqual(self.command({})['status'],'failed')
            self.bridge.config['mapping_provider']='bookorbit'
            self.bridge.config['jellyfin']['auto_ebooks']['enabled']=False
            self.assertEqual(self.command({})['status'],'failed')
        refresh.assert_not_called()

    def test_failed_retry_never_leaks_transport_detail_or_resumes_sync(self):
        self.admin.store.pause(True);before=self.saved_state()
        with patch('ebook_discovery.refresh',side_effect=RuntimeError('PRIVATE URL password token')):
            result=self.command({'file_id':7})
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertTrue(self.admin.store.paused());self.assertEqual(self.saved_state(),before)


class EbookRetryPublicTests(unittest.TestCase):
    def test_only_safe_identifiers_and_diagnostics_are_public(self):
        source=dict(enabled=True,blocked=[dict(title='A book',file_id=7,book_id=8,
            code='epub_content_rejected',retry_mode='manual_or_source_change',
            reason='PRIVATE-TOKEN https://example.test/feed?password=secret',
            binding='PRIVATE-BINDING',retry_at=1234)])
        before=copy.deepcopy(source);value=public_ebook_imports(source)
        self.assertEqual(source,before)
        row=value['blocked'][0]
        self.assertEqual(row['file_id'],7);self.assertEqual(row['book_id'],8)
        self.assertEqual(row['retry_mode'],'manual_or_source_change')
        self.assertEqual(row['retry_at'],1234)
        self.assertNotIn('PRIVATE',json.dumps(value));self.assertNotIn('example.test',json.dumps(value))

    def test_malformed_optional_diagnostics_are_not_publish_failures(self):
        self.assertEqual(public_ebook_imports(None),dict(enabled=False,blocked=[]))
        self.assertEqual(public_ebook_imports({'blocked':None})['blocked'],[])
        value=public_ebook_imports({'blocked':[None,dict(file_id=True,book_id='7',retry_at=float('nan'),code='PRIVATE')]})
        self.assertEqual(len(value['blocked']),1)
        self.assertNotIn('file_id',value['blocked'][0]);self.assertNotIn('book_id',value['blocked'][0])
        self.assertNotIn('retry_at',value['blocked'][0]);self.assertNotIn('PRIVATE',json.dumps(value))


if __name__=='__main__':unittest.main()
