using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace EscapeRoom;

/// <summary>
/// Text to speech for the room's characters (ElevenLabs). Each line becomes an mp3 that the room's speakers fetch
/// by URL. Lines are cached by model, voice and text, so a repeated line costs nothing. Without a key, lines stay text.
/// </summary>
public class Voice(IConfiguration config, ILogger<Voice> log)
{
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(30) };

    public string Folder { get; } = config["Voice:Cache"] ?? Path.Combine(Path.GetTempPath(), "escaperoom-voice");

    /// <summary>The URL of the spoken line, or null when there is no voice, no key or the service failed.</summary>
    public async Task<string?> Speak(string? voiceId, string text)
    {
        // On a Sprite the key lives in a Connector: BaseUrl points at its gateway, which adds the key on the way out.
        var baseUrl = config["Voice:BaseUrl"];
        var key = Environment.GetEnvironmentVariable("ELEVENLABS_API_KEY");
        if (string.IsNullOrEmpty(voiceId) || (string.IsNullOrEmpty(key) && string.IsNullOrEmpty(baseUrl))) return null;
        var model = config["Voice:Model"] ?? "eleven_v3";
        var name = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes($"{model}|{voiceId}|{text}")))[..32] + ".mp3";
        var path = Path.Combine(Folder, name);
        if (!File.Exists(path))
        {
            var url = $"{(string.IsNullOrEmpty(baseUrl) ? "https://api.elevenlabs.io" : baseUrl.TrimEnd('/'))}/v1/text-to-speech/{Uri.EscapeDataString(voiceId)}?output_format=mp3_44100_128";
            using var request = new HttpRequestMessage(HttpMethod.Post, url)
            {
                // A fixed-length body, not chunked: gateways in front of the API don't all accept chunked uploads.
                Content = new StringContent(JsonSerializer.Serialize(new { text, model_id = model }), Encoding.UTF8, "application/json"),
            };
            if (!string.IsNullOrEmpty(key)) request.Headers.Add("xi-api-key", key);
            try
            {
                using var response = await Http.SendAsync(request);
                if (!response.IsSuccessStatusCode)
                {
                    var body = await response.Content.ReadAsStringAsync();
                    log.LogWarning("Voice {Voice}: HTTP {Status} {Body}", voiceId, (int)response.StatusCode, body[..Math.Min(300, body.Length)]);
                    return null;
                }
                Directory.CreateDirectory(Folder);
                var part = $"{path}.{Guid.NewGuid():N}.part";
                await File.WriteAllBytesAsync(part, await response.Content.ReadAsByteArrayAsync());
                File.Move(part, path, overwrite: true);
            }
            catch (Exception ex)
            {
                log.LogWarning("Voice {Voice}: {Error}", voiceId, ex.Message);
                return null;
            }
        }
        return $"{(config["Voice:PublicUrl"] ?? "http://localhost:5201").TrimEnd('/')}/voice/{name}";
    }
}
