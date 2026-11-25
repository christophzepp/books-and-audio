"""Sort completed radio-drama imports through Listenarr's native physical move API."""
import collections
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import http.client
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path as HostPath, PurePosixPath as Path
import re
import stat
import sys
import time
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/audio-dramas')
PATTERN = '{Author}/{Series}/{Title}'
SPECIAL_ASINS = frozenset(os.environ.get('SERIES_SPECIAL_ASINS', '').split(',')) - {''}
TERMINAL_DOWNLOADS = {'moved', 'failed'}
LOCK_DIRECTORY = os.environ.get('SERIES_MAINTENANCE_LOCK_DIR')
MAINTENANCE_LOCKS = tuple(HostPath(LOCK_DIRECTORY) / name for name in
    ('storage-maintenance.lock', 'snapraid-guard.lock')) if LOCK_DIRECTORY else ()
LOCK_OWNER = 0
STATUS_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError, TypeError)
ACTIVE_MOVES = {'queued', 'running', 'retryscheduled'}
TERMINAL_MOVES = {'failed', 'superseded', 'needsattention'}

# The installer creates this private NVMe state directory.
PENDING = HostPath(os.environ.get('SERIES_STATE', '/series-state')) / 'offener-auftrag.json'
MAX_STATUS_OUTAGE = 600


class Defer(Exception):
    """This entry cannot safely be changed on this run."""


class TargetCollision(Defer):
    """Only a confirmed target collision can suspend future filesystem probes."""


@contextmanager
def maintenance_guard():
    """Join the two existing root-owned lock files. Never create, replace or remove them."""
    with ExitStack() as handles:
        for name in MAINTENANCE_LOCKS:
            try:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                fd = os.open(name, flags)
                handles.callback(os.close, fd)
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != LOCK_OWNER
                        or before.st_nlink != 1 or before.st_mode & 0o022):
                    raise Defer('Wartungssperre ist nicht vertrauenswürdig')
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if not os.path.samestat(before, os.stat(name, follow_symlinks=False)):
                    raise Defer('Wartungssperre wurde zwischenzeitlich ausgetauscht')
            except OSError as error:
                raise Defer('Wartungssperre belegt oder nicht lesbar') from error
        yield


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, origin):
        self.origin = origin.rstrip('/')
        parts = urllib.parse.urlsplit(self.origin)
        try:
            local = ipaddress.ip_address(parts.hostname or '').is_loopback
        except ValueError:
            local = parts.hostname == 'localhost'
        try:
            local = local or ipaddress.ip_address(parts.hostname or '').is_private
        except ValueError:
            local = local or bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]*', parts.hostname or ''))
        if (not parts.hostname or parts.username is not None or parts.password is not None
                or parts.path or parts.query or parts.fragment
                or not (parts.scheme == 'https' or (parts.scheme == 'http' and local))):
            raise ValueError('Listenarr benötigt HTTPS oder eine interne HTTP-Adresse')
        self.csrf = None
        self.api_key = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, route, body=None, query=None):
        url = self.origin + '/api/v1/' + route
        if query:
            url += '?' + urllib.parse.urlencode(query)
        headers = {'Accept': 'application/json', 'Origin': self.origin,
                   'User-Agent': 'Books-and-Audio/series-organizer'}
        if self.api_key:
            headers['X-Api-Key'] = self.api_key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
            if self.csrf:
                headers['X-XSRF-TOKEN'] = self.csrf
        with self.opener.open(urllib.request.Request(url, data=data, headers=headers),
                              timeout=40) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def login(self):
        config_path = os.environ.get('LISTENARR_CONFIG_FILE')
        key_path = os.environ.get('LISTENARR_KEY_FILE')
        if config_path and key_path:
            raise ValueError('Specify only one key source')
        if config_path:
            with open(config_path, encoding='utf-8-sig') as stream:
                data = json.load(stream)
            values = [value for name, value in data.items() if name.casefold() == 'apikey']
            if len(values) != 1:
                raise ValueError('Missing or ambiguous top-level Listenarr API key')
            self.api_key = values[0]
        elif key_path:
            with open(key_path, encoding='utf-8') as stream:
                self.api_key = stream.read(8193).strip()
        if config_path or key_path:
            if (not isinstance(self.api_key, str) or not self.api_key or len(self.api_key) > 8192
                    or not self.api_key.isascii() or any(not 33 <= ord(c) <= 126 for c in self.api_key)):
                raise ValueError('Unusable Listenarr API key')
            return
        self.call('account/login', {'username': os.environ['LISTENARR_USERNAME'],
                  'password': os.environ['LISTENARR_PASSWORD'], 'rememberMe': False})
        self.csrf = self.call('antiforgery/token')['token']
        if not self.csrf:
            raise RuntimeError('No antiforgery token')


