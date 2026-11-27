using System.Text.Json;

namespace EscapeRoom.Tests;

public class JadroTests
{
    static readonly DateTimeOffset Now = DateTimeOffset.UnixEpoch;

    static Room Jadro() => JsonSerializer.Deserialize<Room>(
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "examples", "jadro.json")), Engine.Json)!;

    static Puzzle P(Room r, string id) => r.Puzzles.Single(p => p.Id == id);

    [Fact]
    public void Room_is_valid_and_has_two_endings()
    {
        var room = Jadro();
        Assert.Empty(room.Validate());
        Assert.Equal(["detonacja", "uklad"], room.Puzzles.Where(p => p.Ends).Select(p => p.Id));
    }

    [Fact]
    public void Every_variant_keeps_the_room_valid()
    {
        var room = Jadro();
        // With the lockdown in place, so the variants of its puzzle have something to set.
        var game = room.WithTwist(room.Twists!["lockdown"]) with { Twists = null };
        foreach (var (handle, options) in room.Variants!)
            foreach (var v in options)
                Assert.Empty(game.WithVariant(v).Validate().Select(e => $"{handle} ({v.Summary}): {e}"));
    }

    [Fact]
    public void Duty_guard_sets_both_the_locker_code_and_the_badge()
    {
        foreach (var v in Jadro().Variants!["szafka"])
        {
            var clue = v.Clues!.Single().Value!.Value;
            var duty = clue.GetProperty("duty").GetString()!;
            var shift = clue.GetProperty("shifts").GetProperty(duty);
            Assert.Equal($"{shift[0].GetInt32():00}{shift[1].GetInt32():00}", Payload.Text(v.Solutions["szafka"].Is!.Value));
            Assert.Equal("OCHRONA-" + duty.ToUpperInvariant(), Payload.Text(v.Solutions["bramka"].Is!.Value));
        }
    }

    [Fact]
    public void Lockdown_adds_a_puzzle_the_firewall_waits_for()
    {
        var room = Jadro().WithTwist(Jadro().Twists!["lockdown"]);
        Assert.Contains("blokada", P(room, "firewall").After!);
        var game = new Game(room, new GameState());
        foreach (var id in new[] { "przeszukanie", "telefon", "szafka", "bramka", "sciana", "zdrive", "zmart", "skanery" })
            game.Solve(id, Now);
        Assert.Equal("active", game.Status(P(room, "blokada")));
        Assert.Equal("locked", game.Status(P(room, "firewall")));
    }

    [Fact]
    public void Game_master_data_is_checked()
    {
        var room = Jadro();
        Assert.Contains((room with { Cues = new() { ["c"] = new Cue("c", [new Step("nope", room.Start![0].Value)]) } }).Validate(), e => e.Contains("unknown output 'nope'"));
        Assert.Contains((room with { Characters = [new Character("X", "x", "nope")] }).Validate(), e => e.Contains("unknown output 'nope'"));
        Assert.Contains((room with { Variants = null, Twists = new() { ["t"] = new Twist("t", Blocks: ["nope"]) } }).Validate(), e => e.Contains("unknown puzzle 'nope'"));
        Assert.Contains((room with { Variants = new() { ["v"] = [room.Variants!["telefon"][0], new Variant(new() { ["nope"] = room.Puzzles[0].When! })] } }).Validate(),
            e => e.Contains("unknown puzzle 'nope'"));
    }

    [Fact]
    public void Either_ending_finishes_the_game()
    {
        var room = Jadro();
        var game = new Game(room, new GameState());
        foreach (var id in new[] { "przeszukanie", "telefon", "szafka", "bramka", "sciana", "zdrive", "zmart", "skanery", "firewall", "uzbrojenie" })
            game.Solve(id, Now);
        Assert.False(game.Finished);
        game.Input("accept", "PRESSED", Now);
        Assert.True(game.Finished);
        Assert.Equal("uklad", game.Ending);
    }

    [Theory]
    [InlineData("claude-opus-5-5", "anthropic/claude-opus-5.5")]
    [InlineData("claude-opus-5", "anthropic/claude-opus-5")]
    [InlineData("anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-5.5")]
    public void Model_names_map_to_OpenRouter(string model, string expected) =>
        Assert.Equal(expected, GameMaster.ForOpenRouter(model));
}
