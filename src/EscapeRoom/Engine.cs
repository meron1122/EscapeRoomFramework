using System.Text.Json;
using System.Text.Json.Serialization;
using Npgsql;

namespace EscapeRoom;

/// <summary>Runs games: routes MQTT messages to running sessions, stores every change in Postgres, fires outputs.</summary>
public class Engine(NpgsqlDataSource db, Mqtt mqtt, ILogger<Engine> log) : BackgroundService
{
    public const string Schema = """
        create table if not exists rooms (
            id text primary key,
            definition jsonb not null,
            updated_at timestamptz not null default now());
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

    record Session(Guid Id, string RoomId, Room Room, Game Game, ILookup<string, (string Input, string? Path)> Topics);

    readonly SemaphoreSlim gate = new(1, 1); // one change at a time across all rooms; a venue's traffic is tiny
    readonly Dictionary<string, Session> running = [];

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
        mqtt.Received += OnMessage;
    }

    public async Task<Guid?> Start(string roomId)
    {
        await gate.WaitAsync();
        try
        {
            if (await db.Scalar("select definition from rooms where id = $1", roomId) is not string json) return null;
            if (running.TryGetValue(roomId, out var old)) await End(old, "restarted");
            var s = Add(Guid.NewGuid(), roomId, Parse<Room>(json), new GameState());
            await db.Exec("insert into sessions (id, room_id, definition, state) values ($1, $2, $3::jsonb, $4::jsonb)",
                s.Id, roomId, json, Serialize(s.Game.State));
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
        await gate.WaitAsync();
        try
        {
            foreach (var s in running.Values.ToList())
                foreach (var (input, path) in s.Topics[topic])
                    if (Payload.Extract(payload, path) is { } value && running.ContainsKey(s.RoomId))
                    {
                        await Log(s, "input", new { input, value });
                        await Apply(s, s.Game.Input(input, value, DateTimeOffset.UtcNow));
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

    async Task Apply(Session s, List<Puzzle> solved)
    {
        foreach (var p in solved) await Log(s, "solved", new { puzzle = p.Id });
        await db.Exec("update sessions set state = $2::jsonb where id = $1", s.Id, Serialize(s.Game.State));
        _ = Run(s, solved.SelectMany(p => p.Then ?? []).ToList());
        if (s.Game.Finished) await End(s, "finished");
    }

    async Task End(Session s, string reason)
    {
        running.Remove(s.RoomId);
        await db.Exec("update sessions set ended_at = now() where id = $1", s.Id);
        await Log(s, reason, new { });
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
                var (topic, path) = s.Room.Outputs[step.Output!].AsOutput();
                var payload = Payload.ForOutput(step.Value!.Value, path);
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
        return running[roomId] = new Session(id, roomId, room, new Game(room, state), topics);
    }

    Task Log(Session s, string type, object data) =>
        db.Exec("insert into events (session_id, type, data) values ($1, $2, $3::jsonb)", s.Id, type, Serialize(data));

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
