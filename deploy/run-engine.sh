#!/usr/bin/env bash
# Starts the engine with its settings from /home/sprite/app/env. Listens on localhost only.
set -euo pipefail
APP=/home/sprite/app
set -a; . "$APP/env"; set +a
export ASPNETCORE_URLS=http://127.0.0.1:5201
export ASPNETCORE_ENVIRONMENT=Production
export ConnectionStrings__Postgres="Host=127.0.0.1;Port=5432;Database=escaperoom;Username=escaperoom;Password=$DB_PASSWORD"
export Mqtt__Host=127.0.0.1
if [ -n "${GAMEMASTER_GATEWAY:-}" ]; then
  export GameMaster__Provider=openrouter
  export GameMaster__BaseUrl="$GAMEMASTER_GATEWAY"
  export GameMaster__Model="${GAMEMASTER_MODEL:-deepseek/deepseek-v4-flash}"
  [ -z "${GAMEMASTER_CHAT_PATH:-}" ] || export GameMaster__ChatPath="$GAMEMASTER_CHAT_PATH"
else
  export GameMaster__RulesOnly=true
fi
[ -z "${VOICE_GATEWAY:-}" ] || export Voice__BaseUrl="$VOICE_GATEWAY"
# The OpenRouter Connector also speaks (ElevenLabs models on /v1/audio/speech); a gateway to ElevenLabs itself needs no provider.
case "${VOICE_GATEWAY:-}" in */gateway/openrouter/*) export Voice__Provider=openrouter;; esac
[ -z "${VOICE_MODEL:-}" ] || export Voice__Model="$VOICE_MODEL"
export Voice__PublicUrl="$PUBLIC_URL"
export Voice__Cache="$APP/voice"
export Limits__GameStartsPerHour="$GAME_STARTS_PER_HOUR"
export Voice__MaxCharactersPerDay="$VOICE_CHARACTERS_PER_DAY"
cd "$APP/engine"
exec ./EscapeRoom
