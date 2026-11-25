namespace EscapeRoom;

public record InputEvent(string Input, string Value);

/// <summary>Progress of one game. Plain data so it round-trips through jsonb and survives restarts.</summary>
public class GameState
{
    public Dictionary<string, string> Inputs { get; init; } = [];
    public Dictionary<string, DateTimeOffset> Solved { get; init; } = [];

    /// <summary>Per sequence puzzle: the last relevant input events, at most as many as the sequence has steps.</summary>
    public Dictionary<string, List<InputEvent>> Recent { get; init; } = [];
}

/// <summary>Pure puzzle logic: feed it input values, it returns the puzzles that got solved, in order.</summary>
public class Game(Room room, GameState state)
{
    public GameState State => state;
    public bool Finished => room.Puzzles.All(p => state.Solved.ContainsKey(p.Id));

    public bool IsActive(Puzzle p) => !state.Solved.ContainsKey(p.Id) && (p.After ?? []).All(state.Solved.ContainsKey);

    public string Status(Puzzle p) => state.Solved.ContainsKey(p.Id) ? "solved" : IsActive(p) ? "active" : "locked";

    public List<Puzzle> Input(string input, string value, DateTimeOffset now)
    {
        state.Inputs[input] = value;
        foreach (var p in room.Puzzles.Where(IsActive))
        {
            // Events matching no step (a button release, an unrelated sensor) neither advance nor break a sequence.
            if (p.When?.Sequence is not { } seq || !seq.Any(s => s.Input == input && s.Matches(value))) continue;
            if (!state.Recent.TryGetValue(p.Id, out var recent)) state.Recent[p.Id] = recent = [];
            recent.Add(new(input, value));
            if (recent.Count > seq.Length) recent.RemoveAt(0);
        }
        return Settle(now);
    }

    /// <summary>Game-master override, e.g. when a sensor dies mid-game. Runs the puzzle's actions like a real solve.</summary>
    public List<Puzzle> Solve(string puzzleId, DateTimeOffset now)
    {
        var p = room.Puzzles.Single(x => x.Id == puzzleId);
        if (state.Solved.ContainsKey(p.Id)) return [];
        state.Solved[p.Id] = now;
        return [p, .. Settle(now)];
    }

    List<Puzzle> Settle(DateTimeOffset now)
    {
        var solved = new List<Puzzle>();
        // A solve activates later puzzles whose condition may already hold (the sensors are in place), so repeat.
        for (var changed = true; changed;)
        {
            changed = false;
            foreach (var p in room.Puzzles.Where(IsActive).ToList())
                if (p.When is { } w && Met(p, w))
                {
                    state.Solved[p.Id] = now;
                    solved.Add(p);
                    changed = true;
                }
        }
        return solved;
    }

    bool Met(Puzzle p, Condition c) => c switch
    {
        { Sequence: { } seq } => state.Recent.TryGetValue(p.Id, out var r) && r.Count == seq.Length
            && seq.Zip(r).All(x => x.First.Input == x.Second.Input && x.First.Matches(x.Second.Value)),
        { All: { } all } => all.All(x => Met(p, x)),
        { Any: { } any } => any.Any(x => Met(p, x)),
        _ => state.Inputs.TryGetValue(c.Input!, out var v) && c.Matches(v),
    };
}
