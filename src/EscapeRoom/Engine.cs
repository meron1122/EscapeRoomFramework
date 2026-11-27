using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Channels;
using Npgsql;

namespace EscapeRoom;

/// <summary>Runs games: routes MQTT messages to running sessions, stores every change in Postgres, fires outputs.</summary>
public class Engine(NpgsqlDataSource db, Mqtt mqtt, Safety safety, IConfiguration config, ILogger<Engine> log) : BackgroundService
{
    public const string Schema = """
        create table if not exists rooms (
            id text primary key,
            definition jsonb not null,
            updated_at timestamptz not null default now());
        -- the venue's description of the room for the game master (Markdown): story, mood, staging, pacing
        alter table rooms add column if not exists brief text;
        create table if not exists sessions (
            id uuid primary key,
            room_id text not null references rooms (id),
            definition jsonb not null,
            state jsonb not null,
            started_at timestamptz not null default now(),
            ended_at timestamptz);
        create unique index if not exists sessions_one_running_per_room on sessions (room_id) where ended_at is null;
        create table if not exists events (
            id bigserial primary key,
            session_id uuid not null references sessions (id),
            at timestamptz not null default now(),
            type text not null,
            data jsonb not null);
        create index if not exists events_by_session on events (session_id, id);
        """;

    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow, // a typo like "equal" must fail, not match any value
        RespectNullableAnnotations = true,
        RespectRequiredConstructorParameters = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    class Session(Guid id, string roomId, Room room, Game game, ILookup<string, (string Input, string? Path)> topics)
    {
        public Guid Id => id;
        public string RoomId => roomId;
        public Room Room => room;
        public Game Game => game;
        public ILookup<string, (string Input, string? Path)> Topics => topics;
        public double Speed = 1.0;  // game seconds per real second; a simulation runs faster
        public string? Nonce;  // set by whoever started the game, so a client can tell its game from an older one
        public JsonElement? Players;  // latest tracking snapshot from the room's cameras or the simulator
        public readonly List<object> Recent = [];  // last events, for the game master agent
        // shortcut: in memory, so a resumed game may fire a twist again; persist it if engine restarts mid-game matter
        public readonly HashSet<string> Fired = [];  // twists used this game
    }

    /// <summary>Raised after every logged event of a running game: (room, event type). The game master agent wakes on it.</summary>
    public event Action<string, string>? Changed;

