using System.Collections.Concurrent;
using MQTTnet;
using MQTTnet.Protocol;

namespace EscapeRoom;

/// <summary>The one broker connection everything goes through: Tasmota, ESPHome, zigbee2mqtt, cameras. Reconnects forever.</summary>
public class Mqtt(IConfiguration config, ILogger<Mqtt> log) : BackgroundService
{
    readonly IMqttClient client = new MqttClientFactory().CreateMqttClient();

    public event Func<string, string, Task>? Received;

    /// <summary>Last payload per topic since startup, so new devices can be discovered and wired in.</summary>
    // shortcut: unbounded and in memory; fine for one venue's broker
    public ConcurrentDictionary<string, (string Payload, DateTimeOffset At)> Seen { get; } = new();

    public Task Publish(string topic, string payload) =>
        client.PublishStringAsync(topic, payload, MqttQualityOfServiceLevel.AtLeastOnce);

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        client.ApplicationMessageReceivedAsync += e =>
        {
            var (topic, payload) = (e.ApplicationMessage.Topic, e.ApplicationMessage.ConvertPayloadToString());
            Seen[topic] = (payload, DateTimeOffset.UtcNow);
            return Received?.Invoke(topic, payload) ?? Task.CompletedTask;
        };

        var options = new MqttClientOptionsBuilder()
            .WithTcpServer(config["Mqtt:Host"] ?? "localhost", config.GetValue("Mqtt:Port", 1883))
            .WithClientId($"escaperoom-{Environment.MachineName}");
        if (config["Mqtt:User"] is { } user) options.WithCredentials(user, config["Mqtt:Password"]);

        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (!await client.TryPingAsync(ct))
                {
                    await client.ConnectAsync(options.Build(), ct);
                    // shortcut: subscribes to every topic; fine for one venue's broker, narrow it if traffic grows
                    await client.SubscribeAsync(new MqttClientSubscribeOptionsBuilder().WithTopicFilter("#").Build(), ct);
                    log.LogInformation("MQTT connected");
                }
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                log.LogWarning("MQTT: {Error}; retrying", ex.Message);
            }
            await Task.Delay(TimeSpan.FromSeconds(5), ct);
        }
    }
}