def identity(value):
    return unicodedata.normalize('NFC', str(value)).casefold()


def exact_path(value):
    if not isinstance(value, str) or not value.startswith('/'):
        raise Defer('absoluter Pfad fehlt')
    path = Path(value)
    if str(path) != value or '..' in path.parts or '\\' in value or any(
            unicodedata.category(char) == 'Cc' for char in value):
        raise Defer('Pfad unerwartet')
    return path


def episode_label(book):
    number = str(book.get('seriesNumber') if book.get('seriesNumber') is not None else '').strip()
    match = re.fullmatch(r'([0-9]{1,4})(?:[.,]([0-9]{1,4}))?', number)
    if match:
        fraction = (match.group(2) or '').rstrip('0')
        label = f'Folge {int(match.group(1)):03d}' + ('.' + fraction if fraction else '')
    elif not number and (book.get('asin') in SPECIAL_ASINS or re.search(
            r'\bsonderfolge\b', ' '.join(str(book.get(k) or '') for k in ('title', 'subtitle')), re.I)):
        label = 'Sonderfolge'
    elif not number:
        label = 'Ohne Folgennummer'
    else:
        raise Defer('Folgenposition ungeklärt')
    return label


def target_for(book):
    """Use existing sanitized title and series components only; native preview is checked before moving."""
    src = exact_path(book.get('basePath'))
    if ROOT not in src.parents or not (book.get('series') or '').strip():
        raise Defer('außerhalb der Serienauswahl')
    if len(src.parts) == 4:
        if already_sorted(book, src):
            return src
        # Listenarr collapses immediately repeated native components, e.g. author == series.
        if (src.parent.name != display_component(book['series'])
                or src.name != display_component(book.get('title'))):
            raise Defer('abweichender benutzerdefinierter Serienpfad')
    elif len(src.parts) != 5:
        raise Defer('kein alter Autoren-/Serien-/Titelpfad')
    label = episode_label(book)
    dest = ROOT / src.parent.name / f'{label} - {src.name}'
    if len(dest.name.encode('utf-8')) > 255 or dest == src or src in dest.parents:
        raise Defer('Zielname nicht sicher verwendbar')
    return dest


def owners(books):
    """Reserve current locations as well as projected locations of unimported siblings."""
    index = collections.defaultdict(set)
    for book in books:
        if book.get('basePath'):
            index[identity(book['basePath'])].add(book['id'])
        try:
            index[identity(target_for(book))].add(book['id'])
        except Defer:
            pass
    return index


def primary_series_asin(book):
    members = book.get('seriesMemberships') or []
    values = {member['seriesAsin'] for member in members if member.get('seriesAsin')
              and member.get('seriesName') and identity(member['seriesName']) == identity(book.get('series'))
              and member.get('isPrimary', True)}
    if len(values) > 1:
        raise TargetCollision('mehrdeutige Serienidentität')
    return next(iter(values), None) or book.get('seriesAsin')


def check_series_identity(book, target, current):
    known_names = {identity(book['series'])}
    primary = primary_series_asin(book)
    known_asins = {primary} if primary else set()
    for entry in current:
        if not entry.get('series') or not entry.get('basePath'):
            continue
        p = Path(entry['basePath'])
        if ROOT not in p.parents or len(p.parts) not in (4, 5):
            continue
        if identity(p.parent.name) != identity(target.parent.name):
            continue
        known_names.add(identity(entry['series']))
        other = primary_series_asin(entry)
        if other:
            known_asins.add(other)
    if len(known_names) > 1 or len(known_asins) > 1:
        raise TargetCollision('verschiedene Serien beanspruchen denselben Serienordner')


def browse(api, path):
    try:
        data = api.call('filesystem/browse', query={'path': str(path)})
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if data['currentPath'] != str(path) or not isinstance(data['items'], list):
        raise Defer('unerwartete Ordnerauskunft')
    return data['items']


