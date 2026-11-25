"""ReadMeABook terminal requests enriched with an exact, scoped ABS item match."""
import collections
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests

from abs_client import ABSClient
from series_imports import item_path, receipt


def finished_requests(fetch):
    finished = {}
    for status in ('available', 'downloaded'):
        page = 1
        while True:
            result = fetch({'type': 'audiobook', 'status': status, 'page': page, 'pageSize': 100})
            if result.get('page') != page or type(result.get('totalPages')) is not int or not isinstance(result.get('requests'), list):
                raise ValueError('ReadMeABook liefert keine vollständige Anfragenseite')
            for row in result['requests']:
                asin = row.get('asin')
                if (isinstance(asin, str) and asin and row.get('status') == status
                        and row.get('type') == 'audiobook' and row.get('inLibrary') is True
                        and row.get('libraryAsin') in (None, asin) and row.get('requestId')):
                    finished.setdefault(asin, row['requestId'])
            if page >= result['totalPages']:
                break
            page += 1
            if page > 10000:
                raise ValueError('Zu viele ReadMeABook-Seiten')
    return finished


def match_requests(finished, abs_api, library_id, known=None, intake_root=None):
    def translated(path):
        if intake_root is None:
            return path
        root = PurePosixPath(intake_root)
        actual = PurePosixPath(path)
        if not root.is_absolute() or '..' in root.parts or root == PurePosixPath('/'):
            raise ValueError('Ungültiger ABS-Importstamm')
        if root not in actual.parents or '..' in actual.parts:
            return '/unregistered'
        return str(PurePosixPath('/audio-dramas/.series-import/readmeabook') / actual.relative_to(root))

    by_asin = collections.defaultdict(list)
    for row in abs_api.inventory(library_id):
        if row.get('libraryId') != library_id or row.get('mediaType') != 'book' or row.get('isMissing') or row.get('isInvalid'):
            continue
        asin = row.get('media', {}).get('metadata', {}).get('asin')
        if asin in finished:
            by_asin[asin].append(row)
    for asin, rows in by_asin.items():
        if len(rows) != 1:
            continue
        row = rows[0]
        path = row.get('path')
        if not isinstance(path, str):
            continue
        parts = item_path('readmeabook', translated(path) + '/track.m4b')
        if not parts or len(parts) != 4:
            continue
        revision = [finished[asin], row['id'], row.get('updatedAt')]
        if known and known('readmeabook', '/'.join(parts[:3]), revision):
            continue
        item = abs_api.item(row['id'])
        if (item.get('path') != path or item.get('libraryId') != library_id or item.get('id') != row['id']
                or item.get('media', {}).get('metadata', {}).get('asin') != asin):
            continue
        files = []
        for track in item['media']['tracks']:
            p = track['metadata']['path']
            if not item_path('readmeabook', translated(p)) or PurePosixPath(path) not in PurePosixPath(p).parents:
                raise ValueError('ReadMeABook-Titel enthält fremde Dateipfade')
            files.append(str(PurePosixPath(p).relative_to(path)))
        if files:
            yield receipt('readmeabook', parts[:3], files, revision)


def completed_imports(known=None):
    root = os.environ['SERIES_RMAB_ORIGIN'].rstrip('/')
    parsed = urlsplit(root)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Ungültige ReadMeABook-Adresse')
    abs_api = ABSClient(os.environ['SERIES_ABS_ORIGIN'], Path(os.environ['SERIES_ABS_KEY_FILE']).read_text().strip())
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.headers['Authorization'] = 'Bearer ' + Path(os.environ['SERIES_RMAB_KEY_FILE']).read_text().strip()
            def fetch(params):
                response = session.get(root + '/api/admin/requests', params=params, allow_redirects=False, timeout=(5, 15))
                if response.status_code != 200:
                    raise ValueError('ReadMeABook HTTP ' + str(response.status_code))
                return response.json()
            yield from match_requests(finished_requests(fetch), abs_api, os.environ['SERIES_ABS_LIBRARY_ID'], known,
                                      os.environ.get('SERIES_ABS_RMAB_ROOT') or None)
    finally:
        abs_api.close()
