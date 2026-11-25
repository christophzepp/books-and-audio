"""Resolve imported files using native Readarr, Bookshelf or Chaptarr metadata."""
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests

from series_files import components
from series_listenarr import Defer, display_component, episode_label


def source_path(source, path):
    if not isinstance(path, str):
        raise ValueError('Ungültiger Arr-Dateipfad')
    for root in ('/data/audio-dramas', '/audio-dramas'):
        prefix = f'{root}/.series-import/{source}/'
        if path.startswith(prefix):
            rel = path[len(prefix):]
            components(rel)
            return rel
    return None


def resolve(event, get):
    from series_imports import AUDIO
    service, book_id, author_id = event['source'], event['book_id'], event['author_id']
    book = get(f'book/{book_id}', None)
    if book.get('id') != book_id or book.get('authorId') != author_id or not book.get('title'):
        raise Defer('Buch oder Autor im Import inzwischen geändert')
    matches = {(row['id'], row['title'], str(link.get('position') or ''))
               for row in get('series', {'authorId': author_id}) for link in row.get('links', [])
               if link.get('bookId') == book_id and link.get('seriesId') == row['id'] and row.get('title')}
    if len(matches) != 1:
        raise Defer('Keine eindeutige Serienzuordnung im Importdienst')
    _, series, number = matches.pop()
    label = episode_label({'seriesNumber': number, 'title': book['title']})
    target = '/'.join((display_component(series), label + ' - ' + display_component(book['title'])))
    components(target)
    if any(len(part.encode()) > 255 for part in target.split('/')):
        raise Defer('Der Serien- oder Folgenname ist zu lang')
    files = {}
    for row in get('bookfile', {'bookId': book_id}):
        path = row.get('path')
        if not isinstance(path, str) or PurePosixPath(path).suffix.casefold() not in AUDIO:
            continue
        rel = source_path(service, path)
        if (rel is None or row.get('bookId') != book_id or type(row.get('id')) is not int or row['id'] <= 0
                or type(row.get('size')) is not int or row['size'] <= 0 or rel in files):
            raise Defer('Native Audiodateien haben keine eindeutige Zuordnung zum Importstamm')
        files[rel] = [row['id'], row['size']]
    if not files or not all(files.get(rel) == expected for rel, expected in event['claimed'].items()):
        raise Defer('Datei-ID, Pfad oder Größe weicht von der Abschlussmeldung ab')
    common = os.path.commonpath([str(PurePosixPath(rel).parent) for rel in files])
    folder = common if common not in ('', '.') else ''
    audio = {str(PurePosixPath(rel).relative_to(folder)): size for rel, (_, size) in files.items()}
    return folder, target, audio


def lookup(job):
    prefix = 'SERIES_' + job['source'].upper()
    origin = os.environ[prefix + '_ORIGIN'].rstrip('/')
    endpoint = urlsplit(origin)
    if endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment:
        raise ValueError('Ungültige Arr-Dienstadresse')
    with requests.Session() as session:
        session.trust_env = False
        session.headers['X-Api-Key'] = Path(os.environ[prefix + '_KEY_FILE']).read_text().strip()
        def get(route, params):
            response = session.get(origin + '/api/v1/' + route, params=params, allow_redirects=False, timeout=(5, 15))
            if response.status_code != 200:
                raise RuntimeError('Arr HTTP ' + str(response.status_code))
            return response.json()
        return resolve(job, get)