def folder_tree(api, root):
    pending = [root]
    result = {}
    while pending:
        current = pending.pop()
        if len(result) > 4000 or len(current.parts) - len(root.parts) > 8:
            raise Defer('umfangreicher Ordner erfordert Einzelprüfung')
        items = browse(api, current)
        if items is None:
            raise Defer('Ordner fehlt')
        for item in items:
            p = exact_path(item['path'])
            if p.parent != current or p.name != item['name'] or not isinstance(item['isDirectory'], bool):
                raise Defer('unerwarteter Verzeichniseintrag')
            relative = str(p.relative_to(root))
            if relative in result:
                raise Defer('doppelter Verzeichniseintrag')
            result[relative] = item['isDirectory']
            if item['isDirectory']:
                pending.append(p)
    return result


def file_facts(book, base):
    files = book.get('files')
    if not isinstance(files, list) or not files:
        raise Defer('wartet auf abgeschlossenen Dateiimport')
    facts = {}
    for item in files:
        p = exact_path(item['path'])
        if base not in p.parents or not item.get('id') or not (item.get('size') or 0) > 0:
            raise Defer('Dateizuordnung ungeklärt')
        name = str(p.relative_to(base))
        if name in facts:
            raise Defer('doppelte Dateizuordnung')
        facts[name] = (item['id'], item['size'], item.get('durationSeconds'))
    return facts


def check_activity(api, book_id):
    for row in api.call('downloads'):
        if str(row['status']).lower() not in TERMINAL_DOWNLOADS and row.get('audiobookId') in (None, book_id):
            raise Defer('offener oder nicht zugeordneter Download')
    if api.call(f'library/{book_id}/move/recovery')['hasUnresolvedMove'] is not False:
        raise Defer('offener Verschiebeauftrag')


def check_free_target(api, target):
    # A file at the target (or at the new series directory) is also a conflict.
    for parent, child in ((ROOT, target.parent), (target.parent, target)):
        items = browse(api, parent)
        if items is None:
            if parent == ROOT:
                raise Defer('Hörspielstamm fehlt')
            return
        for item in items:
            if identity(item['name']) != identity(child.name):
                continue
            if child == target or item['name'] != child.name or not item['isDirectory']:
                raise TargetCollision('Ziel existiert oder kollidiert')
    if browse(api, target) is not None:
        raise TargetCollision('Zielordner existiert')


def native_source(api, book):
    fields = ('title', 'authors', 'series', 'seriesNumber', 'seriesMemberships', 'asin')
    metadata = {key: book[key] for key in fields if key in book}
    preview = api.call('library/preview-path', {'metadata': metadata, 'destinationRoot': str(ROOT)})
    if preview.get('root') != str(ROOT):
        raise Defer('unerwarteter Stamm in der Pfadvorschau')
    return exact_path(preview.get('fullPath'))


def display_component(value):
    # Mirror the upstream portable character rules only for counting existing targets.
    # Move decisions always use the live, native preview instead.
    value = ''.join(c for c in str(value or '') if unicodedata.category(c) != 'Cc')
    value = value.translate(str.maketrans({':': ' - ', '/': ' - ', chr(92): ' - ',
                                           **{c: '_' for c in '<>"|?*'}}))
    value = re.sub(r'\s+', ' ', value)
    value = re.sub(r'(?:\s*-\s*){2,}', ' - ', value)
    value = re.sub('_+', '_', value).strip().rstrip('. ')
    value = re.sub(r'^\s*[-_]+\s*|\s*[-_]+\s*$', '', value) or 'Unknown'
    stem, sep, ext = value.partition('.')
    if re.fullmatch(r'CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]', stem, re.I):
        value = stem + '_' + sep + ext
    return value


def already_sorted(book, path):
    label = episode_label(book)
    return (path.parent.name == display_component(book.get('series'))
            and path.name == f'{label} - {display_component(book.get("title"))}')


