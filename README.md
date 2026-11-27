# EscapeRoomFramework

**Shadowman** is the AI game master at the heart of it: he watches every player and decides what the room does next. The landing page in `site/` introduces him; see [deploy/README.md](deploy/README.md) for hosting.

A scenario engine for escape rooms. You describe a room in JSON: its inputs (sensors), outputs (locks, lights)
and a graph of puzzles. The engine tracks the game's progress, stores every event in Postgres and drives the hardware.

## Architecture

Every piece of hardware talks to the engine over **MQTT**. It is the common denominator of the whole IoT ecosystem:

```
Tasmota ─┐
ESPHome ─┤                      ┌─ Engine (.NET) ── Postgres
zigbee2mqtt (Zigbee) ─┼─ MQTT ──┤
Shelly / anything ────┤         └─ HTTP API (for a future React front end)
RTSP camera → QrCamera ─┘
```

An RTSP camera does not speak MQTT, so `QrCamera` turns it into an MQTT device. ffmpeg grabs frames, ZXing reads QR codes,
and the code it reads goes to `escaperoom/camera/<id>/qr` like any other sensor. Other "virtual sensors"
(image recognition, audio and so on) can be attached later the same way, with no changes to the engine.

## Running

```bash
docker compose up -d                      # Postgres (port 5433) + Mosquitto (1883)
dotnet run --project src/EscapeRoom       # API at http://localhost:5201
curl -X PUT localhost:5201/rooms/lab -H 'content-type: application/json' --data-binary @examples/lab.json
curl -X POST localhost:5201/rooms/lab/start
curl localhost:5201/rooms/lab/session
```

Tests: `dotnet test`.

## Room definition

Full example: [examples/lab.json](examples/lab.json). A camera reads a QR code, which opens a trapdoor. Inside are sensors
(Zigbee flasks, an ESPHome thermometer, Tasmota buttons). Solving the flasks opens a stash, and the flasks together
with the buttons unlock the exit.

### Inputs and outputs (`inputs`, `outputs`)

Each one sets exactly one of: `topic`, `tasmota`, `esphome`, `z2m`, `camera`.

| Preset | Input (subscription) | Output (publication) |
|---|---|---|
| `{"topic": "a/b", "path": "x.y"}` | `a/b`, field `x.y` of the JSON | `a/b`, with `path` → `{"path": value}` |
| `{"tasmota": "dev", "path": "Button1.Action"}` | `stat/dev/RESULT` | `cmnd/dev/<command>` (`Power1` by default) |
| `{"esphome": "node", "entity": "switch/lock"}` | `node/switch/lock/state` | `node/switch/lock/command` |
| `{"z2m": "name", "path": "contact"}` | `zigbee2mqtt/name` | `zigbee2mqtt/name/set` |
| `{"camera": "cam1"}` | `escaperoom/camera/cam1/qr` | — |

Without `path` the value is the whole payload. Values are compared as text, case-insensitively
(`true`, `"ON"`, `42`).

Tasmota sends buttons and switches to `stat/.../RESULT` only after they are detached from the relays:
`SetOption73 1` for buttons, `SetOption114 1` for switches.

### Puzzles (`puzzles`)

```json
{ "id": "flasks", "after": ["qr-code"], "when": { ... }, "then": [ ... ] }
```

- `after`: the puzzle becomes active only once all the listed ones are solved. This allows parallel branches
  and their joins.
- `when`: the condition. It counts from the moment of activation, including state from before it: a sensor that is
  already in the right position solves the puzzle at once. No `when` means a puzzle that only the GM can solve by hand.
  - `{"input": "thermo", "equals": "x" | "gt": 40 | "lt": 10}`. Without a comparison any value matches.
  - `{"all": [...]}`, `{"any": [...]}`.
  - `{"sequence": [...]}`: an order of events, e.g. buttons or a code. The last N matching events count,
    so a mistake does not block the game, and events that match no step (releasing a button) are ignored.
    It must be the whole `when`.
