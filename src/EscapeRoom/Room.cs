using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace EscapeRoom;

/// <summary>A room: its hardware (inputs and outputs as MQTT endpoints) and the puzzle graph that drives it.</summary>
public record Room(
    string Name,
    Dictionary<string, Io> Inputs,
    Dictionary<string, Io> Outputs,
    Puzzle[] Puzzles,
    Step[]? Start = null)
{
    public IEnumerable<string> Validate()
    {
        foreach (var (id, io) in Inputs)
            if (io.Problem(input: true) is { } p) yield return $"input '{id}': {p}";
        foreach (var (id, io) in Outputs)
            if (io.Problem(input: false) is { } p) yield return $"output '{id}': {p}";
        foreach (var e in CheckSteps(Start)) yield return $"start: {e}";

        var ids = new HashSet<string>();
        foreach (var p in Puzzles)
        {
            if (!ids.Add(p.Id)) yield return $"puzzle '{p.Id}': duplicate id";
            foreach (var a in p.After ?? [])
                if (Puzzles.All(q => q.Id != a)) yield return $"puzzle '{p.Id}': unknown puzzle '{a}' in after";
            if (p.When is { } w)
                foreach (var e in CheckCondition(w, topLevel: true)) yield return $"puzzle '{p.Id}': {e}";
            foreach (var e in CheckSteps(p.Then)) yield return $"puzzle '{p.Id}': {e}";
        }

        var reachable = new HashSet<string>();
        for (var grew = true; grew;)
        {
            grew = false;
            foreach (var p in Puzzles)
                if ((p.After ?? []).All(reachable.Contains)) grew |= reachable.Add(p.Id);
        }
        var stuck = Puzzles.Where(p => !reachable.Contains(p.Id)).Select(p => p.Id).ToArray();
        if (stuck.Length > 0) yield return $"puzzles can never become active (cycle in after): {string.Join(", ", stuck)}";
    }

    IEnumerable<string> CheckCondition(Condition c, bool topLevel)
    {
        if (new object?[] { c.Input, c.All, c.Any, c.Sequence }.Count(x => x is not null) != 1)
        {
            yield return "condition needs exactly one of input, all, any, sequence";
            yield break;
        }
        if (c.Input is { } i && !Inputs.ContainsKey(i)) yield return $"unknown input '{i}'";
        if (c.Input is null && (c.Is is not null || c.Gt is not null || c.Lt is not null)) yield return "equals/gt/lt need an input";
        if ((c.All ?? c.Any ?? c.Sequence) is { Length: 0 }) yield return "all/any/sequence cannot be empty";
        if (c.Sequence is { } seq)
        {
            if (!topLevel) yield return "sequence must be the whole 'when' of a puzzle";
            if (seq.Any(s => s.Input is null)) yield return "sequence steps must be input conditions";
        }
        foreach (var child in (c.All ?? []).Concat(c.Any ?? []).Concat(c.Sequence ?? []))
            foreach (var e in CheckCondition(child, topLevel: false)) yield return e;
    }

    IEnumerable<string> CheckSteps(Step[]? steps)
    {
        foreach (var s in steps ?? [])
        {
            if (s.Delay is not null)
            {
                if (s.Output is not null || s.Value is not null || s.Delay < 0) yield return "a delay step takes only a non-negative delay";
            }
            else if (s.Output is null || s.Value is null) yield return "a step needs output and value, or delay";
            else if (!Outputs.ContainsKey(s.Output)) yield return $"unknown output '{s.Output}'";
        }
    }
}

