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
    public void Every_reroll_keeps_the_room_valid()
    {
        var rng = new Random(7);
        foreach (var id in Variants.Rerollable)
            for (var i = 0; i < 20; i++)
            {
                var result = Variants.Reroll(Jadro(), id, rng);
                Assert.Empty(result.Room.Validate());
                Assert.NotEmpty(result.Clues);
            }
    }

    [Fact]
    public void Duty_guard_sets_both_the_locker_code_and_the_badge()
    {
        var result = Variants.Reroll(Jadro(), "szafka", new Random(3));
        var clue = JsonSerializer.SerializeToElement(result.Clues.Single().Value);
        var duty = clue.GetProperty("duty").GetString()!;
        var shift = clue.GetProperty("shifts").GetProperty(duty);
        Assert.Equal($"{shift[0].GetInt32():00}{shift[1].GetInt32():00}", Payload.Text(P(result.Room, "szafka").When!.Is!.Value));
        Assert.Equal("OCHRONA-" + duty.ToUpperInvariant(), Payload.Text(P(result.Room, "bramka").When!.Is!.Value));
    }

    [Fact]
    public void Lockdown_inserts_a_puzzle_the_firewall_waits_for()
    {
        var room = Variants.Lockdown(Jadro(), new Random(1)).Room;
        Assert.Empty(room.Validate());
        Assert.Contains("blokada", P(room, "firewall").After!);
        var game = new Game(room, new GameState());
        foreach (var id in new[] { "przeszukanie", "telefon", "szafka", "bramka", "sciana", "zdrive", "zmart", "skanery" })
            game.Solve(id, Now);
        Assert.Equal("active", game.Status(P(room, "blokada")));
        Assert.Equal("locked", game.Status(P(room, "firewall")));
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
