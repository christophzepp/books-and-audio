#!/bin/sh
# Exercise the same image and compose file a new Dockge stack uses.
set -eu
: "${BOOKS_AUDIO_IMAGE:?set BOOKS_AUDIO_IMAGE to the image under test}"
export COMPOSE_FILE=compose.dockge.yaml
export COMPOSE_PROJECT_NAME="books-audio-test-${GITHUB_RUN_ID:-$$}"
export BOOKS_AUDIO_BIND=127.0.0.1
export BOOKS_AUDIO_PORT=18790
series_fixture=$(mktemp -d)
export SERIES_MEDIA_DIR="$series_fixture/hoerspiele"
export SERIES_CONFIG_DIR="$series_fixture/config"
mkdir "$SERIES_MEDIA_DIR" "$SERIES_CONFIG_DIR"
trap 'docker compose --profile hoerspiele down -v --remove-orphans; rmdir "$SERIES_MEDIA_DIR" "$SERIES_CONFIG_DIR" "$series_fixture"' EXIT
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
if [ "${CI:-}" = true ] || command -v node >/dev/null 2>&1; then
  node --test test_admin_*.cjs
fi
start_stack
docker compose exec -T worker python -c 'from zoneinfo import ZoneInfo; import podcast_media_queue, series_organizer; assert ZoneInfo("Europe/Berlin").key == "Europe/Berlin"'
curl -fsS http://127.0.0.1:18790/ | grep -q 'Books &amp; Audio'
docker compose exec -T worker python -c 'import json; assert json.load(open("/state/control/status.json"))["paused"] is True'
docker compose --profile hoerspiele up -d --pull never series-organizer
for attempt in $(seq 1 20); do
  if docker compose exec -T worker python -c 'import json; assert json.load(open("/state/series-status/status.json"))["status"] == "disabled"'; then
    break
  fi
  if [ "$attempt" -eq 20 ]; then docker compose logs --tail 20 series-organizer; exit 1; fi
  sleep 2
done
docker compose exec -T series-organizer python -c 'import os; assert os.getuid()==1000; assert os.statvfs("/series-control").f_flag & os.ST_RDONLY; assert not os.statvfs("/audio-dramas").f_flag & os.ST_RDONLY'
signature() {
  docker compose exec -T web python -c 'from pathlib import Path; import hashlib; print(hashlib.sha256(Path("/keys/secret.key").read_bytes()+Path("/admin-state/web-auth/setup-token").read_bytes()).hexdigest())'
}
before=$(signature)
# Persist a real administration choice through container replacement.
docker compose exec -T worker python -c 'from admin_store import AdminStore; AdminStore("/state","/installation/current/secret.key").pause(False)'
start_stack --force-recreate
test "$before" = "$(signature)"
docker compose exec -T worker python -c 'from admin_store import AdminStore; assert not AdminStore("/state","/installation/current/secret.key").paused()'