- `then`: steps run after the puzzle is solved: `{"output": "lock", "value": "OFF"}` or `{"delay": 3000}`
  (e.g. a pulse for an electric lock).

`start` (at room level) is the steps run when the game starts, e.g. locking everything.

### For the game master (`characters`, `variants`, `twists`, `hints`, brief)

The AI game master has nothing about a particular room in its code. Everything it knows comes from the engine, so it runs
any room, one at a time.

- **Brief** (`PUT /rooms/{id}/brief`, Markdown): the story, the mood, the characters, what the puzzles look like, pacing and
  direction, described by the venue the way it would be for a human. Example: [docs/scenario-the-core.md](docs/scenario-the-core.md).
- `characters`: the voices it speaks with, e.g. `{"name": "RAVEN", "about": "...", "output": "voice", "voice": "<TTS voice id>", "screen": "screen_zeta"}`.
  A line goes to `output` as `{speaker, text, to, audio}`, and the text also to `screen`.
- `variants`: for each key, a list of variants `{"solutions": {"puzzle": {condition}}, "clues": [steps], "summary": "..."}`.
  A draw swaps the solution and the clue together, only in the current game (the saved room keeps its default).
- `twists`: prepared twists `{"about": "...", "then": [steps], "adds": [puzzles], "blocks": [...], "after": [...]}`,
  once per game. `adds` adds puzzles, `blocks` makes puzzles wait for the added ones, `after` says what must already be solved.
- `cues`: wordless hints built into the room (a lamp, a neon, a sound): `{"about": "...", "puzzle": "phone", "then": [steps]}`.
  The game master can fire them repeatedly before it says anything; with `puzzle`, only while that puzzle is active.
- `hints` in a puzzle: the venue's prepared hints, from the gentlest. Without a model the game master uses only these.

