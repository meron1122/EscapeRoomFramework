# The Core simulator

An isometric simulator of the room "The Core" (scenario: [docs/scenario-the-core.md](../docs/scenario-the-core.md)) in Godot 4.7,
running in the browser. Five simulated Nomads play through the game while Shadowman, the AI game master, draws a variant, gives hints
and adds twists the way the scenario describes. Without the engine a small local stand-in plays his part.

## What it is made of

| Path | What it is |
| --- | --- |
| `godot/layout.json` | the floor plan: dimensions, doors, props, puzzles and work spots; read by both Blender and Godot |
| `blender/build_assets.py` | builds the models of the rooms, props and characters → `godot/assets/*.glb` |
| `music/compose.py` | synthesises the music and effects from code → `godot/audio/*.mp3` |
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

Then `http://localhost:8060`. Controls: LMB/RMB pans, the wheel zooms, 1/2/3 jump to the rooms, F follows the team,
H hides the panels, space pauses.

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
