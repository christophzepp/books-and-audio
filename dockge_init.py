"""Seed empty Compose volumes once; a restart preserves the selected sync state."""
import fcntl
import os
import tempfile
from pathlib import Path

from admin_store import AdminStore, atomic
from bootstrap import initialize


def publish_key(path, key):
    if path.exists():
        if path.read_bytes() != key:
            raise ValueError("Die Volumes enthalten unterschiedliche Schlüssel.")
        return
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(key)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def prepare(config_root, keys, state, cfg=None, credentials=None):
    """The private config directory lock also serializes concurrent initializers."""
    with (config_root / ".setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = config_root / "current"
        ready = config_root / "dockge-ready.json"
        web_key = keys / "secret.key"
        db = state / "control" / "admin.sqlite"
        if ready.exists():
            if not (current / "config.json").is_file() or not (current / "credentials.enc").is_file():
                raise ValueError("Die bestehende Konfiguration ist unvollständig.")
            if not db.is_file() or web_key.read_bytes() != (current / "secret.key").read_bytes():
                raise ValueError("Die Datenvolumes passen nicht zur bestehenden Installation.")
            return
        if not current.exists():
            if web_key.exists() or any(state.joinpath("control").iterdir()) or any(p.name != "control" for p in state.iterdir()):
                raise ValueError("Vorhandene Daten ohne zugehörige Konfiguration. Bitte Volumes prüfen.")
            with tempfile.TemporaryDirectory(dir=config_root, prefix=".setup-") as folder:
                stage = Path(folder)
                initialize(stage, cfg if cfg is not None else {"mappings": [], "interval_seconds": 60}, credentials or {})
                os.rename(stage / "config", current)
        key_file = current / "secret.key"
        publish_key(web_key, key_file.read_bytes())
        store = AdminStore(state, key_file)
        # A retry after interruption must preserve any recorded pause decision.
        with store.db() as connection:
            connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('paused','true')")
        atomic(ready, {"version": 1})


if __name__ == "__main__":
    import argparse
    import json
    from bootstrap import prompt_credentials
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basiskonfiguration für eine neue Installation")
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text()) if args.config else None
    secrets = prompt_credentials(config) if config is not None else None
    prepare(Path("/installation"), Path("/keys"), Path("/state"), config, secrets)
    print("Datenvolumes bereit.")
