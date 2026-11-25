using System.Text.Json;
using ZXing;
using ZXing.QrCode;

namespace EscapeRoom.Tests;

public class GameTests
{
    static readonly DateTimeOffset Now = DateTimeOffset.UnixEpoch;

    static Room Lab() => JsonSerializer.Deserialize<Room>(
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "examples", "lab.json")), Engine.Json)!;

    static string[] Feed(Game g, string input, string value) => g.Input(input, value, Now).Select(p => p.Id).ToArray();

    [Fact]
    public void Example_room_is_valid() => Assert.Empty(Lab().Validate());

    [Fact]
    public void Lab_scenario_plays_through()
    {
        var g = new Game(Lab(), new GameState());
        Assert.Empty(Feed(g, "flaskLeft", "false")); // trap still locked, but the sensor state is remembered
        Assert.Empty(Feed(g, "qr", "WRONG"));
        Assert.Equal(["qr-code"], Feed(g, "qr", "LAB-7731"));
        Assert.Empty(Feed(g, "flaskRight", "false"));
        Assert.Equal(["flasks"], Feed(g, "thermo", "41.5"));

        // red, blue, red, green with a wrong start and an irrelevant double-click in between
        foreach (var (button, action) in new[] { ("btnRed", "SINGLE"), ("btnRed", "SINGLE"), ("btnBlue", "DOUBLE"), ("btnBlue", "SINGLE"), ("btnRed", "SINGLE") })
            Assert.Empty(Feed(g, button, action));
        Assert.Equal(["buttons"], Feed(g, "btnGreen", "SINGLE"));

        Assert.False(g.Finished);
        Assert.Equal(["exit"], Feed(g, "exitKey", "ON"));
        Assert.True(g.Finished);
    }

    [Fact]
    public void Gm_solve_cascades_into_puzzles_already_satisfied()
    {
        var room = Lab();
        var g = new Game(room, new GameState());
        Feed(g, "flaskLeft", "false");
        Feed(g, "flaskRight", "false");
        Feed(g, "thermo", "50");
        Assert.Equal(["qr-code", "flasks"], g.Solve("qr-code", Now).Select(p => p.Id));
        Assert.Equal("active", g.Status(room.Puzzles.Single(p => p.Id == "buttons")));
        Assert.Equal("locked", g.Status(room.Puzzles.Single(p => p.Id == "exit")));
    }

    [Fact]
    public void Rewiring_mid_game_keeps_progress_and_solves_what_already_holds()
    {
        var room = Lab();
        var g = new Game(room, new GameState());
        Feed(g, "qr", "LAB-7731");
        Feed(g, "thermo", "35");
        foreach (var button in new[] { "btnRed", "btnBlue", "btnRed" }) Feed(g, button, "SINGLE");

        // Live edit: the button code shrinks to blue, red, and a new puzzle wires the thermometer to the lamp.
        var buttons = room.Puzzles.Single(p => p.Id == "buttons");
        var rewired = room with
        {
            Puzzles =
            [
                .. room.Puzzles.Select(p => p == buttons ? p with { When = new(Sequence: buttons.When!.Sequence![1..3]) } : p),
                new("warm", new(Input: "thermo", Gt: 30), [new(Output: "lamp", Value: JsonDocument.Parse("\"ON\"").RootElement)], ["qr-code"]),
            ],
        };
        Assert.Empty(rewired.Validate());
        var live = new Game(rewired, g.State);
        Assert.Equal(["buttons", "warm"], live.Settle(Now).Select(p => p.Id));
        Assert.Equal("solved", live.Status(rewired.Puzzles[0]));
        Assert.Equal("active", live.Status(rewired.Puzzles.Single(p => p.Id == "flasks")));
    }

    [Fact]
    public void Validation_reports_broken_definitions()
    {
        var room = Lab() with
        {
            Puzzles =
            [
                new("a", new Condition(Input: "nope"), After: ["b"]),
                new("b", After: ["a"]),
                new("c", new Condition(All: [new(Sequence: [new(Input: "qr")])])),
            ],
            Start = [new Step(Output: "ghost", Value: JsonDocument.Parse("1").RootElement)],
        };
        var errors = room.Validate().ToArray();
        Assert.Contains(errors, e => e.Contains("unknown input 'nope'"));
        Assert.Contains(errors, e => e.Contains("cycle") && e.Contains("a, b"));
        Assert.Contains(errors, e => e.Contains("sequence must be the whole"));
        Assert.Contains(errors, e => e.Contains("unknown output 'ghost'"));
    }

    [Fact]
    public void Typo_in_definition_is_rejected_not_ignored() =>
        Assert.Throws<JsonException>(() => JsonSerializer.Deserialize<Condition>("""{"input":"qr","equal":"x"}""", Engine.Json));

    [Fact]
    public void Presets_map_to_ecosystem_topics()
    {
        var lab = Lab();
        Assert.Equal(("stat/trap-buttons/RESULT", "Button1.Action"), lab.Inputs["btnRed"].AsInput());
        Assert.Equal(("trap-panel/sensor/thermometer/state", (string?)null), lab.Inputs["thermo"].AsInput());
        Assert.Equal(("zigbee2mqtt/trap_flask_left", "contact"), lab.Inputs["flaskLeft"].AsInput());
        Assert.Equal(("escaperoom/camera/lab-cam/qr", (string?)null), lab.Inputs["qr"].AsInput());
        Assert.Equal("cmnd/trap-relay/Power1", lab.Outputs["trapLock"].AsOutput().Topic);
        Assert.Equal("locker2/switch/solenoid/command", lab.Outputs["locker2"].AsOutput().Topic);
        Assert.Equal("zigbee2mqtt/lab_lamp/set", lab.Outputs["lamp"].AsOutput().Topic);
    }

    [Fact]
    public void Payloads_are_extracted_and_built()
    {
        Assert.Equal("SINGLE", Payload.Extract("""{"Button1":{"Action":"SINGLE"}}""", "Button1.Action"));
        Assert.Equal("false", Payload.Extract("""{"contact":false,"battery":90}""", "contact"));
        Assert.Null(Payload.Extract("""{"POWER":"ON"}""", "Button1.Action"));
        Assert.Null(Payload.Extract("ON", "contact"));
        Assert.Equal("ON", Payload.Extract(" ON\n", null));
        Assert.Equal("""{"state":"ON"}""", Payload.ForOutput(JsonDocument.Parse("\"ON\"").RootElement, "state"));
    }

    [Fact]
    public void Qr_code_is_read_from_a_grey_frame()
    {
        const int w = 640, h = 360;
        var frame = Enumerable.Repeat((byte)255, w * h).ToArray();
        var qr = new QRCodeWriter().encode("LAB-7731", BarcodeFormat.QR_CODE, 200, 200);
        for (var y = 0; y < qr.Height; y++)
            for (var x = 0; x < qr.Width; x++)
                if (qr[x, y]) frame[(y + 80) * w + x + 220] = 0;
        Assert.Equal("LAB-7731", QrCamera.Decode(frame, w, h));
    }
}
