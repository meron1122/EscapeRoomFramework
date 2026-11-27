using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace EscapeRoom;

/// <summary>
/// Text to speech for the room's characters: ElevenLabs directly, or its models through OpenRouter
/// (Voice:Provider=openrouter). Each line becomes an mp3 that the room's speakers fetch by URL.
/// Lines are cached by model, voice and text, so a repeated line costs nothing. Without a key, lines stay text.
/// </summary>
public class Voice(IConfiguration config, ILogger<Voice> log)
{
    sealed class Budget { public DateOnly Day; public int Characters; public bool Warned; }
    readonly Budget spent = new();

    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(30) };

    public string Folder { get; } = config["Voice:Cache"] ?? Path.Combine(Path.GetTempPath(), "escaperoom-voice");

    /// <summary>The URL of the spoken line, or null when there is no voice, no key or the service failed.</summary>
    public async Task<string?> Speak(string? voiceId, string text)
    {
        // On a Sprite the key lives in a Connector: BaseUrl points at its gateway, which adds the key on the way out.
        var baseUrl = config["Voice:BaseUrl"];
        var openRouter = config["Voice:Provider"] == "openrouter";
        var key = Environment.GetEnvironmentVariable(openRouter ? "OPENROUTER_API_KEY" : "ELEVENLABS_API_KEY");
        if (string.IsNullOrEmpty(voiceId) || (string.IsNullOrEmpty(key) && string.IsNullOrEmpty(baseUrl))) return null;
        var model = config["Voice:Model"] ?? (openRouter ? "elevenlabs/eleven-v3" : "eleven_v3");
        var name = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes($"{model}|{voiceId}|{text}")))[..32] + ".mp3";
        var path = Path.Combine(Folder, name);
        if (!File.Exists(path))
        {
            // Voice:MaxCharactersPerDay caps what a public demo can spend on speech (0 = no cap). Cached lines are free.
            var cap = config.GetValue("Voice:MaxCharactersPerDay", 0);
            lock (spent)
            {
                var today = DateOnly.FromDateTime(DateTime.UtcNow);
                if (spent.Day != today) (spent.Day, spent.Characters) = (today, 0);
                if (cap > 0 && spent.Characters + text.Length > cap)
                {
                    if (!spent.Warned) log.LogWarning("Voice budget of {Cap} characters for today is used up; lines stay text", cap);
                    spent.Warned = true;
                    return null;
                }
                spent.Characters += text.Length;
                spent.Warned = false;
            }
            // OpenRouter takes the same ElevenLabs voice ids, on its own speech endpoint.
            var root = string.IsNullOrEmpty(baseUrl) ? (openRouter ? "https://openrouter.ai/api" : "https://api.elevenlabs.io") : baseUrl.TrimEnd('/');
            var url = openRouter ? $"{root}/v1/audio/speech" : $"{root}/v1/text-to-speech/{Uri.EscapeDataString(voiceId)}?output_format=mp3_44100_128";
            var body = openRouter
                ? JsonSerializer.Serialize(new { model, voice = voiceId, input = text, response_format = "mp3" })
                : JsonSerializer.Serialize(new { text, model_id = model });
            using var request = new HttpRequestMessage(HttpMethod.Post, url)
            {
                // A fixed-length body, not chunked: gateways in front of the API don't all accept chunked uploads.
                Content = new StringContent(body, Encoding.UTF8, "application/json"),
            };
            if (!string.IsNullOrEmpty(key)) request.Headers.Add(openRouter ? "Authorization" : "xi-api-key", openRouter ? $"Bearer {key}" : key);
            try
            {
                using var response = await Http.SendAsync(request);
                if (!response.IsSuccessStatusCode)
                {
                    var error = await response.Content.ReadAsStringAsync();
                    log.LogWarning("Voice {Voice}: HTTP {Status} {Body}", voiceId, (int)response.StatusCode, error[..Math.Min(300, error.Length)]);
                    return null;
                }
                // A gateway can answer 200 with an error page; only audio becomes a line.
                if (response.Content.Headers.ContentType?.MediaType?.StartsWith("audio/") != true)
                {
                    log.LogWarning("Voice {Voice}: the answer is {Type}, not audio", voiceId, response.Content.Headers.ContentType);
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
