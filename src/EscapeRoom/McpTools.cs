using System.ComponentModel;
using System.Text.Json;
using ModelContextProtocol;
using ModelContextProtocol.Server;

namespace EscapeRoom;

/// <summary>MCP tools: lets an AI game master run games and rewire puzzles while they are being played.</summary>
[McpServerToolType]
public class McpTools(Engine engine, Mqtt mqtt, Safety safety)
{
    // Deliberately no tool changes, triggers or simulates anything in the safety layer.
    const string Live =
        " Applies to the running game at once and keeps its progress. A puzzle whose condition already holds is solved"
        + " immediately and runs its steps, which moves real hardware. Invalid changes are rejected with the reasons.";

    const string Device =
        """
         DEVICE is exactly one of:
        {"topic":"any/mqtt/topic","path":"field.sub"} (path optional; picks a field of a JSON payload; on an output wraps the value as {"field": value}),
        {"tasmota":"<topic>","path":"Button1.Action"} input from stat/<topic>/RESULT; {"tasmota":"<topic>","command":"Power1"} output to cmnd/<topic>/Power1,
        {"esphome":"<node>","entity":"switch/lock"} input <node>/switch/lock/state, output <node>/switch/lock/command,
        {"z2m":"<friendly name>","path":"contact"} input zigbee2mqtt/<name>, output zigbee2mqtt/<name>/set,
        {"camera":"<camera id>"} QR codes read by an RTSP camera (input only).
        Use list_mqtt_topics to see which devices exist and what they send.
        Devices of the venue's safety functions (emergency exits, alarms, lock power) are off-limits to games:
        outputs that would publish to them are rejected, and list_mqtt_topics marks their topics with safety=true.
        """;

    const string PuzzleFormat =
        """
         PUZZLE: {"id":"...","after":["puzzle ids that must be solved first"],"when":CONDITION,"then":[STEP,...]}.
        CONDITION: {"input":"<input id>","equals":"ON"} | {"input":"x","gt":40} | {"input":"x","lt":10} | {"input":"x"} (any value)
        | {"all":[CONDITION,...]} | {"any":[CONDITION,...]} | {"sequence":[input conditions in the required order]} (sequence only as the whole 'when').
        Values compare as case-insensitive text (true, "ON", 42). Without 'when' the puzzle is solved only manually.
        STEP: {"output":"<output id>","value":"ON" or any JSON} | {"delay":milliseconds}.
        """;

    [McpServerTool(Name = "list_rooms", ReadOnly = true), Description("Rooms with their name and whether a game is running.")]
    public Task<List<object>> ListRooms() => engine.ListRooms();

    [McpServerTool(Name = "get_room", ReadOnly = true), Description("A room's full definition: inputs, outputs, start steps and puzzles.")]
    public async Task<string> GetRoom(string roomId) => await engine.RoomJson(roomId) ?? throw NoRoom(roomId);

    [McpServerTool(Name = "save_room"), Description(
        """Create or replace a whole room: {"name":"...","inputs":{"<id>":DEVICE},"outputs":{"<id>":DEVICE},"start":[STEP,...],"puzzles":[PUZZLE,...]}. 'start' runs when a game starts, e.g. to lock everything."""
        + Device + PuzzleFormat + Live)]
    public Task<object> SaveRoom(string roomId, JsonElement room) => Save(roomId, _ => Parse<Room>(room));

    [McpServerTool(Name = "upsert_input"), Description("Add or replace an input (a sensor) of a room." + Device + Live)]
    public Task<object> UpsertInput(string roomId, string inputId, [Description("DEVICE JSON")] JsonElement device)
    {
        var io = Parse<Io>(device);
        return Save(roomId, r => Existing(r, roomId) with { Inputs = new(r!.Inputs) { [inputId] = io } });
    }

    [McpServerTool(Name = "upsert_output"), Description("Add or replace an output (a lock, relay, lamp) of a room." + Device + Live)]
    public Task<object> UpsertOutput(string roomId, string outputId, [Description("DEVICE JSON")] JsonElement device)
    {
        var io = Parse<Io>(device);
        return Save(roomId, r => Existing(r, roomId) with { Outputs = new(r!.Outputs) { [outputId] = io } });
    }

    [McpServerTool(Name = "upsert_puzzle"), Description(
        "Add a puzzle, or replace the one with the same id: this is how inputs and outputs are wired into puzzles." + PuzzleFormat + Live)]
    public Task<object> UpsertPuzzle(string roomId, [Description("PUZZLE JSON")] JsonElement puzzle)
    {
        var p = Parse<Puzzle>(puzzle);
        return Save(roomId, r =>
        {
            var room = Existing(r, roomId);
            return room with
            {
                Puzzles = room.Puzzles.Any(x => x.Id == p.Id)
                    ? room.Puzzles.Select(x => x.Id == p.Id ? p : x).ToArray()
                    : [.. room.Puzzles, p],
            };
        });
    }

