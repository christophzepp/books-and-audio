"""Publish the optional, minimized Floppy snapshot on the host using the two stock containers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

# The standalone host helper verifies the version-1 export without installing
# the Floppy ORM or Books & Audio on the Docker host.
MAX_BYTES = 8 * 1024 * 1024


def scope(upstream, username, floppy_user, relay_url):
    values = [upstream.rstrip('/'), username, floppy_user, relay_url.rstrip('/')]
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError('Incomplete Floppy account scope')
    encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


# Runs only in the short-lived Floppy process. Read the installed exporter
# from stdin unchanged; do not interpolate arguments, URLs or its source.
FLOPPY_PROCESS = """\
from contextlib import redirect_stdout
import io
import os
import sys

code = compile(sys.stdin.buffer.read(), '/app/floppy_evidence.py', 'exec')
# Framework handlers may write directly to fd 1 or during interpreter shutdown.
# Keep diagnostics on stderr; reserve the original stdout for the export.
sys.stdout.flush()
with os.fdopen(os.dup(1), 'w', encoding='utf-8') as output:
    os.dup2(2, 1)
    with redirect_stdout(sys.stderr):
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        from django.conf import settings
        settings.TESTING = True
        from django.db import connections
        db = connections['default']
        if db.vendor == 'sqlite':
            options = db.settings_dict.get('OPTIONS', {})
            if options.get('transaction_mode'):
                options['transaction_mode'] = 'DEFERRED'
    result = io.StringIO()
    with redirect_stdout(result):
        exec(code, {'__name__': '__main__', '__file__': '/app/floppy_evidence.py'})
    output.write(result.getvalue())
"""


def refresh(output, web_container, floppy_container, upstream, pine_user, floppy_user, relay_url):
    script = subprocess.run(['docker', 'exec', web_container, 'cat', '/app/floppy_evidence.py'],
                            check=True, stdout=subprocess.PIPE, timeout=15).stdout
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.floppy-', delete=False) as temp:
        path = Path(temp.name)
        try:
            subprocess.run(['docker', 'exec', '-i', floppy_container, 'python', '-c', FLOPPY_PROCESS, '--floppy-user', floppy_user,
                            '--relay-url', relay_url, '--pine-user', pine_user, '--upstream', upstream],
                           input=script, stdout=temp, check=True, timeout=120)
            temp.flush()
            if not 0 < temp.tell() <= MAX_BYTES:
                raise ValueError('Floppy returned an invalid snapshot size')
            snapshot = json.loads(path.read_bytes())
            if (snapshot.get('version') != 1 or snapshot.get('scope') != scope(upstream,pine_user,floppy_user,relay_url)
                    or not isinstance(snapshot.get('episodes'), dict)):
                raise ValueError('Floppy returned an unexpected snapshot')
            os.fsync(temp.fileno())
            os.replace(path, output)
            return len(snapshot['episodes'])
        finally:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('web-container', 'floppy-container', 'upstream', 'pine-user', 'floppy-user', 'relay-url'):
        parser.add_argument('--'+arg, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print('Floppy-Episoden im Lesebeleg:', refresh(**vars(args)))