    readonly SemaphoreSlim gate = new(1, 1); // one change at a time across all rooms; a venue's traffic is tiny
    readonly Dictionary<string, Session> running = [];
    readonly Queue<DateTimeOffset> mqttStarts = [];  // touched only by the single message loop

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            // Pick up games that were running when the process stopped.
            await using var cmd = db.Cmd("select id, room_id, definition, state from sessions where ended_at is null");
            await using var r = await cmd.ExecuteReaderAsync(ct);
            while (await r.ReadAsync(ct))
                Add(r.GetGuid(0), r.GetString(1), Parse<Room>(r.GetString(2)), Parse<GameState>(r.GetString(3)));
            log.LogInformation("Resumed {Count} running game(s)", running.Count);
        }
        finally
        {
            gate.Release();
        }
        // Own queue, so a slow database never holds up the MQTT loop that safety rules run on.
        var inbox = Channel.CreateUnbounded<(string Topic, string Payload)>();
        mqtt.Received += (topic, payload) =>
        {
            inbox.Writer.TryWrite((topic, payload));
            return Task.CompletedTask;
        };
        _ = Tick(ct);
        await foreach (var (topic, payload) in inbox.Reader.ReadAllAsync(ct)) await OnMessage(topic, payload);
    }

    /// <summary>The game clock: advances every running game, publishes its state, and ends it when time runs out.</summary>
    async Task Tick(CancellationToken ct)
    {
        var n = 0;
        while (!ct.IsCancellationRequested)
        {
            await Task.Delay(1000, ct);
            await gate.WaitAsync(ct);
            try
            {
                n++;
                foreach (var s in running.Values.ToList())
                {
                    s.Game.State.Clock += s.Speed;
                    if (s.Room.Minutes is { } m && s.Game.State.Clock >= m * 60)
                    {
                        await End(s, "timeout");
                        continue;
                    }
                    if (n % 10 == 0)
                        await db.Exec("update sessions set state = $2::jsonb where id = $1", s.Id, Serialize(s.Game.State));
                    await PublishState(s, null);
                }
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                log.LogError(ex, "Game clock tick failed");
            }
            finally
            {
                gate.Release();
            }
        }
    }

    Task PublishState(Session s, string? ending) => mqtt.Publish($"escaperoom/{s.RoomId}/state", Serialize(new
    {
        session = s.Id,
        nonce = s.Nonce,
        clock = Math.Round(s.Game.State.Clock),
        minutes = s.Room.Minutes,
        speed = s.Speed,
        ended = ending != null,
        ending,
        puzzles = s.Room.Puzzles.Select(p => new { p.Id, status = s.Game.Status(p) }),
    }), retain: true);

    /// <summary>What the game master agent sees: the clock, every puzzle with its condition, recent events and the players.</summary>
    public async Task<object?> Observe(string roomId)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s)) return null;
            return new
            {
                clockMinutes = Math.Round(s.Game.State.Clock / 60.0, 1),
                minutes = s.Room.Minutes,
                speed = s.Speed,
                puzzles = s.Room.Puzzles.Select(p => new { p.Id, p.Name, status = s.Game.Status(p), p.After, p.Ends, when = p.When }),
                inputs = s.Game.State.Inputs,
                recent = s.Recent.TakeLast(25),
                players = s.Players,
                twistsUsed = s.Fired,
            };
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task<List<object>> ListRooms()
    {
        await using var cmd = db.Cmd("""
            select id, definition->>'name',
                   exists (select 1 from sessions s where s.room_id = rooms.id and s.ended_at is null)
            from rooms order by id
            """);
        await using var r = await cmd.ExecuteReaderAsync();
        var rooms = new List<object>();
        while (await r.ReadAsync()) rooms.Add(new { id = r.GetString(0), name = r.GetString(1), running = r.GetBoolean(2) });
        return rooms;
    }

    public async Task<string?> RoomJson(string roomId) => await db.Scalar("select definition from rooms where id = $1", roomId) as string;

    public async Task<string?> Brief(string roomId) => await db.Scalar("select brief from rooms where id = $1", roomId) as string;

    public async Task<bool> SaveBrief(string roomId, string brief) =>
        await db.Scalar("update rooms set brief = $2, updated_at = now() where id = $1 returning id", roomId, brief) is not null;

    /// <summary>Rooms with a game running now.</summary>
    public async Task<string[]> Running()
    {
        await gate.WaitAsync();
        try { return [.. running.Keys]; }
        finally { gate.Release(); }
    }

    /// <summary>What a running game plays right now: the room with this game's variants and twists.</summary>
    public async Task<Room?> Playing(string roomId)
    {
        await gate.WaitAsync();
        try { return running.TryGetValue(roomId, out var s) ? s.Room : null; }
        finally { gate.Release(); }
    }

    /// <summary>
    /// Draws another variant for a running game: solutions and clues change together. Only this game changes;
    /// the stored room keeps its authored default, which the start steps' clues match.
    /// </summary>
    public async Task<(bool Ok, string Message)> Reroll(string roomId, string handle, Random rng)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s)) return (false, "no game running");
            if (s.Room.Variants?.GetValueOrDefault(handle) is not { } options) return (false, $"'{handle}' has no variants");
            var puzzles = options.SelectMany(v => v.Solutions.Keys).Distinct()
                .Select(id => s.Room.Puzzles.FirstOrDefault(p => p.Id == id)).ToList();
            if (puzzles.Contains(null)) return (false, $"'{handle}' is not in this game");
            // Players may already be reading the clue of a puzzle in play, so it keeps its solution after the first minute.
            if (s.Game.State.Clock > 60 && puzzles.Any(p => s.Game.Status(p!) != "locked")) return (false, $"'{handle}' is already in play");
            var others = options.Where(v => v.Solutions.Any(kv => Serialize(s.Room.Puzzles.First(p => p.Id == kv.Key).When!) != Serialize(kv.Value))).ToArray();
            var pick = others.Length > 0 ? others[rng.Next(others.Length)] : options[rng.Next(options.Length)];
            var changed = await Rewire(s, s.Room.WithVariant(pick));
            await Log(changed, "reroll", new { variants = handle, pick.Summary });
            _ = Run(changed, pick.Clues ?? []);
            return (true, pick.Summary ?? handle);
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Fires a prepared twist, once per game: its steps run, its puzzles join (with a drawn variant) and block.</summary>
    public async Task<(bool Ok, string Message)> Twist(string roomId, string id, Random rng)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s)) return (false, "no game running");
            if (s.Room.Twists?.GetValueOrDefault(id) is not { } twist) return (false, $"no twist '{id}'");
            if (s.Fired.Contains(id)) return (false, $"'{id}' was already used this game");
            string Status(string p) => s.Room.Puzzles.FirstOrDefault(q => q.Id == p) is { } q ? s.Game.Status(q) : "missing";
            if ((twist.After ?? []).FirstOrDefault(p => Status(p) != "solved") is { } early) return (false, $"'{id}' waits for '{early}'");
            if ((twist.Blocks ?? []).FirstOrDefault(p => Status(p) == "solved") is { } late) return (false, $"too late, '{late}' is solved");
            s.Fired.Add(id);
            var room = s.Room.WithTwist(twist);
            var steps = new List<Step>(twist.Then ?? []);
            var summary = new List<string> { id };
            var added = (twist.Adds ?? []).Select(p => p.Id).ToHashSet();
            foreach (var options in (room.Variants ?? []).Values.Where(o => o.SelectMany(v => v.Solutions.Keys).All(added.Contains)))
            {
                var pick = options[rng.Next(options.Length)];
                room = room.WithVariant(pick);
                steps.AddRange(pick.Clues ?? []);
                if (pick.Summary is { } text) summary.Add(text);
            }
            var changed = await Rewire(s, room);
            await Log(changed, "twist", new { twist = id });
            _ = Run(changed, steps);
            return (true, string.Join(", ", summary));
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Fires one of the room's cues (a hint lamp, a sign): its steps run in the running game.</summary>
    public async Task<(bool Ok, string Message)> Cue(string roomId, string id)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s)) return (false, "no game running");
            if (s.Room.Cues?.GetValueOrDefault(id) is not { } cue) return (false, $"no cue '{id}'");
            if (cue.Puzzle is { } p && (s.Room.Puzzles.FirstOrDefault(q => q.Id == p) is not { } puzzle || s.Game.Status(puzzle) != "active"))
                return (false, $"'{p}' is not active");
            await Log(s, "cue", new { cue = id });
            _ = Run(s, cue.Then);
            return (true, id);
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Switches a running game to a changed room; it keeps its progress. Call inside the gate.</summary>
    async Task<Session> Rewire(Session old, Room room)
    {
        var json = Serialize(room);
        var s = Add(old.Id, old.RoomId, room, old.Game.State);
        await db.Exec("update sessions set definition = $2::jsonb where id = $1", s.Id, json);
        await mqtt.Publish($"escaperoom/{s.RoomId}/definition", json, retain: true);
        await Log(s, "room-updated", new { });
        // New or rewired puzzles may already be satisfied by what the sensors report right now.
        await Apply(s, s.Game.Settle(DateTimeOffset.UtcNow));
        return s;
    }

    /// <summary>
    /// Validates and stores a change to the room (null = room doesn't exist yet). A running game switches
    /// to the new definition at once and keeps its progress.
    /// </summary>
    public async Task<string[]> SaveRoom(string roomId, Func<Room?, Room> change)
    {
        await gate.WaitAsync();
        try
        {
            var room = change(await RoomJson(roomId) is { } current ? Parse<Room>(current) : null);
            var errors = room.Validate().ToArray();
            if (errors.Length == 0) errors = [.. safety.Current.Conflicts(room)];
            if (errors.Length > 0) return errors;
            var json = Serialize(room);
            await db.Exec("""
                insert into rooms (id, definition) values ($1, $2::jsonb)
                on conflict (id) do update set definition = excluded.definition, updated_at = now()
                """, roomId, json);
            if (running.TryGetValue(roomId, out var old)) await Rewire(old, room);
            return [];
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Feeds a value as if the sensor sent it: to test a puzzle or stand in for a broken sensor.</summary>
    public async Task<bool> Inject(string roomId, string input, string value)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s) || !s.Room.Inputs.ContainsKey(input)) return false;
            await Feed(s, input, value, "gm-input");
            return true;
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Drives an output right now, game running or not: open a locker, test a lamp. Returns an error or null.</summary>
    public async Task<string?> Trigger(string roomId, string output, JsonElement value)
    {
        await gate.WaitAsync();
        try
        {
            var room = running.TryGetValue(roomId, out var s) ? s.Room
                : await RoomJson(roomId) is { } json ? Parse<Room>(json) : null;
            if (room is null || !room.Outputs.ContainsKey(output)) return $"Room '{roomId}' or its output '{output}' not found";
            var (topic, payload) = room.Outputs[output].Message(value);
            if (safety.Blocks(topic, $"room {roomId}, manual output {output}")) return $"{topic} belongs to a safety device; blocked and audited";
            await mqtt.Publish(topic, payload);
            if (s is not null) await Log(s, "gm-output", new { output, topic, payload });
            return null;
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task<Guid?> Start(string roomId, string? nonce = null)
    {
        await gate.WaitAsync();
        try
        {
            if (await RoomJson(roomId) is not { } json) return null;
            if (running.TryGetValue(roomId, out var old)) await End(old, "restarted");
            var s = Add(Guid.NewGuid(), roomId, Parse<Room>(json), new GameState());
            s.Nonce = nonce;
            await db.Exec("insert into sessions (id, room_id, definition, state) values ($1, $2, $3::jsonb, $4::jsonb)",
                s.Id, roomId, json, Serialize(s.Game.State));
            await mqtt.Publish($"escaperoom/{roomId}/definition", json, retain: true);
            await Log(s, "started", new { });
            _ = Run(s, s.Room.Start ?? []);
            return s.Id;
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task<bool> Stop(string roomId)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s)) return false;
            await End(s, "stopped");
            return true;
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task<bool> Solve(string roomId, string puzzleId)
    {
        await gate.WaitAsync();
        try
        {
            if (!running.TryGetValue(roomId, out var s) || s.Room.Puzzles.All(p => p.Id != puzzleId)) return false;
            await Log(s, "gm-solve", new { puzzle = puzzleId });
            await Apply(s, s.Game.Solve(puzzleId, DateTimeOffset.UtcNow));
            return true;
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>The room's latest game, running or ended.</summary>
    public async Task<object?> Status(string roomId)
    {
        await using var cmd = db.Cmd(
            "select id, definition, state, started_at, ended_at from sessions where room_id = $1 order by started_at desc limit 1", roomId);
        await using var r = await cmd.ExecuteReaderAsync();
        if (!await r.ReadAsync()) return null;
        var room = Parse<Room>(r.GetString(1));
        var game = new Game(room, Parse<GameState>(r.GetString(2)));
        return new
        {
            id = r.GetGuid(0),
            startedAt = r.GetDateTime(3),
            endedAt = r.IsDBNull(4) ? (DateTime?)null : r.GetDateTime(4),
            puzzles = room.Puzzles.Select(p => new { p.Id, status = game.Status(p) }),
            inputs = game.State.Inputs,
        };
    }

    async Task OnMessage(string topic, string payload)
    {
        // escaperoom/<room>/control {"command":"start"} starts a game; the simulator uses it instead of the HTTP API.
        if (topic.StartsWith("escaperoom/") && topic.EndsWith("/control") && Payload.Extract(payload, "command") == "start")
        {
            // Anyone who reaches the broker can ask for a game, and each game costs model and voice calls.
            // Limits:GameStartsPerHour caps that on a public demo (0 = no cap; the HTTP API and MCP are not capped).
            var cap = config.GetValue("Limits:GameStartsPerHour", 0);
            var now = DateTimeOffset.UtcNow;
            while (mqttStarts.TryPeek(out var at) && now - at > TimeSpan.FromHours(1)) mqttStarts.Dequeue();
            if (cap > 0 && mqttStarts.Count >= cap)
            {
                log.LogWarning("Start of {Topic} refused: {Cap} games per hour already started over MQTT", topic, cap);
                return;
            }
            mqttStarts.Enqueue(now);
            await Start(topic.Split('/')[1], Payload.Extract(payload, "nonce"));
            return;
        }
        await gate.WaitAsync();
        try
        {
            foreach (var s in running.Values.ToList())
            {
                if (topic == $"escaperoom/{s.RoomId}/players")
                {
                    s.Players = JsonSerializer.Deserialize<JsonElement>(payload);
                    continue;
                }
                if (topic == $"escaperoom/{s.RoomId}/control" && Payload.Extract(payload, "speed") is { } sp && double.TryParse(sp,
                        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var speed))
                {
                    s.Speed = Math.Clamp(speed, 0.0, 120.0);
                    continue;
                }
                foreach (var (input, path) in s.Topics[topic])
                    if (Payload.Extract(payload, path) is { } value && running.ContainsKey(s.RoomId))
                        await Feed(s, input, value, "input");
            }
        }
        catch (Exception ex)
        {
            log.LogError(ex, "Handling {Topic} failed", topic);
        }
        finally
        {
            gate.Release();
        }
    }

    async Task Feed(Session s, string input, string value, string eventType)
    {
        await Log(s, eventType, new { input, value });
        await Apply(s, s.Game.Input(input, value, DateTimeOffset.UtcNow));
    }

    async Task Apply(Session s, List<Puzzle> solved)
    {
        foreach (var p in solved) await Log(s, "solved", new { puzzle = p.Id });
        await db.Exec("update sessions set state = $2::jsonb where id = $1", s.Id, Serialize(s.Game.State));
        _ = Run(s, solved.SelectMany(p => p.Then ?? []).ToList());
        if (s.Game.Finished) await End(s, "finished", s.Game.Ending);
        else await PublishState(s, null);
    }

    async Task End(Session s, string reason, string? ending = null)
    {
        running.Remove(s.RoomId);
        await db.Exec("update sessions set ended_at = now(), state = $2::jsonb where id = $1", s.Id, Serialize(s.Game.State));
        await Log(s, reason, new { ending });
        await PublishState(s, ending ?? reason);
    }

    /// <summary>Runs outside the gate so delays (e.g. pulsing a solenoid) don't hold up other rooms.</summary>
    async Task Run(Session s, IEnumerable<Step> steps)
    {
        foreach (var step in steps)
            try
            {
                if (step.Delay is { } ms)
                {
                    await Task.Delay(ms);
                    continue;
                }
                var (topic, payload) = s.Room.Outputs[step.Output!].Message(step.Value!.Value);
                if (safety.Blocks(topic, $"room {s.RoomId}, output {step.Output}")) continue;
                await mqtt.Publish(topic, payload);
                await Log(s, "output", new { output = step.Output, topic, payload });
            }
            catch (Exception ex)
            {
                log.LogError(ex, "Room {Room}: step {Step} failed", s.RoomId, step);
            }
    }

    Session Add(Guid id, string roomId, Room room, GameState state)
    {
        var topics = room.Inputs
            .Select(kv => (Id: kv.Key, Io: kv.Value.AsInput()))
            .ToLookup(x => x.Io.Topic, x => (x.Id, x.Io.Path));
        var s = new Session(id, roomId, room, new Game(room, state), topics);
        if (running.TryGetValue(roomId, out var previous) && previous.Id == id)
        {
            // A live room change keeps the game's speed, players and history.
            s.Speed = previous.Speed;
            s.Nonce = previous.Nonce;
            s.Players = previous.Players;
            s.Recent.AddRange(previous.Recent);
            s.Fired.UnionWith(previous.Fired);
        }
        return running[roomId] = s;
    }

    async Task Log(Session s, string type, object data)
    {
        await db.Exec("insert into events (session_id, type, data) values ($1, $2, $3::jsonb)", s.Id, type, Serialize(data));
        s.Recent.Add(new { minute = Math.Round(s.Game.State.Clock / 60.0, 1), type, data });
        if (s.Recent.Count > 60) s.Recent.RemoveRange(0, s.Recent.Count - 60);
        Changed?.Invoke(s.RoomId, type);
    }

    static T Parse<T>(string json) => JsonSerializer.Deserialize<T>(json, Json)!;
    static string Serialize(object value) => JsonSerializer.Serialize(value, Json);
}

static class Sql
{
    public static NpgsqlCommand Cmd(this NpgsqlDataSource db, string sql, params object?[] args)
    {
        var cmd = db.CreateCommand(sql);
        foreach (var a in args) cmd.Parameters.Add(new NpgsqlParameter { Value = a ?? DBNull.Value });
        return cmd;
    }

    public static async Task Exec(this NpgsqlDataSource db, string sql, params object?[] args)
    {
        await using var cmd = db.Cmd(sql, args);
        await cmd.ExecuteNonQueryAsync();
    }

    public static async Task<object?> Scalar(this NpgsqlDataSource db, string sql, params object?[] args)
    {
        await using var cmd = db.Cmd(sql, args);
        return await cmd.ExecuteScalarAsync();
    }
}
