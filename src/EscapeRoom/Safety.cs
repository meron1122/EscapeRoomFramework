using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace EscapeRoom;

/// <summary>Life-safety devices and rules for the whole venue (emergency buttons, smoke detectors, lock power, emergency light).</summary>
public record SafetyConfig(Dictionary<string, Io> Inputs, Dictionary<string, Io> Outputs, SafetyRule[] Rules)
{
    public static readonly SafetyConfig Empty = new([], [], []);

    public IEnumerable<string> Validate() =>
        new Room("safety", Inputs, Outputs, [.. Rules.Select(r => new Puzzle(r.Id, r.When, r.Then))]).Validate()
            .Select(e => e.Replace("puzzle '", "rule '"))
            .Concat(Rules.Where(r => r.When.Sequence is not null).Select(r => $"rule '{r.Id}': sequence is not allowed in safety rules"));

    public bool Protects(string topic) => Inputs.Values.Concat(Outputs.Values).Any(io => io.Owns(topic));

    /// <summary>Game outputs that would reach a safety device.</summary>
    public IEnumerable<string> Conflicts(Room room) =>
        from o in room.Outputs
        let topic = o.Value.AsOutput().Topic
        where Protects(topic)
        select $"output '{o.Key}' publishes to {topic}, which belongs to a safety device; games cannot drive safety devices";
}

public record SafetyRule(string Id, Condition When, Step[] Then);

public record SafetyChange(string Reason, SafetyConfig Config);

/// <summary>
/// The safety layer. Separate from games: its rules run with or without a game, games and MCP cannot publish
/// to its devices, and its configuration changes only with an operator token. Every change, attempt and
/// activation goes to the append-only safety_log.
/// </summary>
public class Safety(NpgsqlDataSource db, Mqtt mqtt, IConfiguration config, ILogger<Safety> log)
{
    public const string Schema = """
        create table if not exists safety_log (
            id bigserial primary key,
            at timestamptz not null default now(),
            type text not null,
            actor text,
            data jsonb not null);
        create index if not exists safety_log_by_type on safety_log (type, id);
        create or replace function safety_log_append_only() returns trigger language plpgsql as
            $$ begin raise exception 'safety_log is append-only'; end $$;
        create or replace trigger safety_log_append_only before update or delete or truncate on safety_log
            for each statement execute function safety_log_append_only();
        """;

    volatile SafetyConfig current = SafetyConfig.Empty;
    readonly ConcurrentDictionary<string, string> inputs = new();
    readonly SemaphoreSlim changes = new(1, 1);

    public SafetyConfig Current => current;

    /// <summary>Loads the last audited configuration and starts listening. Runs before any game can publish.</summary>
    public async Task Start()
    {
        if (await db.Scalar("select data->'config' from safety_log where type = 'config' order by id desc limit 1") is string json)
            current = JsonSerializer.Deserialize<SafetyConfig>(json, Engine.Json)!;
        mqtt.Received += OnMessage;
        log.LogInformation("Safety: {Rules} rule(s) active", current.Rules.Length);
    }

    /// <summary>The only way to change safety. Authorization comes first so that every attempt is audited.</summary>
    public async Task<(int Status, string[] Errors)> Change(string? token, string? ip, Stream body)
    {
        var who = Operator(token);
        if (who is null)
        {
            await Audit("denied", null, new { ip });
            return (401, ["missing or invalid X-Safety-Token"]);
        }
        SafetyChange? change;
        try
        {
            change = await JsonSerializer.DeserializeAsync<SafetyChange>(body, Engine.Json);
        }
        catch (JsonException ex)
        {
            await Audit("rejected", who, new { ip, errors = new[] { ex.Message } });
            return (400, [ex.Message]);
        }
        string[] errors = change is null ? ["empty body"]
            : string.IsNullOrWhiteSpace(change.Reason) ? ["reason is required"]
            : [.. change.Config.Validate()];
        if (errors.Length > 0)
        {
            await Audit("rejected", who, new { ip, errors });
            return (400, errors);
        }

        await changes.WaitAsync();
        try
        {
            await Audit("config", who, new { ip, change!.Reason, change.Config }); // audited before it takes effect
            current = change.Config;
            return (200, []);
        }
        finally
        {
            changes.Release();
        }
    }

