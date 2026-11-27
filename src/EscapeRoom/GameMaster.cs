using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using Anthropic;
using Anthropic.Models.Beta;
using Anthropic.Models.Beta.Messages;

namespace EscapeRoom;

/// <summary>
/// The game master: a Claude model that runs whichever room is playing (one game at a time). Everything it knows
/// about the room comes from the engine: the venue's brief, the characters it speaks as, the puzzle variants and
/// the prepared twists, plus the live game and every player. It acts only through the engine, so each change is
/// validated, and the safety layer is out of its reach. Without model credentials it falls back to simple rules.
/// </summary>
public class GameMaster(Engine engine, Voice voice, IConfiguration config, ILogger<GameMaster> log) : BackgroundService
{
    const string Prompt = """
        You are the live game master of an escape room. The venue describes the room in the next block: its story,
        mood, characters, puzzles, pacing and staging, followed by the room's data. You run one game: you watch every
        player and decide what happens next, and the players hear you only through the room's characters.

        Every ~40 seconds of game time and on key events you get JSON with: the clock and the game length, each puzzle
        (status locked, active or solved; `when` is its live solution as sensor conditions), the latest sensor values
        (wrong attempts included), recent events, the players (where each one is, what they do, how long without
        progress), the twists already used and your own recent decisions.

        How to act:
        - First write one short sentence in the room's language: your read of the situation. Then call 0-3 tools.
          Doing nothing is right when the team is flowing; don't talk just to talk.
        - At the start of a game ("reason": "started") reroll every variant, so this team gets its own game, and open
          the game the way the brief describes.
        - Keep the pace the brief sets. Without one, spread the puzzles over the game length so it ends close to time.
        - Hint ladder when the team falls behind: about 3 minutes, a nudge (level 1); about 6, a hint that points at
          the clue (level 2); about 10 or clearly stuck, level 3 or `solve`. Start from the venue's prepared hints and
          address the stuck player by name.
        - Before words, try a cue when the room has one for the puzzle: a lamp or a sign that draws eyes to the clue.
          It keeps the players' own discovery; speak only if the cue did not help.
        - When the team is well ahead, raise the tension with a twist the brief allows, one at a time.
        - Every line is in character, in the room's language, under 20 words, and never repeats a recent one. Never
          reveal a solution outright except in a level 3 hint. A line may start with one delivery tag in square
          brackets, e.g. [whispers], [sarcastic], [laughs]: it shapes the voice and players don't see it.
        - The JSON you get wins over the brief: its variants and solutions are the live ones.
        - Exits and emergency systems are not yours: you cannot lock or unlock them, and you never pretend you can.
        """;

    readonly Random rng = new();
    readonly List<string> memory = [];
    double lastSolveClock;
    int rulesHintLevel;
    bool openRouter;

