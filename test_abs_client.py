import unittest
from unittest.mock import Mock, patch

import requests

from abs_client import ABSClient, RequestError


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class ABSClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.session.request.return_value = response(data={})
        self.factory = patch("abs_client.requests.Session", return_value=self.session)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.client = ABSClient("http://abs.local:8053/", "secret-test-token")

    def test_transport_configuration(self):
        self.client.progress("one")
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.session.headers["Authorization"], "Bearer secret-test-token")
        self.session.request.assert_called_once_with(
            "GET", "http://abs.local:8053/api/me/progress/one",
            params=None, json=None, timeout=(5, 20), allow_redirects=False, verify=True,
        )

    def test_progress_raw_and_only_404_is_missing(self):
        raw = {"currentTime": 123.5, "isFinished": False}
        self.session.request.side_effect = [response(data=raw), response(404), response(500)]
        self.assertIs(self.client.progress("one"), raw)
        self.assertIsNone(self.client.progress("one"))
        with self.assertRaises(RequestError) as error:
            self.client.progress("one")
        self.assertEqual(error.exception.status, 500)

    def test_other_404_is_error(self):
        self.session.request.return_value = response(404)
        for call in (self.client.libraries, self.client.allprogress, lambda: self.client.item("one")):
            with self.assertRaises(RequestError) as error:
                call()
            self.assertEqual(error.exception.status, 404)

    def test_redirects_fail_closed(self):
        for status in (301, 302, 307, 308):
            self.session.request.return_value = response(status)
            with self.assertRaises(RequestError) as error:
                self.client.progress("one")
            self.assertEqual(error.exception.status, status)

    def test_invalid_json_is_sanitized(self):
        bad = response()
        bad.json.side_effect = ValueError("secret response body")
        self.session.request.return_value = bad
        with self.assertRaises(RequestError) as error:
            self.client.progress("one")
        self.assertNotIn("secret", str(error.exception))
        self.assertIn("invalid JSON", str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def test_transport_failure_is_sanitized_and_not_retried(self):
        self.session.request.side_effect = requests.Timeout("secret-test-token")
        with self.assertRaises(RequestError) as error:
            self.client.write_position("one", 4, 20)
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(error.exception.status)
        self.session.request.assert_called_once()

    def test_object_and_list_schema_errors(self):
        for data in ([], "HTML", None):
            self.session.request.return_value = response(data=data)
            with self.assertRaises(RequestError):
                self.client.progress("one")
        for data in ({}, {"mediaProgress": {}}, {"mediaProgress": [None]}):
            self.session.request.return_value = response(data=data)
            with self.assertRaises(RequestError):
                self.client.allprogress()

    def test_libraries_allprogress_and_version(self):
        self.session.request.side_effect = [
            response(data={"libraries": [{"id": "lib"}]}),
            response(data={"mediaProgress": [{"id": "mp"}]}),
            response(data={"app": "audiobookshelf", "serverVersion": "2.37.1"}),
        ]
        self.assertEqual(self.client.libraries(), [{"id": "lib"}])
        self.assertEqual(self.client.allprogress(), [{"id": "mp"}])
        self.assertEqual(self.client.version(), "2.37.1")

    def test_wrong_app_does_not_provide_version(self):
        self.session.request.return_value = response(data={"app": "other", "serverVersion": "2.37.1"})
        with self.assertRaises(RequestError):
            self.client.version()

    def test_item_expansion_and_path_encoding(self):
        self.client.item("item/with space")
        args, kwargs = self.session.request.call_args
        self.assertTrue(args[1].endswith("/item%2Fwith%20space"))
        self.assertEqual(kwargs["params"], {"expanded": 1, "include": "progress"})

    def test_inventory_paginates_and_empty_is_valid(self):
        first = [{"id": str(i)} for i in range(100)]
        final = [{"id": "last"}]
        self.session.request.side_effect = [
            response(data={"results": first, "total": 101}),
            response(data={"results": final, "total": 101}),
            response(data={"results": [], "total": 0}),
        ]
        self.assertEqual(self.client.inventory("lib"), first + final)
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].kwargs["params"], {"limit": 100, "page": 0})
        self.assertEqual(calls[1].kwargs["params"], {"limit": 100, "page": 1})
        self.assertEqual(self.client.inventory("empty"), [])

    def test_inconsistent_inventory_rejected(self):
        for bad in (
            {"results": [], "total": 1},
            {"results": [{"id": "a"}], "total": 0},
            {"results": [{"id": "a"}, {"id": "a"}], "total": 2},
            {"results": [{}], "total": 1},
            {"results": [], "total": True},
        ):
            self.session.request.return_value = response(data=bad)
            with self.assertRaises(RequestError):
                self.client.inventory("lib")

    def test_changed_total_rejected(self):
        self.session.request.side_effect = [
            response(data={"results": [{"id": str(i)} for i in range(100)], "total": 101}),
            response(data={"results": [{"id": "last"}], "total": 102}),
        ]
        with self.assertRaises(RequestError):
            self.client.inventory("lib")

    def test_position_payload_omits_finished_and_disables_auto_completion(self):
        self.client.write_position("one", 99, 100)
        payload = self.session.request.call_args.kwargs["json"]
        self.assertEqual(payload, {"currentTime": 99, "duration": 100, "progress": 0.99,
                                   "markAsFinishedTimeRemaining": 0})
        self.assertNotIn("isFinished", payload)
        self.session.request.return_value.json.assert_not_called()

    def test_completion_payload_and_known_timestamp(self):
        self.client.write_finished("one", True, 1793340000000)
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "isFinished": True, "markAsFinishedTimeRemaining": 0, "finishedAt": 1793340000000,
        })
        self.client.write_finished("one", False)
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "isFinished": False, "markAsFinishedTimeRemaining": 0,
        })

    def test_invalid_write_values_do_not_send(self):
        for pos, duration in ((-1, 10), (11, 10), (0, 0), (True, 10), (float("nan"), 10), (1, float("inf"))):
            with self.assertRaises(ValueError):
                self.client.write_position("one", pos, duration)
        for status, when in ((1, None), (False, 1000), (True, -1), (True, 1.5), (True, float("nan"))):
            with self.assertRaises(ValueError):
                self.client.write_finished("one", status, when)
        self.session.request.assert_not_called()

    def test_invalid_base_urls_do_not_echo_secrets(self):
        for url in ("ftp://host", "http://user:secret@host", "http://host?token=secret", "http://host:secret"):
            with self.assertRaises(ValueError) as error:
                ABSClient(url, "secret-token")
            self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