def move_result(api, book_id, source, target):
    """Submit exactly once. The local receipt already covers an ambiguous response."""
    route = f'library/{book_id}'
    job_id = None
    try:
        job = api.call(route + '/move', {'sourcePath': str(source), 'destinationPath': str(target),
                                        'moveFiles': True, 'deleteEmptySource': True})
        job_id = job['jobId']
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', str(job_id)):
            job_id = None
    except urllib.error.HTTPError as error:
        # Explicit rejections are conclusive. A broken connection or server error
        # after POST can still mean that a job was committed.
        if 400 <= error.code < 500 and error.code != 408:
            raise
    except STATUS_ERRORS:
        pass
    if job_id:
        remember_job_id(book_id, source, target, job_id)
        print(f'Verschiebeauftrag für #{book_id}: {job_id}', flush=True)
    else:
        print(f'Antwort für #{book_id} unklar; Abgleich ohne erneutes Senden.', flush=True)

    return observe_move(api, book_id, source, target, job_id)


def observe_move(api, book_id, source, target, job_id=None):
    route = f'library/{book_id}'
    last_known = time.monotonic()
    outage = False
    last_notice = None
    healthy_idle_since = None
    delay = 2
    while True:
        if outage and time.monotonic() - last_known >= MAX_STATUS_OUTAGE:
            raise RuntimeError(f'#{book_id}: Auftragsstatus seit zehn Minuten ungeklärt. '
                               'Wartungssperren werden freigegeben. Der nächste Lauf prüft zuerst diesen Auftrag.')
        try:
            if job_id:
                try:
                    job = api.call('library/move/' + job_id)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                    # A missing historical job alone does not establish inactivity.
                    job_id = None
                    outage = True
                    healthy_idle_since = None
                    time.sleep(2)
                    continue
                status = str(job['status']).lower()
                if status == 'completed':
                    return job
                if status in TERMINAL_MOVES:
                    raise Defer('Verschiebeauftrag erfordert Einzelprüfung: ' + status)
                if status not in ACTIVE_MOVES:
                    raise ValueError('Unbekannter Auftragszustand')
            else:
                recovery = api.call(route + '/move/recovery')
                if recovery['hasUnresolvedMove'] is True:
                    healthy_idle_since = None
                    candidate = recovery.get('jobId')
                    if (recovery.get('requestedPath') == str(target) and isinstance(candidate, str)
                            and re.fullmatch(r'[0-9a-fA-F-]{36}', candidate)):
                        job_id = candidate
                        time.sleep(2)
                        # Resolving a valid id is not itself proof of a readable job.
                        continue
                    status = str(recovery.get('status')).lower()
                    if status in TERMINAL_MOVES:
                        raise Defer('Listenarr meldet einen abgeschlossenen Schutzstopp')
                    if status not in ACTIVE_MOVES:
                        raise ValueError('Unbekannter Wiederanlaufzustand')
                elif recovery['hasUnresolvedMove'] is False:
                    current = api.call(route)['basePath']
                    if current == str(target):
                        # Publication completed before lookup; permit verification, but
                        # never approve host pruning without the actual terminal job receipt.
                        return {'status': 'Completed', 'sourceRetained': None}
                    if current != str(source):
                        raise Defer('Listenarr meldet ein anderes Ziel ohne aktiven Auftrag')
                    # The original HTTP connection is closed. Allow time for request
                    # cancellation / publication before giving up after continuous healthy
                    # observations. Neither network errors nor any active job count as idle.
                    now = time.monotonic()
                    if healthy_idle_since is None:
                        healthy_idle_since = now
                    elif now - healthy_idle_since >= 120:
                        raise Defer('Listenarr bestätigt wiederholt keinen laufenden Auftrag')
                else:
                    raise ValueError('Unbekannter Wiederanlaufzustand')
            delay = 5
            last_notice = None
            last_known = time.monotonic()
            outage = False
        except STATUS_ERRORS:
            healthy_idle_since = None
            outage = True
            now = time.monotonic()
            if last_notice is None or now - last_notice >= 300:
                print(f'#{book_id}: Auftragsstatus nicht prüfbar; Wartungssperren bleiben '
                      'während der begrenzten Wiederholungsfrist gehalten.', file=sys.stderr, flush=True)
                last_notice = now
            delay = min(60, max(5, delay * 2))
        time.sleep(min(delay, max(0, MAX_STATUS_OUTAGE - (time.monotonic() - last_known)))
                   if outage else delay)


def save_pending(book_id, source, target, facts, tree):
    """Publish a private receipt before POST. A later run observes it without replaying."""
    parent = PENDING.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise RuntimeError('Unsicheres Verzeichnis für den Auftragsbeleg')
    write_receipt({'id': book_id, 'source': str(source), 'target': str(target),
                   'files': facts, 'tree': tree}, replace=False)


