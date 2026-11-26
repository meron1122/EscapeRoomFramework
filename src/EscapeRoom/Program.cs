using System.Text.Json;
using EscapeRoom;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);
var connectionString = builder.Configuration.GetConnectionString("Postgres")
    ?? throw new InvalidOperationException("Missing ConnectionStrings:Postgres");
builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(connectionString));
builder.Services.AddSingleton<Mqtt>();
builder.Services.AddSingleton<Safety>();
builder.Services.AddSingleton<Engine>();
builder.Services.AddSingleton<IHostedService>(sp => sp.GetRequiredService<Mqtt>());
builder.Services.AddSingleton<IHostedService>(sp => sp.GetRequiredService<Engine>());
builder.Services.AddHostedService<GameMaster>();
// AddHostedService would keep only one QrCamera (it dedupes by type), hence AddSingleton per camera.
foreach (var cam in builder.Configuration.GetSection("Cameras").Get<CameraConfig[]>() ?? [])
    builder.Services.AddSingleton<IHostedService>(sp =>
        new QrCamera(cam, sp.GetRequiredService<Mqtt>(), sp.GetRequiredService<ILogger<QrCamera>>()));
builder.Services.AddMcpServer().WithHttpTransport(o => o.Stateless = true).WithTools<McpTools>();

var app = builder.Build();
var db = app.Services.GetRequiredService<NpgsqlDataSource>();
// shortcut: idempotent schema instead of migrations; switch to migrations once a released schema has to change
await db.Exec(Engine.Schema);
await db.Exec(Safety.Schema);
var safety = app.Services.GetRequiredService<Safety>();
await safety.Start(); // before app.Run, so no game can publish while the safety config is still unknown

app.MapMcp("/mcp");

// Safety has its own endpoints; nothing in the game API or MCP can reach them.
app.MapGet("/safety", () => Results.Json(safety.Current, Engine.Json));
app.MapGet("/safety/log", async () => await safety.History());
app.MapPut("/safety", async (HttpRequest request) =>
{
    var (status, errors) = await safety.Change(
        request.Headers["X-Safety-Token"], request.HttpContext.Connection.RemoteIpAddress?.ToString(), request.Body);
    return status == 200 ? Results.NoContent() : Results.Json(new { errors }, statusCode: status);
});

app.MapPut("/rooms/{id}", async (string id, HttpRequest request, Engine engine) =>
{
    Room? room;
    try
    {
        room = await JsonSerializer.DeserializeAsync<Room>(request.Body, Engine.Json);
    }
    catch (JsonException ex)
    {
        return Results.BadRequest(new { errors = new[] { ex.Message } });
    }
    if (room is null) return Results.BadRequest(new { errors = new[] { "empty definition" } });
    var errors = await engine.SaveRoom(id, _ => room);
    return errors.Length > 0 ? Results.BadRequest(new { errors }) : Results.NoContent();
});

app.MapGet("/rooms/{id}", async (string id, Engine engine) =>
    await engine.RoomJson(id) is { } json ? Results.Text(json, "application/json") : Results.NotFound());

app.MapPost("/rooms/{id}/start", async (string id, Engine engine) =>
    await engine.Start(id) is { } session ? Results.Ok(new { session }) : Results.NotFound());

app.MapPost("/rooms/{id}/stop", async (string id, Engine engine) =>
    await engine.Stop(id) ? Results.NoContent() : Results.NotFound());

app.MapPost("/rooms/{id}/puzzles/{puzzle}/solve", async (string id, string puzzle, Engine engine) =>
    await engine.Solve(id, puzzle) ? Results.NoContent() : Results.NotFound());

app.MapGet("/rooms/{id}/session", async (string id, Engine engine) =>
    await engine.Status(id) is { } status ? Results.Ok(status) : Results.NotFound());

app.Run();
