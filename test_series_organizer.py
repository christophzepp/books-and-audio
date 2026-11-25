import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import series_organizer as app
from series_imports import ImportQueue


class Exchange:
    def __init__(self, data):
        self.input = BytesIO(data)
        self.output = b''

    def settimeout(self, _):
        pass

    def makefile(self, *_):
        return self.input

    def sendall(self, data):
        self.output += bytes(data)


class OrganizerSessionTests(unittest.TestCase):
    def setUp(self):
        selftemp = tempfile.TemporaryDirectory()
        self.addCleanup(selftemp.cleanup)
        self.root = Path(selftemp.name)
        self.queue = ImportQueue(self.root)
        self.key = 'private-test-key-only-123456789'

    def post(self, source, payload, auth=None):
        body = json.dumps(payload).encode()
        sock = Exchange((f'POST /imports/{source} HTTP/1.1\r\nHost: test\r\nContent-Length: {len(body)}\r\nAuthorization: {auth or ""}\r\n\r\n').encode() + body)
        app.receive(self.queue, self.key)(sock, ('127.0.0.1', 0), object())
        return int(sock.output.split(b' ', 2)[1])

    def test_producer_test_auth_and_dedup_from_actual_http_handler(self):
        path = '/data/audio-dramas/.series-import/readarr/A/S/1 - T/track.m4b'
        event = dict(eventType='Download',bookFiles=[{'path':path,'id':5}])
        self.assertEqual(self.post('readarr',event),401)
        self.assertEqual(self.post('readarr',dict(eventType='Test'),'Bearer '+self.key),204)
        credentials = base64.b64encode(('readarr:'+self.key).encode()).decode()
        self.assertEqual(self.post('readarr',event,'Basic '+credentials),202)
        self.assertEqual(self.post('readarr',event,'Bearer '+self.key),202)
        self.assertEqual(self.post('bookshelf',event,'Basic '+credentials),401)
        self.assertEqual(len(self.queue.queued()),1)

    def test_arr_basic_challenge_before_credentials(self):
        sock = Exchange(b'POST /imports/readarr HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n')
        app.receive(self.queue, self.key)(sock, ('127.0.0.1', 0), object())
        self.assertTrue(sock.output.startswith(b'HTTP/1.0 401'))
        self.assertIn(b'WWW-Authenticate: Basic realm=', sock.output)
        self.assertNotIn(self.key.encode(), sock.output)

    def test_settings_and_empty_queue_touch_no_media(self):
        with patch.dict(os.environ,{'LISTENARR_ORIGIN':'','SERIES_RMAB_ORIGIN':'','SERIES_DRAMA_MOUNT':'/absent-media-must-not-open'}):
            self.assertEqual(app.run(self.queue,True)['status'],'completed')
        settings = self.root/'settings.json'
        with patch.object(app,'SETTINGS',settings):
            self.assertEqual(app.options(),(False,3600))
            settings.write_text(json.dumps(dict(enabled=True,interval_seconds=600)))
            self.assertEqual(app.options(),(True,600))
            settings.write_text(json.dumps(dict(enabled=True,interval_seconds=1)))
            with self.assertRaises(ValueError): app.options()

    def test_unavailable_arr_catalog_does_not_touch_media_or_drop_event(self):
        self.queue.enqueue(dict(kind='arr-event', source='readarr', author_id=9, book_id=5,
                                claimed={'Author/Title.m4b': [17, 100]}))
        with patch.dict(os.environ, {'LISTENARR_ORIGIN': '', 'SERIES_RMAB_ORIGIN': ''}), \
             patch('series_arr.lookup', side_effect=OSError), \
             patch('series_organizer.os.open', side_effect=AssertionError('medienbezogener Zugriff')):
            result = app.run(self.queue, True)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(len(self.queue.queued()), 1)
