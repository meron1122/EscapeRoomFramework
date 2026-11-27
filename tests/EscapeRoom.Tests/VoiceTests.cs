using System.Net;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;

namespace EscapeRoom.Tests;

public class VoiceTests
{
    /// <summary>Through OpenRouter a line goes to /v1/audio/speech with the voice id; an answer that is not audio is no line.</summary>
    [Theory]
    [InlineData("audio/mpeg", true)]
    [InlineData("text/html", false)]
    public async Task OpenRouter_speech_is_asked_for_and_only_audio_is_kept(string answerType, bool spoken)
    {
        var port = 20000 + Random.Shared.Next(20000);
        using var server = new HttpListener { Prefixes = { $"http://127.0.0.1:{port}/" } };
        server.Start();
        var folder = Path.Combine(Path.GetTempPath(), "voice-test-" + Guid.NewGuid().ToString("N"));
        var voice = new Voice(new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["Voice:Provider"] = "openrouter", ["Voice:BaseUrl"] = $"http://127.0.0.1:{port}", ["Voice:Cache"] = folder,
            ["Voice:PublicUrl"] = "https://room.example",
        }).Build(), NullLogger<Voice>.Instance);

        var speaking = voice.Speak("voice-1", "Nobody moves.");
        var call = await server.GetContextAsync();
        var asked = JsonDocument.Parse(await new StreamReader(call.Request.InputStream).ReadToEndAsync()).RootElement;
        call.Response.ContentType = answerType;
        call.Response.Close([1, 2, 3], willBlock: true);
        var url = await speaking;

        Assert.Equal("/v1/audio/speech", call.Request.Url!.AbsolutePath);
        Assert.Equal("elevenlabs/eleven-v3", asked.GetProperty("model").GetString());
        Assert.Equal("voice-1", asked.GetProperty("voice").GetString());
        Assert.Equal("Nobody moves.", asked.GetProperty("input").GetString());
        if (spoken) Assert.Matches(@"^https://room\.example/voice/[0-9a-f]{32}\.mp3$", url);
        else Assert.Null(url);
        if (Directory.Exists(folder)) Directory.Delete(folder, true);
    }
}
