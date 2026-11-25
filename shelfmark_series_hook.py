#!/usr/bin/env python3
"""Shelfmark Advanced → Custom Script with native JSON payload enabled."""
import json
import os
from pathlib import Path
import sys
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def main():
    body = sys.stdin.buffer.read(1024 * 1024 + 1)
    if not body or len(body) > 1024 * 1024:
        raise ValueError('Shelfmark JSON-Abschlussmeldung fehlt oder ist zu groß')
    event = json.loads(body)
    if event.get('phase') != 'post_transfer' or event.get('output', {}).get('mode') != 'folder':
        return
    secret = Path(os.environ['SERIES_IMPORT_KEY_FILE']).read_text().strip()
    url = os.environ['SERIES_IMPORT_URL'].rstrip('/') + '/imports/shelfmark'
    request = urllib.request.Request(url, data=body, headers={'Content-Type':'application/json', 'Authorization':'Bearer ' + secret})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        if response.status not in (202, 204):
            raise RuntimeError('Hörspiel-Abschlussmeldung wurde nicht angenommen')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        sys.exit('Books & Audio: ' + type(error).__name__)
