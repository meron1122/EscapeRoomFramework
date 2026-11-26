using System.Text.Json;
using System.Threading.Channels;
using Anthropic;
using Anthropic.Models.Beta;
using Anthropic.Models.Beta.Messages;

namespace EscapeRoom;

/// <summary>
/// The game master: a Claude model that watches the game and every player, then acts through the engine: it voices
/// ZETA and Kruk, gives hints, triggers twists, rerolls and rewires puzzles. Every change still goes through the
/// engine's validation, and the safety layer is out of its reach. Without credentials it falls back to simple rules.
/// </summary>
public class GameMaster(Engine engine, IConfiguration config, ILogger<GameMaster> log) : BackgroundService
{
    const string Prompt = """
        You are the game master of "Jądro", a 60-minute cyberpunk escape room for 3-6 players (a team of Nomads breaking
        into Korporacja Z to blow up its server core). You run the game live: you watch every player and decide what
        happens next. You also voice two characters:
        - ZETA, the corporation's AI and the antagonist. Polite like a receptionist in the lobby (hol), mocking on the lab
          floor (pietro), tempting in the core (jadro). She comments on what players just did. She never lies about puzzles.
        - KRUK, the Nomads' ally on the radio outside. Short, street-wise, on the players' side. Kruk gives the hints.

        Your goal: a tense, fun game that ends dramatically close to the 60-minute mark. The plan: the lobby is done by
        minute 12, the lab floor by minute 37, the core by minute 52.

        Every ~40 seconds and on key events you get JSON with: the clock, each puzzle (status: locked, active or solved;
        `when` is its solution), the latest sensor values (wrong attempts included), recent events, the players (where
        each one is, what they do, how long without progress) and your own recent decisions.

        How to act:
        - First write one short sentence in Polish: your read of the situation. Then call 0-3 tools. Doing nothing is
          right when the team is flowing; don't talk just to talk.
        - At the start of a game ("reason": "started") call reroll with every rerollable puzzle: this team gets its own
          variant. Never reroll a puzzle the players are already working on.
        - Hint ladder when the team is behind plan in its current room: about 3 minutes behind, Kruk nudges; about
          6 minutes behind, a direct hint (level 2); about 10 minutes behind or when they are clearly stuck, level 3 or
          `solve` with Kruk "hacking" it. Prefer addressing the stuck player by name.
        - When the team is 5+ minutes ahead of plan, raise the tension with one twist per room: patrol or blackout on
          the lab floor; lockdown only after `skanery` is solved and before `firewall`. Announce it as ZETA.
        - After `uzbrojenie` is solved, ZETA offers the deal: give up the cores for a lifetime premium subscription.
        - Every line players hear is Polish, under 20 words, in character. Never repeat a recent line. Never reveal a
          solution outright except in a level 3 hint.
        - Exits and emergency systems are not yours: you cannot lock or unlock them, and you never pretend you can.
        """;

    static readonly string[] Speakers = ["ZETA", "KRUK"];
    static readonly string[] Twists = ["patrol", "blackout", "lockdown"];