def remember_job_id(book_id, source, target, job_id):
    try:
        data = load_pending()
        if (data is not None and data['id'] == book_id and data['source'] == str(source)
                and data['target'] == str(target)):
            data['jobId'] = job_id
            write_receipt(data, replace=True)
    except OSError:
        # The initial durable receipt already allows recovery by book id.
        print(f'#{book_id}: Wiederanlauf über Buchkennung erforderlich.', file=sys.stderr, flush=True)


def write_receipt(record, replace):
    fd, tmp = tempfile.mkstemp(prefix='.auftragsbeleg-', dir=PENDING.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(tmp, PENDING)
        else:
            os.link(tmp, PENDING)
        dir_fd = os.open(PENDING.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        HostPath(tmp).unlink(missing_ok=True)


def load_pending():
    try:
        fd = os.open(PENDING, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r', encoding='utf-8') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Auftragsbeleg nicht vertrauenswürdig')
        return json.load(source)


def resume_pending(api, record):
    book_id = record['id']
    source = exact_path(record['source'])
    target = exact_path(record['target'])
    if (type(book_id) is not int or book_id <= 0 or ROOT not in source.parents
            or len(source.parts) not in (4, 5) or target.parent.parent != ROOT or not record['files']):
        raise RuntimeError('Auftragsbeleg enthält unerwartetes Ziel')
    print(f'Prüfe vorherigen Verschiebeauftrag für #{book_id} ohne erneutes Senden.', flush=True)
    try:
        job_id = record.get('jobId')
        if job_id is not None and not re.fullmatch(r'[0-9a-fA-F-]{36}', str(job_id)):
            raise RuntimeError('Unzulässige Auftragskennung im Wiederanlaufbeleg')
        result = observe_move(api, book_id, source, target, job_id)
    except Defer as error:
        PENDING.unlink()
        # A stopped job or failed submission must not be immediately resubmitted
        # in the same run that resolved an uncertain prior request.
        raise RuntimeError(f'Früherer Auftrag #{book_id} geklärt: {error}') from error
    facts = {name: tuple(values) for name, values in record['files'].items()}
    removed = verify_move(api, book_id, source, target, facts, record['tree'], result)
    PENDING.unlink()
    return removed


def reconcile_one(api, row, apply):
    book_id = row['id']
    route = f'library/{book_id}'
    book = api.call(route)
    source = exact_path(book['basePath'])
    target = target_for(book)
    if str(source) != row['basePath'] or target != target_for(row):
        raise Defer('Eintrag wurde zwischenzeitlich geändert')
    facts = file_facts(book, source)
    check_activity(api, book_id)
    # Use Listenarr's own rendering so special characters and multiple authors are handled
    # identically. An explicitly overridden source is never automatically reorganized.
    if native_source(api, book) != source:
        raise Defer('Quellpfad weicht von der nativen Autorenvorlage ab')
    current = api.call('library')
    check_series_identity(book, target, current)
    occupied = owners(current)
    if any(occupied[identity(p)] != {book_id} for p in (target, source)):
        raise TargetCollision('Quelle oder Ziel mehrfach beansprucht')
    for entry in current:
        if entry['id'] == book_id or not entry.get('basePath'):
            continue
        other = Path(identity(entry['basePath']))
        for protected in (Path(identity(source)), Path(identity(target))):
            if other in protected.parents or protected in other.parents:
                raise TargetCollision('Bibliothekseintrag überlappt Quelle oder Ziel')
    from media_activity import record
    record('series', row.get('title'), 'series_target')
    tree = folder_tree(api, source)
    if not all(tree.get(name) is False for name in facts):
        raise Defer('registrierte Datei fehlt im Quellordner')
    check_free_target(api, target)
    if not apply:
        print(f'Vorschau #{book_id}: {source} -> {target}', flush=True)
        return
    last = api.call(route)
    if (last.get('basePath') != str(source) or target_for(last) != target
            or file_facts(last, source) != facts):
        raise Defer('Dateien wurden zwischenzeitlich geändert')
    check_activity(api, book_id)
    # Native manifest, source identity, verified destination publication, cleanup and
    # persisted recovery govern races after these checks. Never rewrite path strings only.
    save_pending(book_id, source, target, facts, tree)
    try:
        result = move_result(api, book_id, source, target)
    except (Defer, urllib.error.HTTPError):
        # move_result only propagates definite submission rejection or observed terminal state.
        PENDING.unlink()
        raise
    removed = verify_move(api, book_id, source, target, facts, tree, result)
    PENDING.unlink()
    return removed


def verify_move(api, book_id, source, target, facts, tree, result):
    route = f'library/{book_id}'
    after = api.call(route)
    if (after.get('basePath') != str(target) or file_facts(after, target) != facts
            or folder_tree(api, target) != tree):
        raise RuntimeError(f'Abschlussprüfung für #{book_id} fehlgeschlagen')
    print(f'Geprüft #{book_id}: {target}' + (' (Quelle von Listenarr beibehalten)'
          if result.get('sourceRetained') else ''), flush=True)

    removed = []
    host_root = os.environ.get('SERIES_DRAMA_MOUNT', '/audio-dramas')
    if host_root:
        from series_cleanup import prune_empty_authors
        bases = [entry.get('basePath') for entry in api.call('library')]
        removed = prune_empty_authors(str(source), str(target), after, result,
                    manifest_verified=True, catalog_base_paths=bases, host_root=host_root)
        if removed:
            print('Leere frühere Ordner entfernt: ' + ', '.join(removed), flush=True)
    return len(removed)


def validate_config(api):
    if api.call('configuration/settings').get('folderNamingPattern') != PATTERN:
        raise RuntimeError('Listenarr-Autorenvorlage hat sich geändert')
    roots = api.call('rootfolders')
    if len([root for root in roots if root.get('path') == str(ROOT)]) != 1:
        raise RuntimeError('Hörspielstamm fehlt oder ist nicht eindeutig')


def run(api, apply):
    removed = 0
    recovered = 0
    pending = load_pending() if apply else None
    if pending is not None:
        try:
            with maintenance_guard():
                removed += resume_pending(api, pending)
                recovered = 1
        except Defer as error:
            print(f'Früherer Auftrag wird zurückgestellt: {error}', flush=True)
            return dict(moved=0, skipped=0, conflicts=1, empty_dirs_removed=0)
    library = api.call('library')
    from listenarr_conflicts import ListenarrConflicts, signature
    control = HostPath(os.environ.get('SERIES_SETTINGS', '/series-control/settings.json')).parent
    parked = ListenarrConflicts(PENDING.parent, control) if apply else None
    keep = set()
    config_checked = False
    waiting = 0
    deferred = 0
    aligned = 0
    processed = 0
    for row in library:
        path = Path(row.get('basePath') or '/')
        if ROOT not in path.parents or not row.get('series') or len(path.parts) not in (4, 5):
            continue
        try:
            if len(path.parts) == 4 and already_sorted(row, path):
                aligned += 1
                continue
            if row.get('fileCount') == 0:
                waiting += 1
                continue
            key = str(row['id'])
            if parked and key in parked.rows:
                stamp = signature(row, library, target_for, Defer)
                if parked.held(key, stamp):
                    keep.add(key)
                    deferred += 1
                    continue
            with maintenance_guard() if apply else nullcontext():
                if not config_checked:
                    validate_config(api)
                    config_checked = True
                removed += reconcile_one(api, row, apply) or 0
            processed += 1
        except TargetCollision as error:
            deferred += 1
            if parked:
                key = str(row['id'])
                parked.record(key, signature(row, library, target_for, Defer), row.get('title'), error)
                keep.add(key)
            print(f'Zurückgestellt #{row["id"]}: {error}', flush=True)
        except Defer as error:
            deferred += 1
            print(f'Zurückgestellt #{row["id"]}: {error}', flush=True)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            deferred += 1
            print(f'Zurückgestellt #{row["id"]}: Listenarr meldet einen Konflikt', flush=True)
    action = 'Umsortiert' if apply else 'Vorgeschlagene Verschiebungen'
    print(f'Bereits korrekt zugeordnet: {aligned}. {action}: {processed}. '
          f'Warten auf Import: {waiting}. Zurückgestellt: {deferred}.', flush=True)

    if parked:
        parked.cleanup(keep)
    result = dict(moved=processed + recovered, skipped=aligned + waiting, conflicts=deferred,
                  empty_dirs_removed=removed)
    if parked and parked.rows:
        result['listenarr_conflicts'] = parked.public()
    return result
