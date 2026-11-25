"""Publish the optional, minimized Floppy snapshot on the host using the two stock containers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from floppy_evidence import MAX_BYTES, scope


def refresh(output, web_container, floppy_container, upstream, pine_user, floppy_user, relay_url):
    script = subprocess.run(['docker', 'exec', web_container, 'cat', '/app/floppy_evidence.py'],
                            check=True, capture_output=True, timeout=15).stdout
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.floppy-', delete=False) as temp:
        path = Path(temp.name)
        try:
            subprocess.run(['docker', 'exec', '-i', floppy_container, 'python', '-', '--floppy-user', floppy_user,
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
