using System.Globalization;
using System.Text.Json;

namespace EscapeRoom;

/// <summary>
/// Puzzle variants for the Jądro room (docs/scenariusz-jadro.md). A reroll changes a puzzle's solution together
/// with the clue that reveals it (screen content, lit indicators), so a rerolled puzzle is always solvable.
/// The game master decides when; this class guarantees the solution and the clue match.
/// </summary>
public static class Variants
{
    public static readonly string[] Rerollable = ["telefon", "szafka", "sciana", "zdrive", "zmart", "skanery", "firewall", "uzbrojenie"];

    static readonly Dictionary<string, int[]> Gestures = new()
    {
        ["Z"] = [1, 2, 3, 5, 7, 8, 9], ["S"] = [3, 2, 1, 4, 5, 6, 9, 8, 7], ["N"] = [7, 4, 1, 5, 9, 6, 3],
        ["U"] = [1, 4, 7, 8, 9, 6, 3], ["L"] = [1, 4, 7, 8, 9], ["7"] = [1, 2, 3, 5, 7],
    };
    static readonly string[] Guards = ["Marek", "Ola", "Tomek"];
    static readonly string[] Apps = ["Z-FEED", "Z-BET", "Z-DATE", "Z-NEWS", "Z-PLAY", "Z-FIT"];
    static readonly (string Profile, string[] Products)[] Profiles =
    [
        ("Anna, 34 lata, bezsenność, dwoje dzieci", ["Z-BOOST", "Z-KIDS", "Z-SNACK", "Z-SEN"]),
        ("Marta, 41 lat, stres w pracy", ["Z-CALM", "Z-BOOST", "Z-SNACK", "Z-SEN"]),
        ("Piotr, 23 lata, gra nocami", ["Z-BOOST", "Z-SNACK", "Z-BOOST-MAX", "Z-SEN"]),
        ("Halina, 67 lat, mieszka sama", ["Z-CALM", "Z-NEWS-PRINT", "Z-SNACK", "Z-SEN"]),
    ];
    static readonly string[] Words = ["Telefon", "Auto", "Sklep"];

    public record Result(Room Room, List<(string Output, object Value)> Clues, string Summary);