    [McpServerTool(Name = "remove_from_room"), Description("Remove an input, output or puzzle. Rejected while anything still refers to it." + Live)]
    public Task<object> RemoveFromRoom(string roomId, [Description("input, output or puzzle")] string kind, string id) =>
        Save(roomId, r =>
        {
            var room = Existing(r, roomId);
            var found = kind switch
            {
                "input" => room.Inputs.ContainsKey(id),
                "output" => room.Outputs.ContainsKey(id),
                "puzzle" => room.Puzzles.Any(p => p.Id == id),
                _ => throw new McpException("kind must be input, output or puzzle"),
            };
            if (!found) throw new McpException($"Room '{roomId}' has no {kind} '{id}'");
            return kind switch
            {
                "input" => room with { Inputs = room.Inputs.Where(kv => kv.Key != id).ToDictionary() },
                "output" => room with { Outputs = room.Outputs.Where(kv => kv.Key != id).ToDictionary() },
                _ => room with { Puzzles = [.. room.Puzzles.Where(p => p.Id != id)] },
            };
        });

    [McpServerTool(Name = "game_status", ReadOnly = true), Description(
        "The room's latest game: start and end time, each puzzle's status (solved, active = waiting for its condition, locked = waiting for 'after') and the last value of every input.")]
    public async Task<object> GameStatus(string roomId) =>
        await engine.Status(roomId) ?? throw new McpException($"Room '{roomId}' has no games yet");

    [McpServerTool(Name = "start_game"), Description("Start a new game in the room, ending a running one. Runs the room's start steps.")]
    public async Task<object> StartGame(string roomId) =>
        await engine.Start(roomId) is { } session ? new { session } : throw NoRoom(roomId);

    [McpServerTool(Name = "stop_game"), Description("End the running game without solving anything.")]
    public async Task<string> StopGame(string roomId) => await engine.Stop(roomId) ? "Stopped." : throw NoGame(roomId);

    [McpServerTool(Name = "solve_puzzle"), Description(
        "Mark a puzzle solved in the running game, as a game master would. Runs its steps and solves whatever that unlocks.")]
    public async Task<object> SolvePuzzle(string roomId, string puzzleId) =>
        await engine.Solve(roomId, puzzleId) ? (await engine.Status(roomId))! : throw NoGame(roomId, $"puzzle '{puzzleId}'");

    [McpServerTool(Name = "simulate_input"), Description(
        "Feed an input value as if its sensor had sent it: to test a puzzle or stand in for a broken sensor. Values are text, e.g. \"ON\", \"false\", \"42\".")]
    public async Task<object> SimulateInput(string roomId, string inputId, string value) =>
        await engine.Inject(roomId, inputId, value) ? (await engine.Status(roomId))! : throw NoGame(roomId, $"input '{inputId}'");

    [McpServerTool(Name = "trigger_output"), Description(
        "Send a value to an output right now, game running or not: open a locker, switch a lamp. Value is text like \"ON\" or JSON like {\"state\":\"ON\"}.")]
    public async Task<string> TriggerOutput(string roomId, string outputId, JsonElement value) =>
        await engine.Trigger(roomId, outputId, value) is { } error ? throw new McpException(error) : "Sent.";

    [McpServerTool(Name = "list_mqtt_topics", ReadOnly = true), Description(
        "MQTT topics seen since the engine started, with their last payload: to discover devices and wire them as inputs or outputs. At most 200; narrow with a prefix such as zigbee2mqtt/ or stat/.")]
    public object ListMqttTopics(string? prefix = null) => mqtt.Seen
        .Where(kv => prefix is null || kv.Key.StartsWith(prefix, StringComparison.Ordinal))
        .OrderBy(kv => kv.Key, StringComparer.Ordinal)
        .Take(200)
        .Select(kv => new
        {
            topic = kv.Key,
            payload = kv.Value.Payload.Length > 300 ? kv.Value.Payload[..300] + "…" : kv.Value.Payload,
            at = kv.Value.At,
            safety = safety.Current.Protects(kv.Key),
        })
        .ToList();

    async Task<object> Save(string roomId, Func<Room?, Room> change)
    {
        var errors = await engine.SaveRoom(roomId, change);
        if (errors.Length > 0) throw new McpException("Rejected, nothing changed:\n" + string.Join("\n", errors));
        return new { saved = roomId, game = await engine.Status(roomId) };
    }

    static T Parse<T>(JsonElement json)
    {
        try
        {
            return json.Deserialize<T>(Engine.Json) ?? throw new McpException($"{typeof(T).Name} cannot be null");
        }
        catch (JsonException ex)
        {
            throw new McpException($"Invalid {typeof(T).Name}: {ex.Message}");
        }
    }

    static Room Existing(Room? room, string roomId) =>
        room ?? throw new McpException($"Room '{roomId}' does not exist; create it with save_room");

    static McpException NoRoom(string roomId) => new($"Room '{roomId}' does not exist");

    static McpException NoGame(string roomId, string? what = null) =>
        new($"No running game in room '{roomId}'" + (what is null ? "" : $", or the room has no {what}"));
}