    readonly Random rng = new();
    readonly List<string> memory = [];
    double lastSolveClock;
    int rulesHintLevel;

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (!config.GetValue("GameMaster:Enabled", true)) return;
        var room = config["GameMaster:Room"] ?? "jadro";
        var gameSeconds = config.GetValue("GameMaster:IntervalSeconds", 40.0);
        var speed = 1.0;
        var client = Client();
        log.LogInformation("Game master for {Room}: {Mode}", room, client is null ? "rules only" : Model);
        var wake = Channel.CreateBounded<string>(new BoundedChannelOptions(16) { FullMode = BoundedChannelFullMode.DropOldest });
        engine.Changed += (r, type) =>
        {
            if (r == room && type is "started" or "solved") wake.Writer.TryWrite(type);
        };
        while (!ct.IsCancellationRequested)
        {
            var reason = "tick";
            using (var wait = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                // The interval is in game time: a simulation at 20x checks in more often in real time, but never
                // more than every 6 s, which keeps model calls affordable.
                wait.CancelAfter(TimeSpan.FromSeconds(Math.Max(6.0, gameSeconds / speed)));
                try { reason = await wake.Reader.ReadAsync(wait.Token); }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested) { }
            }
            await Task.Delay(TimeSpan.FromSeconds(2), ct);  // let a burst of events settle into one decision
            while (wake.Reader.TryRead(out var more))
                if (more == "started") reason = more;
            if (reason == "started")
            {
                memory.Clear();
                lastSolveClock = 0;
                rulesHintLevel = 0;
            }
            if (await engine.Observe(room) is not { } seen) continue;
            var observation = JsonSerializer.SerializeToElement(seen, Engine.Json);
            speed = Math.Max(1.0, observation.GetProperty("speed").GetDouble());
            try
            {
                if (client is null) await Rules(room, reason, observation);
                else await Decide(client, room, reason, observation, ct);
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                if (ex is Anthropic.Exceptions.AnthropicUnauthorizedException or Anthropic.Exceptions.AnthropicForbiddenException
                    || ex.Message.Contains("credential", StringComparison.OrdinalIgnoreCase) || ex.Message.Contains("api key", StringComparison.OrdinalIgnoreCase))
                {
                    log.LogWarning("No working model credentials ({Error}); the game master runs on rules", ex.Message);
                    client = null;
                }
                else log.LogError(ex, "Game master decision failed; using rules this time");
                await Rules(room, reason, observation);
            }
        }
    }

    string Model => config["GameMaster:Model"] ?? "claude-opus-5-5";

    AnthropicClient? Client()
    {
        if (config.GetValue("GameMaster:RulesOnly", false)) return null;
        // On a Sprite the key lives in a Connector: BaseUrl points at the gateway and the key here is only a placeholder.
        // Elsewhere the SDK finds credentials itself (ANTHROPIC_API_KEY or an `ant auth login` profile).
        var baseUrl = config["GameMaster:BaseUrl"];
        if (string.IsNullOrEmpty(baseUrl)) return new AnthropicClient();
        var key = Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY");
        return new AnthropicClient { ApiKey = string.IsNullOrEmpty(key) ? "injected-by-connector" : key, BaseUrl = baseUrl };
    }

    async Task Decide(AnthropicClient client, string room, string reason, JsonElement observation, CancellationToken ct)
    {
        var input = JsonSerializer.Serialize(new { reason, game = observation, yourRecentDecisions = memory.TakeLast(6) }, Engine.Json);
        var response = await client.Beta.Messages.Create(new MessageCreateParams
        {
            Model = Model,
            MaxTokens = 4000,
            Betas = [AnthropicBeta.ServerSideFallback2026_07_01],
            Fallbacks = new Default(),
            OutputConfig = new BetaOutputConfig { Effort = (config["GameMaster:Effort"] ?? "low") switch
            {
                "medium" => Effort.Medium, "high" => Effort.High, _ => Effort.Low,
            } },
            System = new List<BetaTextBlockParam> { new() { Text = Prompt, CacheControl = new BetaCacheControlEphemeral() } },
            Tools = Tools(),
            Messages = [new BetaUserMessageParam { Content = input }],
        }, cancellationToken: ct);
        if (response.StopReason == "refusal")
        {
            log.LogWarning("Model refused the decision; using rules");
            await Rules(room, reason, observation);
            return;
        }
        var thought = string.Join(" ", response.Content.Select(b => b.Value).OfType<BetaTextBlock>().Select(t => t.Text)).Trim();
        var done = new List<string>();
        foreach (var block in response.Content)
            if (block.TryPickToolUse(out BetaToolUseBlock? use))
                done.Add(await Act(room, use.Name, JsonSerializer.SerializeToElement(use.Input), observation));
        await Report(room, thought, done, "model");
    }

    static List<BetaToolUnion> Tools()
    {
        static JsonElement S(object o) => JsonSerializer.SerializeToElement(o);
        var speaker = S(new { type = "string", @enum = Speakers });
        var text = S(new { type = "string", description = "What the character says, in Polish, under 20 words." });
        var to = S(new { type = "string", description = "Optional: the name of the player this is addressed to." });
        var puzzle = S(new { type = "string", description = "Puzzle id from the observation." });
        return
        [
            new BetaTool { Name = "say", Description = "Speak to the players as ZETA or KRUK. ZETA's lines also appear on her console.",
                InputSchema = new() { Properties = new Dictionary<string, JsonElement> { ["speaker"] = speaker, ["text"] = text, ["to"] = to }, Required = ["speaker", "text"] } },
            new BetaTool { Name = "hint", Description = "Kruk gives a hint for one puzzle. Level 1 nudges, 2 points at the clue, 3 nearly gives the answer. Speeds the team up on that puzzle.",
                InputSchema = new() { Properties = new Dictionary<string, JsonElement> { ["puzzle"] = puzzle, ["level"] = S(new { type = "integer", minimum = 1, maximum = 3 }), ["text"] = text, ["to"] = to }, Required = ["puzzle", "level", "text"] } },
            new BetaTool { Name = "twist", Description = "A prepared twist: patrol (players freeze for 20 s), blackout (60 s of UV light, slower work), lockdown (ZETA relocks the stairs door; a 3-button panel puzzle appears and the firewall waits for it).",
                InputSchema = new() { Properties = new Dictionary<string, JsonElement> { ["kind"] = S(new { type = "string", @enum = Twists }), ["why"] = S(new { type = "string" }) }, Required = ["kind", "why"] } },
            new BetaTool { Name = "reroll", Description = "Draw a new variant for puzzles: their solution and their clue change together. Only for puzzles nobody works on yet.",
                InputSchema = new() { Properties = new Dictionary<string, JsonElement> { ["puzzles"] = S(new { type = "array", items = new { type = "string", @enum = Variants.Rerollable } }) }, Required = ["puzzles"] } },
            new BetaTool { Name = "solve", Description = "Mark a puzzle solved (Kruk hacks it). Last resort for a team that is truly stuck.",
                InputSchema = new() { Properties = new Dictionary<string, JsonElement> { ["puzzle"] = puzzle, ["why"] = S(new { type = "string" }) }, Required = ["puzzle", "why"] } },
        ];
    }

    /// <summary>Runs one tool call. The input comes from a model, so every field is checked before anything happens.</summary>
    async Task<string> Act(string room, string tool, JsonElement input, JsonElement observation)
    {
        string? Str(string k) => input.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        string Short(string? s) => (s ?? "").Trim() is var t && t.Length > 220 ? t[..220] : (s ?? "").Trim();
        var statuses = observation.GetProperty("puzzles").EnumerateArray()
            .ToDictionary(p => p.GetProperty("id").GetString()!, p => p.GetProperty("status").GetString()!);
        switch (tool)
        {
            case "say":
            {
                var who = Str("speaker");
                var text = Short(Str("text"));
                if (who is null || !Speakers.Contains(who) || text == "") return "say: rejected (speaker or text)";
                await Out(room, "voice", new { speaker = who, text, to = Str("to") });
                if (who == "ZETA") await Out(room, "screen_zeta", new { line = text });
                return $"{who}: {text}";
            }
            case "hint":
            {
                var id = Str("puzzle");
                var level = input.TryGetProperty("level", out var l) && l.TryGetInt32(out var n) ? Math.Clamp(n, 1, 3) : 1;
                var text = Short(Str("text"));
                if (id is null || !statuses.TryGetValue(id, out var st) || st != "active" || text == "") return $"hint: rejected ({id} is not active)";
                await Out(room, "voice", new { speaker = "KRUK", text, to = Str("to") });
                await Out(room, "hint", new { puzzle = id, level, to = Str("to") });
                return $"podpowiedź {level} ({id}): {text}";
            }
            case "twist":
            {
                var kind = Str("kind");
                if (kind == "patrol" || kind == "blackout")
                {
                    await Out(room, "twist", new { kind, seconds = kind == "patrol" ? 20 : 60 });
                    return $"zwrot akcji: {kind}";
                }
                if (kind != "lockdown") return "twist: rejected (unknown kind)";
                if (statuses.GetValueOrDefault("skanery") != "solved" || statuses.GetValueOrDefault("firewall") == "solved" || statuses.ContainsKey("blokada"))
                    return "lockdown: rejected (only between the scanners and the firewall, once)";
                Variants.Result? result = null;
                var errors = await engine.SaveRoom(room, r => (result = Variants.Lockdown(r!, rng)).Room);
                if (errors.Length > 0 || result is null) return "lockdown: rejected (" + string.Join("; ", errors) + ")";
                foreach (var (output, value) in result.Clues) await Out(room, output, value);
                await Out(room, "twist", new { kind, seconds = 0 });
                return $"blokada ({result.Summary})";
            }
            case "reroll":
            {
                if (!input.TryGetProperty("puzzles", out var list) || list.ValueKind != JsonValueKind.Array) return "reroll: rejected (no list)";
                var clock = observation.GetProperty("clockMinutes").GetDouble();
                var done = new List<string>();
                foreach (var item in list.EnumerateArray())
                {
                    var id = item.GetString() ?? "";
                    // A puzzle whose clue the players may already be reading keeps its solution, except in the first minute.
                    if (!Variants.Rerollable.Contains(id) || (statuses.GetValueOrDefault(id) != "locked" && clock > 1.0)) continue;
                    Variants.Result? result = null;
                    var errors = await engine.SaveRoom(room, r => (result = Variants.Reroll(r!, id, rng)).Room);
                    if (errors.Length > 0 || result is null) continue;
                    foreach (var (output, value) in result.Clues) await Out(room, output, value);
                    done.Add($"{id}: {result.Summary}");
                }
                return done.Count == 0 ? "reroll: nothing changed" : "wariant: " + string.Join("; ", done);
            }
            case "solve":
            {
                var id = Str("puzzle");
                if (id is null || statuses.GetValueOrDefault(id) != "active") return $"solve: rejected ({id} is not active)";
                return await engine.Solve(room, id) ? $"Kruk hakuje: {id}" : "solve: failed";
            }
            default:
                return $"{tool}: unknown tool";
        }
    }

    /// <summary>Without a model: a variant at the start, the hint ladder by silence, Kruk hacking when nothing moves for long.</summary>
    async Task Rules(string room, string reason, JsonElement observation)
    {
        var clock = observation.GetProperty("clockMinutes").GetDouble();
        var active = observation.GetProperty("puzzles").EnumerateArray()
            .Where(p => p.GetProperty("status").GetString() == "active").Select(p => p.GetProperty("id").GetString()!).ToList();
        var done = new List<string>();
        string thought;
        if (reason == "started")
        {
            done.Add(await Act(room, "reroll", JsonSerializer.SerializeToElement(new { puzzles = Variants.Rerollable }), observation));
            done.Add(await Act(room, "say", JsonSerializer.SerializeToElement(new { speaker = "KRUK", text = "Jesteście w środku. Macie godzinę, zanim ochrona odetnie piętra." }), observation));
            thought = "Start gry: losuję wariant dla tej drużyny.";
        }
        else if (reason == "solved")
        {
            lastSolveClock = clock;
            rulesHintLevel = 0;
            thought = "Drużyna idzie dalej.";
        }
        else
        {
            var silence = clock - lastSolveClock;
            var level = silence > 10 ? 3 : silence > 6 ? 2 : silence > 3 ? 1 : 0;
            if (level > rulesHintLevel && active.Count > 0)
            {
                rulesHintLevel = level;
                var id = active[rng.Next(active.Count)];
                done.Add(level == 3
                    ? await Act(room, "solve", JsonSerializer.SerializeToElement(new { puzzle = id, why = "długi przestój" }), observation)
                    : await Act(room, "hint", JsonSerializer.SerializeToElement(new { puzzle = id, level, text = level == 1 ? "Rozejrzyjcie się jeszcze raz. Ekrany coś mówią." : "Skupcie się na " + id + ". Wskazówka jest na ekranie obok." }), observation));
                thought = $"Brak postępu od {silence:0} min, podpowiedź poziomu {level}.";
            }
            else
            {
                thought = "Tempo w porządku, obserwuję.";
            }
        }
        await Report(room, thought, done, "rules");
    }

    async Task Report(string room, string thought, List<string> done, string mode)
    {
        await Out(room, "gm", new { thought, actions = done, mode, model = mode == "model" ? Model : null });
        memory.Add($"{thought} → {(done.Count == 0 ? "nic" : string.Join("; ", done))}");
        log.LogInformation("GM ({Mode}): {Thought} | {Actions}", mode, thought, string.Join(" | ", done));
    }

    Task Out(string room, string output, object value) => engine.Trigger(room, output, JsonSerializer.SerializeToElement(value));
}