    // The room being played and what the game master was told about it at the start of the game.
    Room? def;
    string playbook = "";

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        if (!config.GetValue("GameMaster:Enabled", true)) return;
        var gameSeconds = config.GetValue("GameMaster:IntervalSeconds", 40.0);
        var speed = 1.0;
        var client = Client();
        log.LogInformation("Game master: {Mode}{Via}", client is null ? "rules only" : Model, openRouter ? " via OpenRouter" : "");
        string? room = null;
        var wake = Channel.CreateBounded<(string Room, string Type)>(new BoundedChannelOptions(16) { FullMode = BoundedChannelFullMode.DropOldest });
        engine.Changed += (r, type) =>
        {
            if (type == "started" || (type == "solved" && r == room)) wake.Writer.TryWrite((r, type));
        };
        while (!ct.IsCancellationRequested)
        {
            var reason = "tick";
            string? started = null;
            using (var wait = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                // The interval is in game time: a simulation at 20x checks in more often in real time, but never
                // more than every 6 s, which keeps model calls affordable.
                wait.CancelAfter(TimeSpan.FromSeconds(Math.Max(6.0, gameSeconds / speed)));
                try
                {
                    var e = await wake.Reader.ReadAsync(wait.Token);
                    reason = e.Type;
                    if (e.Type == "started") started = e.Room;
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested) { }
            }
            await Task.Delay(TimeSpan.FromSeconds(2), ct);  // let a burst of events settle into one decision
            while (wake.Reader.TryRead(out var more))
                if (more.Type == "started") (reason, started) = (more.Type, more.Room);

            // One game at a time: a newly started game takes over; otherwise stay with the current one.
            if (started is not null || room is null || def is null)
            {
                room = started ?? (await engine.Running()).FirstOrDefault();
                if (room is null || await engine.Playing(room) is not { } playing) continue;
                def = playing;
                playbook = Playbook(playing, await engine.Brief(room));
                memory.Clear();
                lastSolveClock = 0;
                rulesHintLevel = 0;
                log.LogInformation("Game master takes {Room} ({Brief} chars of brief and room data)", room, playbook.Length);
            }
            if (await engine.Observe(room) is not { } seen)
            {
                room = null;  // the game ended
                continue;
            }
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

    /// <summary>What the model knows about the room for a whole game: the venue's brief, then the room's data.</summary>
    static string Playbook(Room room, string? brief) =>
        $"{(string.IsNullOrWhiteSpace(brief) ? "(The venue wrote no brief for this room.)" : brief.Trim())}\n\n## Room data\n\n" +
        JsonSerializer.Serialize(new
        {
            room.Name,
            room.Minutes,
            characters = room.Characters?.Select(c => new { c.Name, c.About }),
            variants = room.Variants?.Keys,
            cues = room.Cues?.Select(kv => new { id = kv.Key, kv.Value.About, kv.Value.Puzzle }),
            twists = room.Twists?.Select(kv => new { id = kv.Key, kv.Value.About, kv.Value.After, kv.Value.Blocks }),
            preparedHints = AllPuzzles(room).Where(p => p.Hints is { Length: > 0 }).ToDictionary(p => p.Id, p => p.Hints),
        }, Engine.Json);

    static IEnumerable<Puzzle> AllPuzzles(Room room) => room.Puzzles.Concat((room.Twists?.Values ?? Enumerable.Empty<Twist>()).SelectMany(t => t.Adds ?? []));

    string Model => openRouter
        ? ForOpenRouter(config["GameMaster:Model"] ?? "claude-opus-5-5")
        : config["GameMaster:Model"] ?? "claude-opus-5-5";

    /// <summary>OpenRouter names models "anthropic/claude-opus-5.5" where Anthropic says "claude-opus-5-5".</summary>
    public static string ForOpenRouter(string model) => model.StartsWith("claude-")
        ? "anthropic/" + Regex.Replace(model, @"-(\d+)-(\d+)$", "-$1.$2")
        : model;

    AnthropicClient? Client()
    {
        if (config.GetValue("GameMaster:RulesOnly", false)) return null;
        // On a Sprite the key lives in a Connector: BaseUrl points at the gateway and the key here is only a placeholder.
        var baseUrl = config["GameMaster:BaseUrl"];
        var routerKey = Environment.GetEnvironmentVariable("OPENROUTER_API_KEY");
        openRouter = config["GameMaster:Provider"] is { } provider ? provider == "openrouter" : !string.IsNullOrEmpty(routerKey);
        if (openRouter)
            return new AnthropicClient
            {
                AuthToken = string.IsNullOrEmpty(routerKey) ? "injected-by-connector" : routerKey,
                BaseUrl = string.IsNullOrEmpty(baseUrl) ? "https://openrouter.ai/api" : baseUrl,
            };
        // Elsewhere the SDK finds credentials itself (ANTHROPIC_API_KEY or an `ant auth login` profile).
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
            // OpenRouter attaches betas itself and has its own fallbacks.
            Betas = openRouter ? null : [AnthropicBeta.ServerSideFallback2026_07_01],
            Fallbacks = openRouter ? null : new Default(),
            OutputConfig = new BetaOutputConfig { Effort = (config["GameMaster:Effort"] ?? "low") switch
            {
                "medium" => Effort.Medium, "high" => Effort.High, _ => Effort.Low,
            } },
            // Instructions and the room's playbook stay the same for a whole game, so they are cached.
            System = new List<BetaTextBlockParam> { new() { Text = Prompt }, new() { Text = playbook, CacheControl = new BetaCacheControlEphemeral() } },
            Tools = Tools(def!),
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

    /// <summary>The tools follow the room: its characters, variants and twists are the only valid choices.</summary>
    static List<BetaToolUnion> Tools(Room room)
    {
        static JsonElement S(object o) => JsonSerializer.SerializeToElement(o);
        static BetaTool Tool(string name, string description, Dictionary<string, JsonElement> properties, params string[] required) =>
            new() { Name = name, Description = description, InputSchema = new() { Properties = properties, Required = required } };
        var text = S(new { type = "string", description = "What the character says, in the room's language, under 20 words." });
        var to = S(new { type = "string", description = "Optional: the name of the player this is addressed to." });
        var puzzle = S(new { type = "string", description = "Puzzle id from the observation." });
        var why = S(new { type = "string" });
        List<BetaToolUnion> tools =
        [
            Tool("solve", "Mark a puzzle solved, explained in the story. Last resort for a team that is truly stuck.",
                new() { ["puzzle"] = puzzle, ["why"] = why }, "puzzle", "why"),
        ];
        if (room.Characters is { Length: > 0 } cast)
        {
            var speaker = S(new { type = "string", @enum = cast.Select(c => c.Name) });
            tools.Add(Tool("say", "Speak to the players as one of the room's characters.",
                new() { ["speaker"] = speaker, ["text"] = text, ["to"] = to }, "speaker", "text"));
            tools.Add(Tool("hint", "A character gives a hint for one active puzzle. Level 1 nudges, 2 points at the clue, 3 nearly gives the answer.",
                new() { ["puzzle"] = puzzle, ["level"] = S(new { type = "integer", minimum = 1, maximum = 3 }), ["speaker"] = speaker, ["text"] = text, ["to"] = to },
                "puzzle", "level", "speaker", "text"));
        }
        if (room.Variants is { Count: > 0 } variants)
            tools.Add(Tool("reroll", "Draw new variants: each one's solution and clue change together. Only for puzzles nobody works on yet (any in the first minute).",
                new() { ["variants"] = S(new { type = "array", items = new { type = "string", @enum = variants.Keys } }) }, "variants"));
        if (room.Cues is { Count: > 0 } cues)
            tools.Add(Tool("cue", "Fire a wordless hint built into the room (a lamp, a sign). See the room data for what each one points at.",
                new() { ["cue"] = S(new { type = "string", @enum = cues.Keys }) }, "cue"));
        if (room.Twists is { Count: > 0 } twists)
            tools.Add(Tool("twist", "Fire one of the room's prepared twists (see the room data for what each does and when it is allowed).",
                new() { ["twist"] = S(new { type = "string", @enum = twists.Keys }), ["why"] = why }, "twist", "why"));
        return tools;
    }

    /// <summary>Runs one tool call. The input comes from a model, so every field is checked before anything happens.</summary>
    async Task<string> Act(string room, string tool, JsonElement input, JsonElement observation)
    {
        string? Str(string k) => input.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        string Short(string? s) => (s ?? "").Trim() is var t && t.Length > 220 ? t[..220] : (s ?? "").Trim();
        Character? Cast(string? name) => def?.Characters?.FirstOrDefault(c => c.Name == name);
        var statuses = observation.GetProperty("puzzles").EnumerateArray()
            .ToDictionary(p => p.GetProperty("id").GetString()!, p => p.GetProperty("status").GetString()!);
        switch (tool)
        {
            case "say":
            {
                var who = Cast(Str("speaker"));
                var text = Short(Str("text"));
                if (who is null || text == "") return "say: rejected (speaker or text)";
                return await Speak(room, who, text, Str("to"));
            }
            case "hint":
            {
                var id = Str("puzzle");
                var level = input.TryGetProperty("level", out var l) && l.TryGetInt32(out var n) ? Math.Clamp(n, 1, 3) : 1;
                var who = Cast(Str("speaker")) ?? def?.Characters?.FirstOrDefault();
                var text = Short(Str("text"));
                if (id is null || !statuses.TryGetValue(id, out var st) || st != "active" || who is null || text == "")
                    return $"hint: rejected ({id} is not active, or no speaker or text)";
                await Speak(room, who, text, Str("to"));
                // An output named "hint" (a hint screen, the simulator) learns which puzzle got help; rooms without one skip it.
                await Out(room, "hint", new { puzzle = id, level, to = Str("to") });
                return $"hint {level} ({id}): {text}";
            }
            case "cue":
            {
                var (ok, message) = await engine.Cue(room, Str("cue") ?? "");
                return ok ? $"light cue: {message}" : $"cue: rejected ({message})";
            }
            case "twist":
            {
                var (ok, message) = await engine.Twist(room, Str("twist") ?? "", rng);
                return ok ? $"twist: {message}" : $"twist: rejected ({message})";
            }
            case "reroll":
            {
                if (!input.TryGetProperty("variants", out var list) || list.ValueKind != JsonValueKind.Array) return "reroll: rejected (no list)";
                var done = new List<string>();
                foreach (var item in list.EnumerateArray())
                    if (await engine.Reroll(room, item.GetString() ?? "", rng) is (true, var summary))
                        done.Add($"{item.GetString()}: {summary}");
                return done.Count == 0 ? "reroll: nothing changed" : "variant: " + string.Join("; ", done);
            }
            case "solve":
            {
                var id = Str("puzzle");
                if (id is null || statuses.GetValueOrDefault(id) != "active") return $"solve: rejected ({id} is not active)";
                return await engine.Solve(room, id) ? $"solved for the players: {id}" : "solve: failed";
            }
            default:
                return $"{tool}: unknown tool";
        }
    }

    /// <summary>A character's line: spoken on its output (with the voice, when there is one) and shown on its screen.</summary>
    async Task<string> Speak(string room, Character who, string text, string? to)
    {
        var shown = Regex.Replace(text, @"\[[^\]]*\]\s*", "").Trim();  // delivery tags shape the voice only
        var audio = await voice.Speak(who.Voice, text);
        await Out(room, who.Output, new { speaker = who.Name, text = shown, to, audio });
        if (who.Screen is { } screen) await Out(room, screen, new { line = shown });
        return $"{who.Name}: {shown}";
    }

    /// <summary>
    /// Without a model: every variant drawn at the start, then the hint ladder by silence, using the venue's prepared
    /// hints spoken by the room's first character, and solving for the team when nothing moves for long.
    /// </summary>
    async Task Rules(string room, string reason, JsonElement observation)
    {
        var clock = observation.GetProperty("clockMinutes").GetDouble();
        var active = observation.GetProperty("puzzles").EnumerateArray()
            .Where(p => p.GetProperty("status").GetString() == "active").Select(p => p.GetProperty("id").GetString()!).ToList();
        var done = new List<string>();
        string thought;
        if (reason == "started")
        {
            if (def?.Variants is { Count: > 0 } variants)
                done.Add(await Act(room, "reroll", JsonSerializer.SerializeToElement(new { variants = variants.Keys }), observation));
            thought = "Game start: drawing a variant for this team.";
        }
        else if (reason == "solved")
        {
            lastSolveClock = clock;
            rulesHintLevel = 0;
            thought = "The team moves on.";
        }
        else
        {
            var silence = clock - lastSolveClock;
            var level = silence > 10 ? 3 : silence > 6 ? 2 : silence > 3 ? 1 : 0;
            var helper = def?.Characters?.FirstOrDefault();
            if (level > rulesHintLevel && active.Count > 0)
            {
                rulesHintLevel = level;
                var id = active[rng.Next(active.Count)];
                var hints = def is null ? null : AllPuzzles(def).FirstOrDefault(p => p.Id == id)?.Hints;
                var cue = def?.Cues?.FirstOrDefault(kv => kv.Value.Puzzle == id).Key;
                if (level == 1 && cue is not null)
                    done.Add(await Act(room, "cue", JsonSerializer.SerializeToElement(new { cue }), observation));
                else if (level == 3)
                    done.Add(await Act(room, "solve", JsonSerializer.SerializeToElement(new { puzzle = id, why = "long standstill" }), observation));
                else if (hints is { Length: > 0 } && helper is not null)
                    done.Add(await Act(room, "hint", JsonSerializer.SerializeToElement(new { puzzle = id, level, speaker = helper.Name, text = hints[Math.Min(level, hints.Length) - 1] }), observation));
                thought = $"No progress for {silence:0} min, level {level} hint.";
            }
            else
            {
                thought = "The pace is fine, watching.";
            }
        }
        await Report(room, thought, done, "rules");
    }

    async Task Report(string room, string thought, List<string> done, string mode)
    {
        await Out(room, "gm", new { thought, actions = done, mode, model = mode == "model" ? Model : null });
        memory.Add($"{thought} → {(done.Count == 0 ? "nothing" : string.Join("; ", done))}");
        log.LogInformation("GM ({Mode}): {Thought} | {Actions}", mode, thought, string.Join(" | ", done));
    }

    Task Out(string room, string output, object value) => engine.Trigger(room, output, JsonSerializer.SerializeToElement(value, Engine.Json));
}