/// <summary>
/// A device endpoint. Any MQTT device works with <c>topic</c> (+ <c>path</c> into a JSON payload);
/// presets know the topic layout of Tasmota, ESPHome, zigbee2mqtt and our RTSP QR cameras.
/// </summary>
public record Io(
    string? Topic = null, string? Path = null,
    string? Tasmota = null, string? Command = null,
    string? Esphome = null, string? Entity = null,
    string? Z2m = null,
    string? Camera = null)
{
    public (string Topic, string? Path) AsInput() => this switch
    {
        { Topic: not null } => (Topic, Path),
        { Tasmota: not null } => ($"stat/{Tasmota}/RESULT", Path),
        { Esphome: not null } => ($"{Esphome}/{Entity}/state", Path),
        { Z2m: not null } => ($"zigbee2mqtt/{Z2m}", Path),
        _ => (QrCamera.TopicFor(Camera!), null),
    };

    public (string Topic, string? Path) AsOutput() => this switch
    {
        { Topic: not null } => (Topic, Path),
        { Tasmota: not null } => ($"cmnd/{Tasmota}/{Command ?? "Power1"}", Path),
        { Esphome: not null } => ($"{Esphome}/{Entity}/command", Path),
        _ => ($"zigbee2mqtt/{Z2m}/set", Path),
    };

    public (string Topic, string Payload) Message(JsonElement value)
    {
        var (topic, path) = AsOutput();
        return (topic, Payload.ForOutput(value, path));
    }

    /// <summary>True when publishing to <paramref name="topic"/> could command, reconfigure or impersonate this device.</summary>
    public bool Owns(string topic) => this switch
    {
        { Topic: { } t } => topic == t,
        // cmnd/tasmotas/ is Tasmota's broadcast group topic, it reaches every device
        { Tasmota: { } d } => topic.StartsWith("cmnd/tasmotas/") || topic.StartsWith($"cmnd/{d}/")
            || topic.StartsWith($"stat/{d}/") || topic.StartsWith($"tele/{d}/"),
        { Esphome: { } d } => topic.StartsWith($"{d}/"),
        // zigbee2mqtt/bridge/ can remove, rename or re-flash any device
        { Z2m: { } d } => topic == $"zigbee2mqtt/{d}" || topic.StartsWith($"zigbee2mqtt/{d}/") || topic.StartsWith("zigbee2mqtt/bridge/"),
        _ => topic == QrCamera.TopicFor(Camera!),
    };

    internal string? Problem(bool input)
    {
        if (new[] { Topic, Tasmota, Esphome, Z2m, Camera }.Count(x => x is not null) != 1)
            return "set exactly one of topic, tasmota, esphome, z2m, camera";
        if (Esphome is not null && Entity is null) return "esphome needs entity, e.g. switch/maglock";
        if (Camera is not null && !input) return "a camera can only be an input";
        return null;
    }
}

public record Puzzle(string Id, Condition? When = null, Step[]? Then = null, string[]? After = null);

/// <summary>Either a test on one input (equals/gt/lt, none = any value) or all/any/sequence of conditions.</summary>
public record Condition(
    string? Input = null,
    [property: JsonPropertyName("equals")] JsonElement? Is = null,
    double? Gt = null,
    double? Lt = null,
    Condition[]? All = null,
    Condition[]? Any = null,
    Condition[]? Sequence = null)
{
    /// <summary>Evaluates against the last value of each input. A sequence never holds here; it needs event history.</summary>
    public bool Holds(IReadOnlyDictionary<string, string> inputs) => this switch
    {
        { All: { } all } => all.All(x => x.Holds(inputs)),
        { Any: { } any } => any.Any(x => x.Holds(inputs)),
        _ => Input is { } i && inputs.TryGetValue(i, out var v) && Matches(v),
    };

    public bool Matches(string value) =>
        (Is is not { } e || string.Equals(Payload.Text(e), value, StringComparison.OrdinalIgnoreCase))
        && (Gt is not { } gt || Number(value) > gt)
        && (Lt is not { } lt || Number(value) < lt);

    static double Number(string s) =>
        double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : double.NaN;
}

/// <summary>Publish <c>value</c> to an output, or wait <c>delay</c> milliseconds.</summary>
public record Step(string? Output = null, JsonElement? Value = null, int? Delay = null);

public static class Payload
{
    /// <summary>JSON strings unquoted, anything else as raw JSON (true, 42, {...}).</summary>
    public static string Text(JsonElement e) => e.ValueKind == JsonValueKind.String ? e.GetString()! : e.GetRawText();

    /// <summary>The whole payload, or the field at a dot path of a JSON payload; null when it isn't there.</summary>
    public static string? Extract(string payload, string? path)
    {
        if (path is null) return payload.Trim();
        try
        {
            using var doc = JsonDocument.Parse(payload);
            var e = doc.RootElement;
            foreach (var key in path.Split('.'))
                if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(key, out e)) return null;
            return Text(e);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>The value as is, or wrapped as {"path": value} like zigbee2mqtt expects.</summary>
    public static string ForOutput(JsonElement value, string? path) =>
        path is null ? Text(value) : JsonSerializer.Serialize(new Dictionary<string, JsonElement> { [path] = value });
}
