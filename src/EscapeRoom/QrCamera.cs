using System.Diagnostics;
using System.Globalization;
using ZXing;

namespace EscapeRoom;

public class CameraConfig
{
    public required string Id { get; init; }
    public required string Url { get; init; }
    public int Width { get; init; } = 1280;
    public int Height { get; init; } = 720;
    public double Fps { get; init; } = 2;
}

/// <summary>
/// Turns an RTSP camera into an MQTT device: ffmpeg grabs grey frames, ZXing reads QR codes,
/// every newly seen code is published to <see cref="TopicFor"/> like any other sensor.
/// </summary>
public class QrCamera(CameraConfig cam, Mqtt mqtt, ILogger<QrCamera> log) : BackgroundService
{
    public static string TopicFor(string cameraId) => $"escaperoom/camera/{cameraId}/qr";

    public static string? Decode(byte[] gray, int width, int height)
    {
        var reader = new BarcodeReaderGeneric { Options = { PossibleFormats = [BarcodeFormat.QR_CODE], TryHarder = true } };
        return reader.Decode(new RGBLuminanceSource(gray, width, height, RGBLuminanceSource.BitmapFormat.Gray8))?.Text;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var psi = new ProcessStartInfo("ffmpeg") { RedirectStandardOutput = true };
        string[] transport = cam.Url.StartsWith("rtsp", StringComparison.OrdinalIgnoreCase) ? ["-rtsp_transport", "tcp"] : [];
        foreach (var a in (string[])["-hide_banner", "-loglevel", "error", "-nostdin", .. transport, "-i", cam.Url,
                     "-vf", string.Create(CultureInfo.InvariantCulture, $"fps={cam.Fps},scale={cam.Width}:{cam.Height}"),
                     "-pix_fmt", "gray", "-f", "rawvideo", "-"])
            psi.ArgumentList.Add(a);

        string? last = null;
        var misses = 0;
        var frame = new byte[cam.Width * cam.Height];
        while (!ct.IsCancellationRequested)
        {
            Process? ffmpeg = null;
            try
            {
                ffmpeg = Process.Start(psi)!;
                var stdout = ffmpeg.StandardOutput.BaseStream;
                while (true)
                {
                    await stdout.ReadExactlyAsync(frame, ct);
                    var text = Decode(frame, cam.Width, cam.Height);
                    // Code out of view for ~3 s: showing it again (e.g. in the next game) must count again.
                    if (text is null && ++misses >= cam.Fps * 3) last = null;
                    if (text is null || text == last) continue;
                    misses = 0;
                    log.LogInformation("Camera {Id} read {Text}", cam.Id, text);
                    await mqtt.Publish(TopicFor(cam.Id), text);
                    last = text; // only after publishing, so a broker outage doesn't swallow the code
                }
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                log.LogWarning("Camera {Id}: {Error}; restarting in 5 s", cam.Id, ex.Message);
            }
            finally
            {
                if (ffmpeg is { HasExited: false }) ffmpeg.Kill();
                ffmpeg?.Dispose();
            }
            await Task.Delay(TimeSpan.FromSeconds(5), ct);
        }
    }
}
