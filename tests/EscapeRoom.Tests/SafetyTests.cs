using System.Text.Json;

namespace EscapeRoom.Tests;

public class SafetyTests
{
    static SafetyConfig Example() => JsonSerializer.Deserialize<SafetyChange>(
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "examples", "safety.json")), Engine.Json)!.Config;

    [Fact]
    public void Example_is_valid_and_separate_from_the_lab_game()
    {
        Assert.Empty(Example().Validate());
        Assert.Empty(Example().Conflicts(GameTests.Lab()));
    }

    [Theory]
    [InlineData("cmnd/lock-power/Power1")] // the safety relay itself
    [InlineData("cmnd/lock-power/Backlog")] // any other command to the same device
    [InlineData("cmnd/tasmotas/Power1")] // Tasmota broadcast reaches every device
    [InlineData("stat/lock-power/RESULT")] // impersonating its reports
    [InlineData("safety-panel/binary_sensor/emergency/state")] // faking the emergency button
    [InlineData("zigbee2mqtt/smoke_lab")] // faking the smoke detector
    [InlineData("zigbee2mqtt/bridge/request/device/remove")] // removing it from the network
    [InlineData("shellies/emergency-light/relay/0/command")]
    public void A_game_cannot_publish_to_safety_devices(string topic)
    {
        var lab = GameTests.Lab();
        var hijacked = lab with { Outputs = new(lab.Outputs) { ["sneaky"] = new Io(Topic: topic) } };
        Assert.True(Example().Protects(topic));
        Assert.Single(Example().Conflicts(hijacked));
    }

    [Theory]
    [InlineData("cmnd/lock-power-2/Power1")] // another device whose name only starts the same
    [InlineData("zigbee2mqtt/smoke_lab_2/set")]
    [InlineData("shellies/emergency-light/relay/1/command")]
    public void Unrelated_devices_stay_usable(string topic) => Assert.False(Example().Protects(topic));

    [Fact]
    public void Rule_holds_on_any_alarm_and_sequences_are_refused()
    {
        var rule = Example().Rules.Single();
        Assert.True(rule.When.Holds(new Dictionary<string, string> { ["smoke"] = "true" }));
        Assert.False(rule.When.Holds(new Dictionary<string, string> { ["smoke"] = "false", ["emergencyButton"] = "OFF" }));
        var withSequence = Example() with { Rules = [rule with { When = new(Sequence: [new(Input: "smoke")]) }] };
        Assert.Contains(withSequence.Validate(), e => e.Contains("sequence is not allowed"));
    }
}