    public static Result Reroll(Room room, string puzzle, Random rng)
    {
        List<(string, object)> clues = [];
        switch (puzzle)
        {
            case "telefon":
            {
                var gest = Gestures.Keys.ElementAt(rng.Next(Gestures.Count));
                clues.Add(("screen_reklama", new { gest }));
                return new(With(room, "telefon", new Condition(Sequence: [.. Gestures[gest].Select(k => Eq("phone_key", k.ToString()))])),
                    clues, $"gest {gest}");
            }
            case "szafka":
            {
                // The duty guard sets both the locker code (their shift hours) and the only badge the gate accepts.
                var duty = Guards[rng.Next(Guards.Length)];
                int[] starts = [6, 8, 10, 14, 22];
                var shifts = Guards.ToDictionary(g => g, g => { var s = starts[rng.Next(starts.Length)]; return new[] { s, (s + 8) % 24 }; });
                var code = $"{shifts[duty][0]:00}{shifts[duty][1]:00}";
                clues.Add(("screen_grafik", new { duty, shifts }));
                var r = With(room, "szafka", Eq("locker_code", code));
                return new(With(r, "bramka", Eq("gate_qr", "OCHRONA-" + duty.ToUpperInvariant())), clues, $"dyżur {duty}, kod {code}");
            }
            case "sciana":
            {
                var minutes = new Dictionary<string, int>();
                var pool = Enumerable.Range(40, 220).ToList();
                foreach (var app in Apps)
                {
                    var i = rng.Next(pool.Count);
                    minutes[app] = pool[i];
                    pool.RemoveAll(m => Math.Abs(m - minutes[app]) < 5);  // distinct enough to read off a chart
                }
                var order = minutes.OrderByDescending(kv => kv.Value).Select(kv => kv.Key).ToArray();
                clues.Add(("screen_raport", new { minutes }));
                return new(With(room, "sciana", new Condition(All: [.. order.Select((app, i) => Eq($"slot{i + 1}", app))])),
                    clues, "raport: " + string.Join(" > ", order));
            }
            case "zdrive":
            {
                var p = rng.Next(18, 33) / 10.0;
                var t = rng.Next(12, 23) * 5;
                var rpm = rng.Next(10, 61) * 100;
                var kod = string.Create(CultureInfo.InvariantCulture, $"SRV-{p:0.0}/{t}/{rpm}");
                clues.Add(("screen_zdrive", new { kod }));
                return new(With(room, "zdrive", new Condition(All:
                [
                    Gt("knob_pressure", p - 0.05), Lt("knob_pressure", p + 0.05),
                    Gt("knob_temp", t - 2.5), Lt("knob_temp", t + 2.5),
                    Gt("knob_rpm", rpm - 50), Lt("knob_rpm", rpm + 50),
                ])), clues, $"kod {kod}");
            }
            case "zmart":
            {
                var (profile, products) = Profiles[rng.Next(Profiles.Length)];
                clues.Add(("screen_kiosk", new { klientka = profile }));
                return new(With(room, "zmart", new Condition(Sequence: [.. products.Select(x => Eq("mart_qr", x))])), clues, profile);
            }
            case "skanery":
            {
                string[] active = [rng.Next(2) == 0 ? "1" : "2", rng.Next(2) == 0 ? "3" : "4"];
                clues.Add(("scanners", new { active }));
                return new(With(room, "skanery", new Condition(All: [.. active.Select(k => Eq("scanner" + k, "ON"))])),
                    clues, $"skanery {active[0]} i {active[1]}");
            }
            case "firewall":
            {
                var active = Enumerable.Range(1, 5).OrderBy(_ => rng.Next()).Take(3).Order().ToArray();
                clues.Add(("sensors", new { active }));
                return new(With(room, "firewall", new Condition(All: [.. active.Select(k => Gt("light" + k, 600))])),
                    clues, "czujniki " + string.Join(", ", active));
            }
            case "uzbrojenie":
            {
                // Fragment i is shown by station i (Ściana, Z-Drive, Z-Mart); ZETA's slogan gives the order.
                int[] fragments = [rng.Next(10, 100), rng.Next(10, 100), rng.Next(10, 100)];
                var order = Enumerable.Range(0, 3).OrderBy(_ => rng.Next()).ToArray();
                var code = string.Concat(order.Select(i => fragments[i].ToString()));
                var haslo = string.Join(". ", order.Select(i => Words[i])) + ".";
                clues.Add(("screen_fragmenty", new { fragments, haslo }));
                return new(With(room, "uzbrojenie", new Condition(All:
                [
                    Eq("rod1", "ROD"), Eq("rod2", "ROD"), Eq("rod3", "ROD"), Eq("bomb_code", code),
                ])), clues, $"hasło „{haslo}”, kod {code}");
            }
            default:
                throw new ArgumentException($"puzzle '{puzzle}' has no variants; rerollable: {string.Join(", ", Rerollable)}");
        }
    }

    /// <summary>A lockdown: a new three-button puzzle at the stairs door, and the firewall now waits for it.</summary>
    public static Result Lockdown(Room room, Random rng)
    {
        var buttons = Enumerable.Range(0, 3).Select(_ => rng.Next(1, 5).ToString()).ToArray();
        var blokada = new Puzzle("blokada", new Condition(Sequence: [.. buttons.Select(b => Eq("lockdown_panel", b))]),
            [new Step(Output: "door_zejscie", Value: JsonSerializer.SerializeToElement("open"))], Name: "Blokada drzwi");
        var puzzles = room.Puzzles
            .Where(p => p.Id != "blokada")
            .Select(p => p.Id == "firewall" ? p with { After = [.. (p.After ?? []).Where(a => a != "blokada"), "blokada"] } : p)
            .Append(blokada)
            .ToArray();
        return new(room with { Puzzles = puzzles }, [("door_zejscie", "closed")], "sekwencja " + string.Join("-", buttons));
    }

    static Room With(Room room, string id, Condition when) =>
        room with { Puzzles = [.. room.Puzzles.Select(p => p.Id == id ? p with { When = when } : p)] };

    static Condition Eq(string input, string value) => new(Input: input, Is: JsonSerializer.SerializeToElement(value));
    static Condition Gt(string input, double v) => new(Input: input, Gt: Math.Round(v, 2));
    static Condition Lt(string input, double v) => new(Input: input, Lt: Math.Round(v, 2));
}
