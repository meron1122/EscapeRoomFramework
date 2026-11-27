using System.Text.Json;

namespace EscapeRoom.Tests;

public class TheCoreTests
{
    static readonly DateTimeOffset Now = DateTimeOffset.UnixEpoch;

    static Room TheCore() => JsonSerializer.Deserialize<Room>(
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "examples", "the-core.json")), Engine.Json)!;

    static Puzzle P(Room r, string id) => r.Puzzles.Single(p => p.Id == id);

    [Fact]
    public void Room_is_valid_and_has_two_endings()
    {
        var room = TheCore();
        Assert.Empty(room.Validate());
        Assert.Equal(["detonation", "deal"], room.Puzzles.Where(p => p.Ends).Select(p => p.Id));
    }

    [Fact]
    public void Every_variant_keeps_the_room_valid()
    {
        var room = TheCore();
        // With the lockdown in place, so the variants of its puzzle have something to set.
        var game = room.WithTwist(room.Twists!["lockdown"]) with { Twists = null };
        foreach (var (handle, options) in room.Variants!)
            foreach (var v in options)
                Assert.Empty(game.WithVariant(v).Validate().Select(e => $"{handle} ({v.Summary}): {e}"));
    }

    [Fact]
    public void Duty_guard_sets_both_the_locker_code_and_the_badge()
    {
        foreach (var v in TheCore().Variants!["locker"])
        {
            var clue = v.Clues!.Single().Value!.Value;
            var duty = clue.GetProperty("duty").GetString()!;
            var shift = clue.GetProperty("shifts").GetProperty(duty);
            Assert.Equal($"{shift[0].GetInt32():00}{shift[1].GetInt32():00}", Payload.Text(v.Solutions["locker"].Is!.Value));
            Assert.Equal("SECURITY-" + duty.ToUpperInvariant(), Payload.Text(v.Solutions["gate"].Is!.Value));
        }
    }

    [Fact]
    public void Lockdown_adds_a_puzzle_the_firewall_waits_for()
    {
        var room = TheCore().WithTwist(TheCore().Twists!["lockdown"]);
        Assert.Contains("doorlock", P(room, "firewall").After!);
        var game = new Game(room, new GameState());
        foreach (var id in new[] { "search", "phone", "locker", "gate", "wall", "zdrive", "zmart", "scanners" })
            game.Solve(id, Now);
        Assert.Equal("active", game.Status(P(room, "doorlock")));
        Assert.Equal("locked", game.Status(P(room, "firewall")));
    }

    [Fact]
    public void Game_master_data_is_checked()
    {
        var room = TheCore();
        Assert.Contains((room with { Cues = new() { ["c"] = new Cue("c", [new Step("nope", room.Start![0].Value)]) } }).Validate(), e => e.Contains("unknown output 'nope'"));
        Assert.Contains((room with { Characters = [new Character("X", "x", "nope")] }).Validate(), e => e.Contains("unknown output 'nope'"));
        Assert.Contains((room with { Variants = null, Twists = new() { ["t"] = new Twist("t", Blocks: ["nope"]) } }).Validate(), e => e.Contains("unknown puzzle 'nope'"));
        Assert.Contains((room with { Variants = new() { ["v"] = [room.Variants!["phone"][0], new Variant(new() { ["nope"] = room.Puzzles[0].When! })] } }).Validate(),
            e => e.Contains("unknown puzzle 'nope'"));
    }

    [Fact]
    public void Either_ending_finishes_the_game()
    {
        var room = TheCore();
        var game = new Game(room, new GameState());
        foreach (var id in new[] { "search", "phone", "locker", "gate", "wall", "zdrive", "zmart", "scanners", "firewall", "arming" })
            game.Solve(id, Now);
        Assert.False(game.Finished);
        game.Input("accept", "PRESSED", Now);
        Assert.True(game.Finished);
        Assert.Equal("deal", game.Ending);
    }

    [Theory]
    [InlineData("claude-opus-5-5", "anthropic/claude-opus-5.5")]
    [InlineData("claude-opus-5", "anthropic/claude-opus-5")]
    [InlineData("anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-5.5")]
    public void Model_names_map_to_OpenRouter(string model, string expected) =>
        Assert.Equal(expected, GameMaster.ForOpenRouter(model));

    [Fact]
    public void Chat_model_answers_are_read_and_malformed_calls_skipped()
    {
        var (thought, calls) = GameMaster.ParseChat("""
            {"choices":[{"message":{"content":null,"tool_calls":[
              {"function":{"name":"note","arguments":"{\"text\":\"Fox stalls at the locker.\"}"}},
              {"function":{"name":"cue","arguments":"{\"cue\":\"locker\"}"}},
              {"function":{"name":"say","arguments":"not json"}},
              {"function":{"name":"solve","arguments":"[1,2]"}}]}}]}
            """);
        Assert.Equal("Fox stalls at the locker.", thought);
        var call = Assert.Single(calls);
        Assert.Equal("cue", call.Name);
        Assert.Equal("locker", call.Input.GetProperty("cue").GetString());
        Assert.Empty(GameMaster.ParseChat("{}").Calls);
    }

    [Fact]
    public void Chat_request_carries_the_model_the_prompt_and_the_tools()
    {
        var tools = new[] { new GameMaster.ToolSpec("cue", "A lamp.", new() { ["cue"] = JsonSerializer.SerializeToElement(new { type = "string" }) }, ["cue"]) };
        var request = JsonSerializer.Deserialize<JsonElement>(GameMaster.ChatRequest("deepseek/deepseek-v4-flash", "SYSTEM", "INPUT", tools));
        Assert.Equal("deepseek/deepseek-v4-flash", request.GetProperty("model").GetString());
        Assert.Equal("SYSTEM", request.GetProperty("messages")[0].GetProperty("content").GetString());
        Assert.Equal(["note", "cue"], request.GetProperty("tools").EnumerateArray().Select(x => x.GetProperty("function").GetProperty("name").GetString()));
    }
}
