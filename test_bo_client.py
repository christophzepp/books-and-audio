import hashlib
import unittest
from unittest.mock import Mock, patch

import requests

from bo_client import BOClient, RequestError


ASSET = "aud_12345678-1234-4234-8234-123456789abc"
OPERATION = "12345678-1234-4234-8234-123456789def"
WHEN = "2026-10-30T10:00:00.000Z"


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


def stream_response(parts, status=200, headers=None):
    result = response(status)
    result.headers = requests.structures.CaseInsensitiveDict(
        headers if headers is not None else {'Content-Length': str(sum(map(len, parts)))})
    result.iter_content.return_value = iter(parts)
    return result


class BOClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.session.request.return_value = response(data={})
        self.factory = patch("bo_client.requests.Session", return_value=self.session)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.client = BOClient("http://bookorbit.local:3000/", "test-reader", "secret-password")

    def authenticated(self):
        self.client._authenticated = True
        self.session.headers["Authorization"] = "Bearer secret-test-token"

    def position(self):
        return self.client.write_position(1, ASSET, 12345, 2, "a" * 64, OPERATION, WHEN)

    def test_file_sha256_streams_original_without_range_encoding_or_disk(self):
        self.authenticated()
        r = stream_response([b'abc', b'', b'def'])
        self.session.request.return_value = r
        self.assertEqual(self.client.file_sha256(42, 6), hashlib.sha256(b'abcdef').hexdigest())
        call = self.session.request.call_args
        self.assertEqual(call.args, ('GET', 'http://bookorbit.local:3000/api/v1/books/files/42/download'))
        self.assertEqual(call.kwargs, {
            'params': None, 'json': None,
            'headers': {'Accept-Encoding': 'identity', 'Range': None, 'If-Range': None,
                        'If-None-Match': None, 'If-Modified-Since': None},
            'timeout': (5, 30), 'allow_redirects': False, 'verify': True, 'stream': True})
        r.iter_content.assert_called_once_with(chunk_size=1024 * 1024, decode_unicode=False)
        r.json.assert_not_called()
        r.close.assert_called_once()

    def test_file_sha256_removes_inherited_range_and_conditional_headers(self):
        session = requests.sessions.Session()
        session.headers.update({'Range': 'bytes=0-3', 'If-Range': 'old', 'If-None-Match': 'old',
                                'If-Modified-Since': 'old', 'Accept-Encoding': 'gzip'})
        self.authenticated()
        self.session.request.return_value = stream_response([b'file'])
        self.client.file_sha256(1, 4)
        headers = self.session.request.call_args.kwargs['headers']
        prepared = session.prepare_request(requests.Request('GET', 'http://example.test', headers=headers))
        self.assertEqual(prepared.headers['Accept-Encoding'], 'identity')
        for key in ('Range', 'If-Range', 'If-None-Match', 'If-Modified-Since'):
            self.assertNotIn(key, prepared.headers)
        session.close()

    def test_file_sha256_invalid_identifier_or_size_sends_nothing(self):
        for value in (0, -1, True, 1.5, '4', None, 10 * 1024 ** 3 + 1):
            with self.subTest(size=value), self.assertRaises(ValueError):
                self.client.file_sha256(1, value)
        for value in (True, 0, -1, '../secret', '1?token=secret'):
            with self.subTest(identifier=value), self.assertRaises(ValueError):
                self.client.file_sha256(value, 4)
        self.session.request.assert_not_called()

    def test_file_sha256_lazy_login_and_single_401_refresh_before_stream(self):
        first = stream_response([], 401)
        good = stream_response([b'file'])
        self.session.request.side_effect = [response(data={'accessToken': 'initial'}), first,
                                           response(data={'accessToken': 'new-token'}), good]
        self.assertEqual(self.client.file_sha256(1, 4), hashlib.sha256(b'file').hexdigest())
        self.assertEqual([c.args[0] for c in self.session.request.call_args_list], ['POST', 'GET', 'POST', 'GET'])
        first.iter_content.assert_not_called()
        first.close.assert_called_once()
        good.close.assert_called_once()

    def test_file_sha256_second_401_closes_and_stops(self):
        self.authenticated()
        first, second = stream_response([], 401), stream_response([], 401)
        self.session.request.side_effect = [first, response(data={'accessToken': 'new-token'}), second]
        with self.assertRaises(RequestError) as error:
            self.client.file_sha256(1, 4)
        self.assertEqual(error.exception.status, 401)
        self.assertFalse(self.client._authenticated)
        self.assertEqual(self.session.request.call_count, 3)
        for r in (first, second):
            r.close.assert_called_once()
            r.iter_content.assert_not_called()

    def test_file_sha256_failed_reauthentication_closes_first_response(self):
        self.authenticated()
        first = stream_response([], 401)
        self.session.request.side_effect = [first, response(403, {'secret': 'secret'})]
        with self.assertRaises(RequestError) as error:
            self.client.file_sha256(1, 4)
        first.close.assert_called_once()
        self.assertEqual(self.session.request.call_count, 2)
        self.assertNotIn('secret', str(error.exception))

    def test_file_sha256_rejects_partial_redirect_and_server_errors_without_retry(self):
        self.authenticated()
        for status in (206, 301, 302, 307, 304, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                r = stream_response([], status)
                self.session.request.return_value = r
                with self.assertRaises(RequestError) as error:
                    self.client.file_sha256(1, 4)
                self.assertEqual(error.exception.status, status)
                self.session.request.assert_called_once()
                r.close.assert_called_once()
                r.iter_content.assert_not_called()

    def test_file_sha256_requires_exact_full_length_and_unencoded_body(self):
        self.authenticated()
        cases = [{}, {'Content-Length': '3'}, {'Content-Length': '5'},
                 {'Content-Length': '+4'}, {'Content-Length': '٤'}, {'Content-Length': '4,4'},
                 {'Content-Length': '4', 'Content-Range': 'bytes 0-3/4'},
                 {'Content-Length': '4', 'Content-Encoding': 'gzip'},
                 {'Content-Length': '4', 'Content-Encoding': 'identity'},
                 {'Content-Length': '4', 'Transfer-Encoding': 'chunked'}]
        for headers in cases:
            with self.subTest(headers=headers):
                self.session.request.reset_mock()
                r = stream_response([b'file'], headers=headers)
                self.session.request.return_value = r
                with self.assertRaises(RequestError):
                    self.client.file_sha256(1, 4)
                r.close.assert_called_once()
                r.iter_content.assert_not_called()
                self.session.request.assert_called_once()

    def test_file_sha256_body_short_long_and_nonbytes_fail_closed(self):
        self.authenticated()
        for parts in ([b'fil'], [b'file', b'overflow'], ['file']):
            with self.subTest(parts=parts):
                self.session.request.reset_mock()
                r = stream_response(parts, headers={'Content-Length': '4'})
                self.session.request.return_value = r
                with self.assertRaises(RequestError):
                    self.client.file_sha256(1, 4)
                r.close.assert_called_once()
                self.session.request.assert_called_once()

    def test_file_sha256_stops_consuming_immediately_after_overflow(self):
        self.authenticated()
        r = stream_response([], headers={'Content-Length': '4'})
        def oversized():
            yield b'12345'
            self.fail('Overflow response must not be drained')
        r.iter_content.return_value = oversized()
        self.session.request.return_value = r
        with self.assertRaises(RequestError):
            self.client.file_sha256(1, 4)
        r.close.assert_called_once()

    def test_file_sha256_transport_errors_are_sanitized_and_never_retried(self):
        self.authenticated()
        self.session.request.side_effect = requests.Timeout('secret download URL')
        with self.assertRaises(RequestError) as error:
            self.client.file_sha256(1, 4)
        self.assertNotIn('secret', str(error.exception))
        self.session.request.assert_called_once()
        self.session.request.reset_mock()
        self.session.request.side_effect = None
        r = stream_response([], headers={'Content-Length': '4'})
        def broken():
            yield b'fi'
            raise requests.exceptions.ChunkedEncodingError('secret stream body')
        r.iter_content.return_value = broken()
        self.session.request.return_value = r
        with self.assertRaises(RequestError) as error:
            self.client.file_sha256(1, 4)
        self.assertNotIn('secret', str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        r.close.assert_called_once()
        self.session.request.assert_called_once()

    def test_lazy_login_memory_only_and_transport_security(self):
        self.session.request.assert_not_called()
        self.session.request.side_effect = [response(data={"accessToken": "secret-test-token", "user": {"id": 1}}), response(data={"id": 1})]
        self.assertEqual(self.client.detail(1), {"id": 1})
        login, read = self.session.request.call_args_list
        self.assertEqual(login.args, ("POST", "http://bookorbit.local:3000/api/v1/auth/login"))
        self.assertEqual(login.kwargs["json"], {"username": "test-reader", "password": "secret-password"})
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.session.headers["Authorization"], "Bearer secret-test-token")
        self.assertEqual(read.kwargs, {"params": None, "json": None, "timeout": (5, 20), "allow_redirects": False, "verify": True})

    def test_get_401_logs_in_once_then_retries_only_get(self):
        self.authenticated()
        self.session.request.side_effect = [response(401), response(data={"accessToken": "new-token"}), response(data={"id": 1})]
        self.assertEqual(self.client.detail(1), {"id": 1})
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["GET", "POST", "GET"])
        self.assertEqual(self.session.headers["Authorization"], "Bearer new-token")

    def test_second_get_401_does_not_loop(self):
        self.authenticated()
        self.session.request.side_effect = [response(401), response(data={"accessToken": "new-token"}), response(401)]
        with self.assertRaises(RequestError) as error:
            self.client.playback(1)
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(self.session.request.call_count, 3)

    def test_no_implicit_retry_of_writes_or_other_posts(self):
        for operation in (self.position, lambda: self.client.write_status(1, "read"),
                          lambda: self.client._request("POST", "/api/v1/books", "/api/v1/books/query"),
                          lambda: self.client._request("POST", "/api/v1/books/query/", "query")):
            with self.subTest(operation=operation):
                self.authenticated()
                self.session.request.reset_mock()
                self.session.request.return_value = response(401)
                with self.assertRaises(RequestError) as error:
                    operation()
                self.assertEqual(error.exception.status, 401)
                self.session.request.assert_called_once()

    def test_query_401_reauthenticates_once_and_repeats_identical_read(self):
        for status in (200, 201):
            with self.subTest(status=status):
                self.authenticated()
                self.session.request.reset_mock()
                page = {"items": [{"id": 1}], "total": 1, "page": 0, "size": 200}
                self.session.request.side_effect = [response(401), response(data={"accessToken": "new-token"}), response(status, page)]
                self.assertEqual(self.client.query(), [{"id": 1}])
                query, login, repeated = self.session.request.call_args_list
                self.assertEqual(query, repeated)
                self.assertEqual(query.args, ("POST", "http://bookorbit.local:3000/api/v1/books/query"))
                self.assertTrue(login.args[1].endswith("/auth/login"))
                self.assertEqual(self.session.headers["Authorization"], "Bearer new-token")

    def test_second_query_401_does_not_loop(self):
        self.authenticated()
        self.session.request.side_effect = [response(401), response(data={"accessToken": "new-token"}), response(401)]
        with self.assertRaises(RequestError) as error:
            self.client.query()
        self.assertEqual(error.exception.status, 401)
        self.assertFalse(self.client._authenticated)
        self.assertEqual(self.session.request.call_count, 3)

    def test_query_failed_reauthentication_does_not_repeat_search(self):
        self.authenticated()
        self.session.request.side_effect = [response(401), response(401, {"error": "secret-password"})]
        with self.assertRaises(RequestError) as error:
            self.client.query()
        self.assertEqual(error.exception.status, 401)
        self.assertNotIn("secret", str(error.exception))
        self.assertFalse(self.client._authenticated)
        self.assertEqual(self.session.request.call_count, 2)

    def test_query_other_failures_are_not_retried(self):
        self.authenticated()
        for status in (403, 429, 500, 307):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.session.request.return_value = response(status)
                with self.assertRaises(RequestError) as error:
                    self.client.query()
                self.assertEqual(error.exception.status, status)
                self.session.request.assert_called_once()
        self.session.request.reset_mock()
        self.session.request.side_effect = requests.Timeout("secret-password")
        with self.assertRaises(RequestError) as error:
            self.client.query()
        self.assertIsNone(error.exception.status)
        self.assertNotIn("secret", str(error.exception))
        self.session.request.assert_called_once()

    def test_query_expiry_on_second_page_preserves_pagination_and_validation(self):
        first = [{"id": i} for i in range(1, 201)]
        for last_id, last_total, valid in ((201, 201, True), (1, 201, False), (201, 202, False)):
            with self.subTest(last_id=last_id, last_total=last_total):
                self.authenticated()
                self.session.request.reset_mock()
                self.session.request.side_effect = [
                    response(data={"items": first, "total": 201, "page": 0, "size": 200}),
                    response(401), response(data={"accessToken": "new-token"}),
                    response(data={"items": [{"id": last_id}], "total": last_total, "page": 1, "size": 200}),
                ]
                if valid:
                    self.assertEqual(self.client.query(), first + [{"id": 201}])
                else:
                    with self.assertRaises(RequestError):
                        self.client.query()
                calls = self.session.request.call_args_list
                self.assertEqual(len(calls), 4)
                self.assertEqual(calls[1], calls[3])
                self.assertEqual(calls[1].kwargs["json"]["pagination"], {"page": 1, "size": 200})

    def test_position_payload_has_exact_cas_fields_and_no_synthetic_history(self):
        self.authenticated()
        self.position()
        self.assertEqual(self.session.request.call_args.args[0], "PUT")
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "assetId": ASSET, "positionMs": 12345, "capturedAt": WHEN,
            "operationId": OPERATION, "baseRevision": 2, "manifestRevision": "a" * 64,
        })

    def test_conflict_precondition_and_ambiguous_transport_never_retry(self):
        self.authenticated()
        for status in (409, 412, 429, 500, 307):
            self.session.request.reset_mock()
            self.session.request.return_value = response(status, {"error": "secret-password"})
            with self.assertRaises(RequestError) as error:
                self.position()
            self.assertEqual(error.exception.status, status)
            self.assertNotIn("secret", str(error.exception))
            self.session.request.assert_called_once()
        self.session.request.reset_mock()
        self.session.request.side_effect = requests.Timeout("secret-password")
        with self.assertRaises(RequestError) as error:
            self.position()
        self.assertIsNone(error.exception.status)
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.session.request.assert_called_once()

    def test_only_200_json_null_means_missing_playback(self):
        self.authenticated()
        self.session.request.return_value = response(data=None)
        self.assertIsNone(self.client.playback(1))
        for status in (204, 404, 500):
            self.session.request.return_value = response(status, None)
            with self.assertRaises(RequestError) as error:
                self.client.playback(1)
            self.assertEqual(error.exception.status, status)
        self.session.request.return_value = response(data=None)
        with self.assertRaises(RequestError):
            self.client.detail(1)

    def test_invalid_json_is_sanitized_even_after_mutation(self):
        self.authenticated()
        bad = response()
        bad.json.side_effect = ValueError("secret response body")
        self.session.request.return_value = bad
        with self.assertRaises(RequestError) as error:
            self.position()
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.session.request.assert_called_once()

    def test_status_null_is_distinct_from_omitted_and_no_audio_write(self):
        self.authenticated()
        self.client.write_status(1, "reading")
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"status": "reading"})
        self.client.write_status(1, "read", started_at=None, finished_at=None)
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"status": "read", "startedAt": None, "finishedAt": None})
        self.client.write_status(1, "read", started_at="2026-10-01", finished_at=WHEN)
        self.assertEqual(self.session.request.call_args.kwargs["json"]["finishedAt"], WHEN)
        self.assertTrue(all(call.args[0] == "PATCH" and call.args[1].endswith("/status") for call in self.session.request.call_args_list))

    def test_query_complete_pagination_supports_201_and_empty(self):
        self.authenticated()
        first = [{"id": i} for i in range(1, 201)]
        self.session.request.side_effect = [
            response(201, {"items": first, "total": 201, "page": 0, "size": 200}),
            response(200, {"items": [{"id": 201}], "total": 201, "page": 1, "size": 200}),
            response(201, {"items": [], "total": 0, "page": 0, "size": 200}),
        ]
        self.assertEqual(self.client.query(), first + [{"id": 201}])
        self.assertEqual(self.client.query(), [])
        self.assertEqual(self.session.request.call_args_list[1].kwargs["json"]["pagination"], {"page": 1, "size": 200})

    def test_query_rejects_partial_duplicate_changed_or_bad_schema(self):
        self.authenticated()
        for items, total, page, size in (
            ([], 1, 0, 200), ([{"id": 1}, {"id": 1}], 2, 0, 200),
            ([{}], 1, 0, 200), ([], True, 0, 200), ([], 0, 1, 200),
            ([], 0, False, 200), ([], 0, 0, 100), ([{"id": True}], 1, 0, 200),
        ):
            self.session.request.return_value = response(data={"items": items, "total": total, "page": page, "size": size})
            with self.assertRaises(RequestError):
                self.client.query()
        self.session.request.side_effect = [
            response(data={"items": [{"id": i} for i in range(1, 201)], "total": 201, "page": 0, "size": 200}),
            response(data={"items": [{"id": 201}], "total": 202, "page": 1, "size": 200}),
        ]
        with self.assertRaises(RequestError):
            self.client.query()

    def test_invalid_identifiers_dates_and_payloads_send_nothing(self):
        for bad in (0, -1, True, "../secret", "1/secret", "1?token=secret"):
            with self.assertRaises(ValueError):
                self.client.detail(bad)
        for bad in ("finished", None, True):
            with self.assertRaises(ValueError):
                self.client.write_status(1, bad)
        for bad in ("2026-02-30", "secret-date", False, 1, "2026-10-30T10:00:00"):
            with self.assertRaises(ValueError):
                self.client.write_status(1, "read", finished_at=bad)
        for index, bad in ((1, "not-asset"), (2, True), (2, -1), (3, False), (4, "bad"), (5, "not-uuid"), (6, "2026-10-30")):
            args = [1, ASSET, 1, 0, "a" * 64, OPERATION, WHEN]
            args[index] = bad
            with self.assertRaises(ValueError):
                self.client.write_position(*args)
        self.session.request.assert_not_called()

    def test_login_failure_does_not_send_requested_read(self):
        self.session.request.return_value = response(401, {"error": "secret-password"})
        with self.assertRaises(RequestError) as error:
            self.client.detail(1)
        self.assertEqual(error.exception.status, 401)
        self.assertNotIn("secret", str(error.exception))
        self.session.request.assert_called_once()

    def test_bad_login_payload_and_redirects_fail_closed(self):
        for data in ({}, {"accessToken": "token\nsecret"}, [], None):
            self.session.request.return_value = response(data=data)
            with self.assertRaises(RequestError):
                self.client.detail(1)
        for status in (301, 302, 307, 308):
            self.session.request.return_value = response(status)
            with self.assertRaises(RequestError) as error:
                self.client.detail(1)
            self.assertEqual(error.exception.status, status)

    def test_invalid_base_urls_do_not_echo_secrets(self):
        for url in ("ftp://host", "http://user:secret@host", "http://host?secret=x", "http://host:secret", "http://host/path"):
            with self.assertRaises(ValueError) as error:
                BOClient(url, "user", "password")
            self.assertNotIn("secret", str(error.exception))

    def test_read_endpoint_paths_and_close(self):
        self.authenticated()
        self.client.manifest(1)
        self.client.library("2")
        self.assertTrue(self.session.request.call_args_list[0].args[1].endswith("/audiobooks/1/manifest"))
        self.assertTrue(self.session.request.call_args_list[1].args[1].endswith("/libraries/2"))
        self.client.close()
        self.assertNotIn("Authorization", self.session.headers)
        self.assertEqual(self.client._password, "")
        self.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
