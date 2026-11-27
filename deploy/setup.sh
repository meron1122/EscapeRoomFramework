#!/usr/bin/env bash
# Runs ON the Sprite, from the unpacked bundle: installs Postgres, Mosquitto and Caddy, creates the database,
# registers the four services and loads the room. Safe to run again after a new bundle.
set -euo pipefail
APP=/home/sprite/app
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
[ "$(uname -m)" = x86_64 ] || { echo "The engine bundle is built for x86_64; this Sprite is $(uname -m). Rebuild with -r linux-arm64."; exit 1; }

mkdir -p "$APP" && rsync -a --delete "$HERE/engine" "$HERE/web" "$HERE/room" "$HERE/deploy" "$APP/"
mkdir -p "$APP/voice" && chmod +x "$APP/deploy/run-engine.sh" "$APP/engine/EscapeRoom"

if [ ! -f "$APP/env" ]; then
  cp "$APP/deploy/env.example" "$APP/env"
  echo "DB_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9)" >> "$APP/env"
  chmod 600 "$APP/env"
  echo "Created $APP/env. Fill in PUBLIC_URL and the two gateway URLs, then run this script again."
  exit 0
fi
grep -q CHANGE-ME "$APP/env" && { echo "$APP/env still has CHANGE-ME values."; exit 1; }
set -a; . "$APP/env"; set +a

$SUDO apt-get update -qq
$SUDO apt-get install -y -qq postgresql mosquitto caddy rsync curl >/dev/null
# The packages' own system services stay off: the Sprite runtime owns the processes, so they survive sleep and wake.
$SUDO systemctl disable --now postgresql mosquitto caddy 2>/dev/null || true

PGBIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
PGDATA="$APP/pgdata"
if [ ! -d "$PGDATA" ]; then
  "$PGBIN/initdb" -D "$PGDATA" -U escaperoom --auth-local=trust --auth-host=scram-sha-256 >/dev/null
  echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
  echo "unix_socket_directories = '$APP'" >> "$PGDATA/postgresql.conf"
  "$PGBIN/pg_ctl" -D "$PGDATA" -w start >/dev/null
  "$PGBIN/psql" -h "$APP" -U escaperoom -d postgres -c "alter user escaperoom password '$DB_PASSWORD'" -c "create database escaperoom" >/dev/null
  "$PGBIN/pg_ctl" -D "$PGDATA" -w stop >/dev/null
fi

svc() {  # name, then sprite-env arguments; recreated so a new bundle takes effect
  local name="$1"; shift
  sprite-env services delete "$name" >/dev/null 2>&1 || true
  sprite-env services create "$name" "$@" --no-stream
}
svc postgres --cmd "$PGBIN/postgres" --args "-D,$PGDATA"
svc mosquitto --cmd /usr/sbin/mosquitto --args "-c,$APP/deploy/mosquitto.conf"
svc engine --cmd "$APP/deploy/run-engine.sh" --needs postgres,mosquitto
svc web --cmd /usr/bin/caddy --args "run,--config,$APP/deploy/Caddyfile,--adapter,caddyfile" --http-port 8080 --needs engine

for i in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:5201/safety && break; sleep 1; done
ROOM="$(cat "$APP/room/id")"
curl -fsS -X PUT "http://127.0.0.1:5201/rooms/$ROOM" -H 'Content-Type: application/json' --data-binary @"$APP/room/room.json"
curl -fsS -X PUT "http://127.0.0.1:5201/rooms/$ROOM/brief" -H 'Content-Type: text/markdown' --data-binary @"$APP/room/brief.md"
echo "Room '$ROOM' loaded. Public checks:"
for path in / /rooms/$ROOM /mcp /safety; do printf '  %-22s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080$path")"; done
echo "Expected: / 200, the rest 404. Then: sprite url update --auth public"
