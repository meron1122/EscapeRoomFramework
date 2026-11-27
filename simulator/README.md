# The Core simulator

An isometric simulator of the room "The Core" (scenario: [docs/scenario-the-core.md](../docs/scenario-the-core.md)) in Godot 4.7,
running in the browser. Five simulated Nomads play through the game while Shadowman, the AI game master, draws a variant, gives hints
and adds twists the way the scenario describes. Without the engine a small local stand-in plays his part.

## What it is made of

| Path | What it is |
| --- | --- |
| `godot/layout.json` | the floor plan: dimensions, doors, props, puzzles and work spots; read by both Blender and Godot |
| `blender/build_assets.py` | builds the models of the rooms, props and characters → `godot/assets/*.glb` |
| `music/compose.py` | synthesises the sound from code → `godot/audio/*.mp3`: a calm bed per room, accents for what happens in it, a loud finale |
| `godot/scripts/sim.gd` | the clock, the puzzle graph, the game's variant, the offline stand-in for Shadowman (hints, twists) |
| `godot/scripts/npc.gd` | simulated players: walking on an A* grid, working, animation |
| `godot/scripts/main.gd` | assembles the scene, lights, screens, camera and music |
| `godot/scripts/atmosphere.gd` | the city in the background (a separate 3D world with a perspective camera), the platform, the rain |
| `godot/scripts/mqtt.gd` | an MQTT 3.1.1 client over WebSocket: the simulator poses as the room's hardware on the same broker as real devices |

## Rebuilding and running

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python simulator/blender/build_assets.py
```

```bash
python3 simulator/music/compose.py
```

```bash
cd simulator/godot && godot --headless --path . --import && godot --headless --path . --export-release "Web" ../web/index.html
```

```bash
python3 -m http.server 8060 --bind 127.0.0.1 --directory simulator/web
```

Then `http://localhost:8060`. Controls: LMB/RMB pans, the wheel zooms, 1/2/3 jump to the floors, F follows the team,
V shows this game's answers, H hides the panels, space pauses (PAUSE then covers the whole picture).

The game opens with a few pages of story over slow shots of the tower (it may be opened without the landing page),
one of them introducing the crew: five ordinary people from five scenes (Spark the punk, Rust the metalhead, Mouse
the goth, Thunder the cyber raver, Fox the skater), each with a portrait and a colour that is also their ring on the
floor. For the first seconds of a game their names float over their heads. Then comes the start screen. It runs at ×10 by default: a whole night in about six minutes. People walk at a human pace
at any speed up to ×10, so a faster game makes puzzles go faster, not legs.

## Reading the screen

| Where | What it tells |
|---|---|
| top left | the clock, the room in play, one mark per puzzle of the game (yellow: solved, cyan: in play) and what the team is on now |
| top right, pink | Shadowman: whether he is watching or thinking and why he woke up, how long the last decision took and on which model, his last note, his last moves |
| the banner under the buttons | whatever just happened: a puzzle solved, a move of his, something the visitor did |
| labels in the room | cyan over the puzzle in play, with its progress; pink over a lamp he has lit |
| ZETA's hologram | her face over the reception desk, the Z-Drive and her console, lit in the room the team is in; it moves when she speaks |
| a speech bubble in the room | the line being said, at the place its voice comes from: under ZETA's face, or over the players RAVEN talks to; the players stop and listen |
| subtitles | every spoken line; "SHADOWMAN, AS RAVEN" marks a line he chose to say through one of the room's characters |

Colour carries the roles: cyan is the team, pink is Shadowman, white is the visitor. The camera follows the team and
goes to a solved puzzle or a lamp he lights, until the visitor takes it with the mouse (F gives it back).

### Voices

Two kinds of line are heard. The room's own fixed announcements (ZETA's greeting, the patrol warning, RAVEN's last
words) are recordings in `godot/audio/lines/`, as they would be in a real room; `voice/lines.py` lists them from
`sim.gd` and `voice/record.py` speaks them through a speech gateway. The lines Shadowman writes during a game are
synthesised live by the engine and fetched by URL. Neither sounds like a studio: RAVEN goes through a narrow,
overdriven radio, ZETA through a doubled, slightly metallic chain with the echo of a hard, empty hall.
The voices themselves are the `voice` of each character in the room's data (`examples/the-core.json`).

The music is a calm bed per room, and the room plays the action over it: a note when a player settles at a puzzle,
a rising figure for a solved one, a dull pair for a wrong answer, a call for the help button, a swell for a door,
and one low, dark note that belongs only to Shadowman's moves. All of them share one scale, so they fit any room.

## Messing with the team

The TEST HIM bar at the top of the demo lets a visitor interfere, so they can watch Shadowman react:

| Button | What happens in the room | What he can see |
|---|---|---|
| STALL THE TEAM | nobody makes progress for five game minutes | players stuck at their spots, no inputs |
| RUSH THE TEAM | everything is solved four times faster for six minutes | a team running ahead of plan |
| WRONG ANSWER | a player enters three wrong answers | the wrong values on the sensors |
| ASK FOR HELP | the team presses the room's help button | a `help` event; it always gets a hint |

Nothing tells him what the visitor did: he has to read it from the room, as with real players. Only the help
button speaks to him directly, as it does in a real room.

## With the engine and Shadowman (virtual hardware)

When the MQTT broker is reachable, the simulator stops deciding the puzzles itself: it sends inputs (`sim/the-core/...`) like
the sensors in a room, and the engine and the game master agent decide everything. The screens, the doors, RAVEN's lines, hints
and twists come from the engine. Without a broker the local stand-in runs.

```bash
docker compose up -d
```

```bash
ASPNETCORE_ENVIRONMENT=Development dotnet run --project src/EscapeRoom
```

```bash
curl -X PUT localhost:5201/rooms/the-core --data-binary @examples/the-core.json -H 'Content-Type: application/json'
```

Broker address: `?mqtt=ws://host:9001` in the page URL (`?mqtt=off` turns the engine off); by default `ws://localhost:9001`
on localhost and `wss://<host>/mqtt` elsewhere. The game master uses a Claude model through OpenRouter when
`OPENROUTER_API_KEY` is set, and otherwise directly through Anthropic (`ANTHROPIC_API_KEY` or the `ant auth login` profile).
Without credentials the game runs on rules; `GameMaster:RulesOnly=true` forces the rules. As its brief the game master
uses the [room's scenario](../docs/scenario-the-core.md), so changes to the scenario change what it knows about the room at once.

A test without a window (a whole game in ~70 s, the log on standard output):

```bash
cd simulator/godot && godot --headless --path . --fixed-fps 60 --quit-after 4300 -- --autostart --speed=60
```

A frame capture with real rendering: `godot --path . -- --autostart --zoom=22 --shot=/path/frame.png`.
