import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from bookbridge_client import BookBridgeClient, RequestError


TOKEN = 't' * 43


def response(data=None, status=200, text='', headers=None):
    result = Mock(status_code=status, text=text, headers=headers or {})
    result.content = (text or json.dumps(data)).encode()
    result.json.return_value = data
    return result


def account(username='reader'):
    return response(text='<input type="text" name="username" value="' + username
                    + '"><script>var t = "' + TOKEN + '";</script>')


def mapping(identity='abs-one', **changes):
    row = {'abs_id': identity, 'audio_source': 'ABS', 'audio_source_id': identity,
           'ebook_filename': 'one.epub', 'original_ebook_filename': None,
           'ebook_source': 'Local', 'ebook_source_id': None, 'kosync_doc_id': 'hash',
           'status': 'active', 'sync_mode': 'audiobook', 'audio_duration': 120,
           'duration': 120, 'display_title': 'One', 'readalong_ready': True,
           'states': {'abs': {'timestamp': 30, 'percentage': 25,
                              'last_updated': 12345, 'xpath': None}}}
    row.update(changes)
    return row


class BookBridgeClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.patcher = patch('bookbridge_client.requests.Session', return_value=self.session)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = BookBridgeClient('http://bb.local:5757', 'reader', 'secret-test')
        self.addCleanup(self.client.close)

    def login_sequence(self, tail):
        self.session.request.side_effect = [
            response({'ok': True, 'version': '7.11.0'}),
            response(status=302, headers={'Location': '/'}), account(), *tail]

    def inventory_sequence(self, rows=None, claims=None):
        rows = [mapping()] if rows is None else rows
        claims = [dict(abs_id=x['abs_id']) for x in rows] if claims is None else claims
        self.login_sequence([response({'mappings': rows}), response({'books': claims, 'total': len(claims)})])

    def test_imports_only_current_user_pairs_and_validates_identity(self):
        self.inventory_sequence()
        result = self.client.inventory()
        self.assertEqual(result[0]['abs_id'], 'abs-one')
        self.assertEqual(result[0]['ebook_path'], 'one.epub')
        self.assertEqual(result[0]['states']['abs']['percentage'], 25)
        self.assertTrue(result[0]['alignment_ready'])
        self.assertFalse(self.session.trust_env)
        calls = self.session.request.call_args_list
        self.assertEqual([x.args[0] for x in calls], ['GET', 'POST', 'GET', 'GET', 'GET'])
        self.assertEqual(calls[1].kwargs['data'], {'username': 'reader', 'password': 'secret-test'})
        for call in calls:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['verify'])
        self.session.cookies.clear.assert_called_once()

    def test_empty_is_valid_complete_inventory(self):
        self.inventory_sequence([])
        self.assertEqual(self.client.mappings(), [])

    def test_missing_claim_and_changing_inventory_are_not_empty_success(self):
        self.inventory_sequence(claims=[])
        with self.assertRaisesRegex(RequestError, 'incomplete'):
            self.client.inventory()

    def test_duplicate_ids_and_missing_identity_rejected(self):
        for rows in ([mapping(), mapping()], [mapping(abs_id='')], [mapping(abs_id=None)]):
            self.inventory_sequence(rows)
            self.client._authenticated = False
            with self.assertRaises(RequestError):
                self.client.inventory()

    def test_invalid_totals_rejected(self):
        for total in (True, '1', 2, -1):
            self.login_sequence([response({'mappings': [mapping()]}),
                                 response({'books': [{'abs_id': 'abs-one'}], 'total': total})])
            self.client._authenticated = False
            with self.assertRaises(RequestError):
                self.client.inventory()

    def test_non_abs_sources_and_ebook_only_do_not_create_abs_identity(self):
        rows = [mapping('bo:3', audio_source='BookOrbit', audio_source_id='3'),
                mapping('ebook-4', audio_source=None, sync_mode='ebook_only', audio_duration=0, duration=0)]
        self.inventory_sequence(rows)
        self.assertTrue(all(x['abs_id'] is None for x in self.client.inventory()))

    def test_mismatched_abs_source_id_rejected(self):
        self.inventory_sequence([mapping(audio_source_id='different')])
        with self.assertRaisesRegex(RequestError, 'inconsistent ABS'):
            self.client.inventory()

    def test_original_ebook_path_takes_precedence(self):
        self.inventory_sequence([mapping(original_ebook_filename='/library/original.epub')])
        self.assertEqual(self.client.inventory()[0]['ebook_path'], '/library/original.epub')

    def test_numeric_and_schema_failures(self):
        for changes in ({'audio_duration': float('nan')}, {'audio_duration': -1}, {'status': None},
                        {'readalong_ready': 'true'}, {'sync_mode': 'new-mode'}, {'states': []},
                        {'states': {'abs': {'percentage': 101}}},
                        {'states': {'abs': {'percentage': -1}}},
                        {'states': {'abs': {'last_updated': float('inf')}}}):
            self.inventory_sequence([mapping(**changes)])
            self.client._authenticated = False
            with self.assertRaises(RequestError):
                self.client.inventory()

    def test_bad_versions_stop_before_password_sent(self):
        for version in ('7.12.0', 'dev', None):
            self.session.request.reset_mock()
            self.session.request.side_effect = None
            self.session.request.return_value = response({'ok': True, 'version': version})
            with self.assertRaisesRegex(RequestError, 'unsupported server version'):
                self.client.inventory()
            self.session.request.assert_called_once()
            self.assertEqual(self.session.request.call_args.args[0], 'GET')

    def test_login_redirect_is_not_followed(self):
        for location in ('https://foreign.test/', '/setup', '/login', '//foreign.test'):
            self.login_sequence([])
            self.session.request.side_effect = [response({'ok': True, 'version': '7.11.0'}),
                                                 response(status=302, headers={'Location': location})]
            with self.assertRaisesRegex(RequestError, 'login failed'):
                self.client.inventory()

    def test_account_must_match_and_csrf_must_be_unique(self):
        for acct in (account('other'), response(text='<input name="username" value="reader">'),
                     response(text=account().text + '<script>var t = "' + TOKEN + '";</script>')):
            self.login_sequence([])
            self.session.request.side_effect = [response({'ok': True, 'version': '7.11.0'}),
                                                 response(status=302, headers={'Location': '/'}), acct]
            with self.assertRaises(RequestError):
                self.client.inventory()

    def test_probe_checks_saved_abs_account_without_mutating_it(self):
        self.login_sequence([
            response({'mappings': [mapping()]}), response({'books': [{'abs_id': 'abs-one'}], 'total': 1}),
            response(text='<input type="checkbox" name="ABS_ENABLED" checked>'
                          '<input type="checkbox" name="BOOKORBIT_ENABLED">'),
            response({'ok': True, 'message': "Connected as 'abs-reader'"})])
        info = self.client.probe()
        self.assertEqual(info['username'], 'reader')
        self.assertEqual(info['abs_username'], 'abs-reader')
        self.assertTrue(info['abs_enabled'])
        self.assertFalse(info['bookorbit_enabled'])
        last = self.session.request.call_args
        self.assertTrue(last.args[1].endswith('/api/account/test-connection/abs'))
        self.assertEqual(last.kwargs['json'], {})
        self.assertEqual(last.kwargs['headers'], {'X-CSRF-Token': TOKEN})

    def test_globally_disabled_gate_with_hidden_remembered_value_is_off(self):
        self.login_sequence([
            response(text='<input type="checkbox" name="ABS_ENABLED" disabled>'
                          '<input type="hidden" name="ABS_ENABLED" value="on">'
                          '<input type="checkbox" name="BOOKORBIT_ENABLED" disabled>'
                          '<input type="hidden" name="BOOKORBIT_ENABLED" value="on">'),
            response({'ok': True, 'message': "Connected as 'reader'"})])
        info = self.client.integration_status()
        self.assertFalse(info['abs_enabled'])
        self.assertFalse(info['bookorbit_enabled'])

    def test_abs_connection_error_does_not_leak_remote_message(self):
        self.login_sequence([
            response(text='<input type="checkbox" name="ABS_ENABLED" checked>'
                          '<input type="checkbox" name="BOOKORBIT_ENABLED">'),
            response({'ok': False, 'message': 'secret-test in remote traceback'})])
        with self.assertRaises(RequestError) as err:
            self.client.integration_status()
        self.assertNotIn('secret', str(err.exception))

    def test_only_read_get_is_retried_after_unauthorized(self):
        self.client._authenticated = True
        self.session.request.side_effect = [response(status=401),
            response({'ok': True, 'version': '7.11.0'}), response(status=302, headers={'Location': '/'}),
            account(), response({'mappings': []}), response({'books': [], 'total': 0})]
        self.assertEqual(self.client.inventory(), [])
        self.assertEqual(self.session.request.call_count, 6)

    def test_readonly_post_connection_test_is_not_retried(self):
        self.client._authenticated = True
        self.client._csrf = TOKEN
        self.session.request.side_effect = [
            response(text='<input type="checkbox" name="ABS_ENABLED" checked>'
                          '<input type="checkbox" name="BOOKORBIT_ENABLED">'), response(status=401)]
        with self.assertRaises(RequestError):
            self.client.integration_status()
        self.assertEqual(self.session.request.call_count, 2)

    def test_transport_error_and_json_are_sanitized(self):
        self.session.request.side_effect = requests.Timeout('secret-test')
        with self.assertRaises(RequestError) as err:
            self.client.inventory()
        self.assertNotIn('secret-test', str(err.exception))
        self.assertIsNone(err.exception.__cause__)
        bad = response()
        bad.json.side_effect = ValueError('secret-test')
        self.session.request.side_effect = None
        self.session.request.return_value = bad
        with self.assertRaises(RequestError) as err:
            self.client.version()
        self.assertNotIn('secret-test', str(err.exception))

    def test_constructor_rejects_credentials_in_url_and_unsupported_version(self):
        for url in ('http://u:p@bb.local', 'http://bb.local/path', 'http://bb.local?token=x',
                    'file:///etc/passwd', 'http://bb.local:bad', 'http://bb.local\\@other'):
            with self.assertRaises(ValueError):
                BookBridgeClient(url, 'reader', 'pw')
        with self.assertRaises(ValueError):
            BookBridgeClient('http://bb.local', 'reader', 'pw', '7.12.0')

    def test_no_mutation_surface_or_credential_storage(self):
        self.assertFalse(hasattr(self.client, 'write_progress'))
        self.assertFalse(hasattr(self.client, 'create_mapping'))
        self.client.close()
        self.assertEqual(self.client._password, '')
        self.assertIsNone(self.client._csrf)


if __name__ == '__main__':
    unittest.main()