    /// <summary>Last check before every game or MCP publish: safety devices are never driven by a game.</summary>
    public bool Blocks(string topic, string source)
    {
        if (!current.Protects(topic)) return false;
        log.LogWarning("Blocked {Source} from publishing to safety topic {Topic}", source, topic);
        _ = TryAudit("blocked", new { source, topic });
        return true;
    }

    public async Task<List<object>> History(int limit = 200)
    {
        await using var cmd = db.Cmd("select id, at, type, actor, data from safety_log order by id desc limit $1", limit);
        await using var r = await cmd.ExecuteReaderAsync();
        var rows = new List<object>();
        while (await r.ReadAsync())
            rows.Add(new
            {
                id = r.GetInt64(0), at = r.GetDateTime(1), type = r.GetString(2),
                actor = r.IsDBNull(3) ? null : r.GetString(3),
                data = JsonSerializer.Deserialize<JsonElement>(r.GetString(4)),
            });
        return rows;
    }

    /// <summary>Runs on the MQTT loop: in-memory only, outputs and audit go async so a slow database can't delay them.</summary>
    Task OnMessage(string topic, string payload)
    {
        var cfg = current;
        var changed = false;
        foreach (var (id, io) in cfg.Inputs)
        {
            var (t, path) = io.AsInput();
            if (t == topic && Payload.Extract(payload, path) is { } value)
            {
                inputs[id] = value;
                changed = true;
            }
        }
        // Level-triggered: while a rule holds, every update re-asserts its outputs (e.g. a lock controller that rebooted).
        if (changed)
            foreach (var rule in cfg.Rules.Where(r => r.When.Holds(inputs)))
                _ = Fire(cfg, rule);
        return Task.CompletedTask;
    }

    async Task Fire(SafetyConfig cfg, SafetyRule rule)
    {
        log.LogWarning("Safety rule {Rule} fired", rule.Id);
        await TryAudit("fired", new { rule = rule.Id, inputs = inputs.ToDictionary() });
        foreach (var step in rule.Then)
            try
            {
                if (step.Delay is { } ms)
                {
                    await Task.Delay(ms);
                    continue;
                }
                var (topic, payload) = cfg.Outputs[step.Output!].Message(step.Value!.Value);
                await mqtt.Publish(topic, payload);
                await TryAudit("output", new { rule = rule.Id, output = step.Output, topic, payload });
            }
            catch (Exception ex)
            {
                log.LogCritical(ex, "Safety rule {Rule}: step {Step} failed", rule.Id, step);
                await TryAudit("failed", new { rule = rule.Id, output = step.Output, error = ex.Message });
            }
    }

    /// <summary>Operators are configured as Safety:Operators:{name} = SHA-256 hex of their token; the token itself is never stored.</summary>
    string? Operator(string? token)
    {
        if (string.IsNullOrEmpty(token)) return null;
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(token));
        foreach (var op in config.GetSection("Safety:Operators").GetChildren())
            if (op.Value is { } hex && CryptographicOperations.FixedTimeEquals(hash, Convert.FromHexString(hex)))
                return op.Key;
        return null;
    }

    Task Audit(string type, string? actor, object data) =>
        db.Exec("insert into safety_log (type, actor, data) values ($1, $2, $3::jsonb)", type, actor, JsonSerializer.Serialize(data, Engine.Json));

    async Task TryAudit(string type, object data)
    {
        try
        {
            await Audit(type, null, data);
        }
        catch (Exception ex)
        {
            log.LogCritical(ex, "Safety audit write failed: {Type} {Data}", type, JsonSerializer.Serialize(data));
        }
    }
}
