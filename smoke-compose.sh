#!/bin/sh
# Exercise the same image and compose file a new Dockge stack uses.
set -eu
: "${BOOKS_AUDIO_IMAGE:?set BOOKS_AUDIO_IMAGE to the image under test}"
export COMPOSE_FILE=compose.dockge.yaml
export COMPOSE_PROJECT_NAME="books-audio-test-${GITHUB_RUN_ID:-$$}"
export BOOKS_AUDIO_BIND=127.0.0.1
export BOOKS_AUDIO_PORT=18790
trap 'docker compose down -v --remove-orphans' EXIT
start_stack() {
  docker compose up -d --pull never "$@"
  for attempt in $(seq 1 50); do
    if docker compose exec -T web python admin_web.py --health && docker compose exec -T worker python sync.py --health; then
      return
    fi
    sleep 2
  done
  docker compose logs --tail 40
  exit 1
}
start_stack
curl -fsS http://127.0.0.1:18790/ | grep -q 'Books &amp; Audio'
docker compose exec -T worker python -c 'import json; assert json.load(open("/state/control/status.json"))["paused"] is True'
signature() {
  docker compose exec -T web python -c 'from pathlib import Path; import hashlib; print(hashlib.sha256(Path("/keys/secret.key").read_bytes()+Path("/admin-state/web-auth/setup-token").read_bytes()).hexdigest())'
}
before=$(signature)
# Persist a real administration choice through container replacement.
docker compose exec -T worker python -c 'from admin_store import AdminStore; AdminStore("/state","/installation/current/secret.key").pause(False)'
start_stack --force-recreate
test "$before" = "$(signature)"
docker compose exec -T worker python -c 'from admin_store import AdminStore; assert not AdminStore("/state","/installation/current/secret.key").paused()'
