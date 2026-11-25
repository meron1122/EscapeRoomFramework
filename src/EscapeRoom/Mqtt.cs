using MQTTnet;
using MQTTnet.Protocol;

namespace EscapeRoom;

/// <summary>The one broker connection everything goes through: Tasmota, ESPHome, zigbee2mqtt, cameras. Reconnects forever.</summary>
public class Mqtt(IConfiguration config, ILogger<Mqtt> log) : BackgroundService
{
    readonly IMqttClient client = new MqttClientFactory().CreateMqttClient();

    public event Func<string, string, Task>? Received;

    public Task Publish(string topic, string payload) =>
        client.PublishStringAsync(topic, payload, MqttQualityOfServiceLevel.AtLeastOnce);

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        client.ApplicationMessageReceivedAsync += e =>
            Received?.Invoke(e.ApplicationMessage.Topic, e.ApplicationMessage.ConvertPayloadToString()) ?? Task.CompletedTask;

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
