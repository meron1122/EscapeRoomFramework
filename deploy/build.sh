#!/usr/bin/env bash
# Builds everything the Sprite needs into deploy/bundle: the engine (self-contained, no .NET on the Sprite),
# the simulator's web build, the room and its brief, and the proxy and broker configuration.
# Run from anywhere:  deploy/build.sh [room-id] [room.json] [brief.md]
set -euo pipefail
cd "$(dirname "$0")/.."
ROOM_ID="${1:-the-core}"
ROOM_JSON="${2:-examples/the-core.json}"
BRIEF="${3:-docs/scenario-the-core.md}"
OUT=deploy/bundle
rm -rf "$OUT" && mkdir -p "$OUT/deploy" "$OUT/room"

dotnet publish src/EscapeRoom -c Release -r linux-x64 --self-contained true -o "$OUT/engine" --nologo -v q
(cd simulator/godot && godot --headless --path . --export-release "Web" ../web/index.html >/dev/null 2>&1)
# The landing page is the site's front door; the simulator lives under /play/.
mkdir -p "$OUT/web" && cp site/index.html "$OUT/web/" && cp -R site/fonts "$OUT/web/fonts"
cp -R simulator/web "$OUT/web/play"

cp "$ROOM_JSON" "$OUT/room/room.json"
cp "$BRIEF" "$OUT/room/brief.md"
echo "$ROOM_ID" > "$OUT/room/id"
python3 deploy/make-acl.py "$ROOM_ID" "$ROOM_JSON" > "$OUT/deploy/browser.acl"
cp deploy/Caddyfile deploy/mosquitto.conf deploy/setup.sh deploy/run-engine.sh deploy/env.example "$OUT/deploy/"
tar -C deploy -czf deploy/bundle.tgz bundle
du -sh deploy/bundle.tgz