Outputs named `gm` (the game master's thoughts) and `hint` (which puzzle got help) are optional.

Model: `OPENROUTER_API_KEY` (through OpenRouter) or `ANTHROPIC_API_KEY` / `ant auth login`; without credentials the game
runs on rules. Voice: `ELEVENLABS_API_KEY` (model `Voice:Model`, `eleven_v3` by default); the mp3 files are served
under `/voice/…` (public address in `Voice:PublicUrl`). On a Sprite the keys are held by Connectors: `GameMaster:BaseUrl`
and `Voice:BaseUrl` point at their gateways.

The definition is validated on save: unknown fields (the typo `"equal"`), inputs, outputs and puzzles that do not exist,
and cycles in `after` return 400 with a list of errors.

## Cameras

In `appsettings.json` or in environment variables (`Cameras__0__Id`, `Cameras__0__Url`):

```json
"Cameras": [ { "Id": "lab-cam", "Url": "rtsp://user:pass@10.0.0.20/stream1", "Width": 1280, "Height": 720, "Fps": 2 } ]
```

Requires `ffmpeg` on the PATH. The same code is published again only after it has been out of frame for ~3 s.

## API

| | |
|---|---|
| `PUT /rooms/{id}` | save a definition (validated) |
| `GET /rooms/{id}` | the definition |
| `POST /rooms/{id}/start` | a new game (stops the running one) |
| `POST /rooms/{id}/stop` | end the game |
| `POST /rooms/{id}/puzzles/{puzzle}/solve` | solved by hand by the GM (runs `then`) |
| `GET /rooms/{id}/session` | state of the latest game: puzzle statuses and input values |
| `PUT /rooms/{id}/brief`, `GET /rooms/{id}/brief` | the room's brief for the game master (Markdown) |
| `GET /voice/{file}.mp3` | recorded character lines for the room's speakers |
| `GET /safety`, `GET /safety/log`, `PUT /safety` | the safety layer, see below (changes only with an operator token) |

Every event (`input`, `solved`, `output`, `gm-solve`, `room-updated`, `started`, `finished`…) goes
to the `events` table. A running game is resumed after the process restarts.

Saving a room (`PUT` or MCP) during a game swaps the definition in that game at once and keeps the progress.
A new or rewired puzzle whose condition is already met is solved at once and runs its steps.

## MCP server

The engine exposes an MCP server (Streamable HTTP) at `/mcp`. With it an AI model can run the game and rewire
puzzles live. Connecting it to Claude Code:

```bash
claude mcp add --transport http escaperoom http://localhost:5201/mcp
```

| Tool | What for |
|---|---|
| `list_rooms`, `get_room`, `game_status` | reading rooms and the game state |
| `list_mqtt_topics` | what is on the MQTT bus and what it sends, to find new devices |
| `upsert_input`, `upsert_output` | add or replace a sensor or an output |
| `upsert_puzzle` | wire inputs and outputs into a puzzle (add or replace by `id`) |
| `remove_from_room`, `save_room` | remove an element, save the whole room |
| `start_game`, `stop_game`, `solve_puzzle` | running the game |
| `simulate_input` | a value "as if from a sensor": testing a puzzle or standing in for a broken sensor |
| `trigger_output` | driving an output by hand, also without a game |
| `get_brief`, `reroll_variant`, `fire_twist`, `fire_cue` | the room's brief, drawing a variant, a twist and a light cue in the running game |

Every change goes through the same validation as `PUT`. A wrong change is rejected with a list of reasons and nothing is saved.

The API and MCP answer only for hosts in `AllowedHosts` (localhost by default), which protects against DNS rebinding
from web pages. To reach the server from another machine, add its name or the server's IP in `appsettings.json`.

## Safety

Safety functions (the emergency button, the smoke detector, cutting power to the locks, emergency lighting) are a separate
category for the whole venue, independent of rooms and games. Example: [examples/safety.json](examples/safety.json).

- **The rules always run**, also with no game in progress. They are evaluated in memory, in the MQTT loop, and do not wait
  for the database. As long as the condition holds, every new sensor value sends the outputs again (e.g. when the lock
  controller has restarted).
- **The game and MCP cannot touch them.** A room output that would publish to a safety device is
  rejected when the room is saved. In addition, right before every publication by the game a last block is checked,
  written to the audit as `blocked`. A device "owns" every command to it, anything impersonating its readings,
  the Tasmota broadcast `cmnd/tasmotas/` and `zigbee2mqtt/bridge/`. MCP has no safety tool at all,
  and `list_mqtt_topics` marks safety topics as `safety: true`.
- **Changing the configuration requires an operator token** (`PUT /safety`, header `X-Safety-Token`, with `reason`
  and `config` in the body). The server's configuration holds only the SHA-256 of the token, so reading it does not let anyone impersonate an operator:

  ```bash
  TOKEN=$(openssl rand -hex 32); echo "token for the operator: $TOKEN"
  printf %s "$TOKEN" | shasum -a 256   # the result goes to Safety:Operators:<name> (e.g. the variable Safety__Operators__jan)
  ```

- **Audit:** the `safety_log` table is append-only. A trigger in Postgres rejects UPDATE, DELETE and TRUNCATE.
  It records every attempted change (`denied`, `rejected`, `config` with the operator, IP and reason), every time a rule
  fires (`fired`, `output`, `failed`) and every blocked publication by the game (`blocked`). A change is written
  to the audit before it takes effect. Reading: `GET /safety`, `GET /safety/log`.

**This is an additional layer; it does not replace hardware safety.** Electric locks must release when power is lost,
and the emergency button must physically cut their power, independently of this server, the network and the broker. Further
hardening belongs to the deployment:
- ACLs in the broker, so that other MQTT clients cannot publish to safety topics;
- safety devices kept out of zigbee2mqtt groups;
- an application that connects to Postgres with a role that does not own `safety_log` and cannot drop the trigger.

Restoring power to the locks after an alarm is deliberately outside the software and is done by a person.

## What is not there yet

A front end, authentication for the API, MCP and the broker, a game time limit and hints, `simulate_input` and `trigger_output`
in the HTTP API (for now only in MCP), retrying commands when MQTT is down, database migrations (the schema is `create if not exists`).
