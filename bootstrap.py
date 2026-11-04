"""Initialize a fresh Books & Audio installation. Never replace existing state."""
import argparse
import getpass
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from admin_store import AdminStore
from connection_manager import ConnectionManager

def initialize(root, cfg, creds):
    if not isinstance(cfg, dict) or not isinstance(creds, dict):
        raise ValueError("Konfiguration und Zugangsdaten müssen JSON-Objekte sein.")
    key = Fernet.generate_key()
    # Perform schema validation even before the new root is populated.
    ConnectionManager(root / 'state', cfg, creds, key)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Das Datenverzeichnis muss leer sein. Vorhandene Installationen werden nicht überschrieben.")
    conf = root / 'config'
    conf.mkdir(mode=0o700)
    (root / 'web').mkdir(mode=0o700)
    (root / 'web/control').mkdir(mode=0o700)
    for filename, data in {
        'config.json':json.dumps(cfg, ensure_ascii=False, indent=2).encode(),
        'credentials.enc':Fernet(key).encrypt(json.dumps(creds).encode()),
        'secret.key':key,
    }.items():
        with os.fdopen(os.open(conf / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(data)
    AdminStore(root / 'state', conf / 'secret.key').pause(True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--config', type=Path, help='Eigene Basiskonfiguration für eine neue produktive Instanz')
    args = parser.parse_args()
    os.umask(0o077)
    cfg = json.loads(args.config.read_text()) if args.config else {'mappings': [], 'interval_seconds': 60}
    creds = {}
    if args.config:
        fields = []
        if cfg.get('abs_url'): fields += ['abs_token']
        if cfg.get('bo_url'): fields += ['bo_username', 'bo_password']
        if cfg.get('jellyfin', {}).get('enabled'): fields += ['jellyfin_token']
        if cfg.get('bookbridge', {}).get('enabled'): fields += ['bookbridge_password']
        if cfg.get('pinepods', {}).get('enabled'): fields += ['pinepods_password', 'pinepods_api_key']
        if cfg.get('ntfy_url'): fields += ['ntfy_authorization']
        creds = {field:getpass.getpass(field + ': ') for field in fields}
    initialize(args.directory, cfg, creds)
    print("Initialisiert. Die Synchronisation ist pausiert.")

if __name__ == '__main__':
    main()
