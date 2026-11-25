using System.Text.Json;
using EscapeRoom;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);
var connectionString = builder.Configuration.GetConnectionString("Postgres")
    ?? throw new InvalidOperationException("Missing ConnectionStrings:Postgres");
builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(connectionString));
builder.Services.AddSingleton<Mqtt>();
builder.Services.AddSingleton<Engine>();
builder.Services.AddSingleton<IHostedService>(sp => sp.GetRequiredService<Mqtt>());
builder.Services.AddSingleton<IHostedService>(sp => sp.GetRequiredService<Engine>());
// AddHostedService would keep only one QrCamera (it dedupes by type), hence AddSingleton per camera.
foreach (var cam in builder.Configuration.GetSection("Cameras").Get<CameraConfig[]>() ?? [])
    builder.Services.AddSingleton<IHostedService>(sp =>
        new QrCamera(cam, sp.GetRequiredService<Mqtt>(), sp.GetRequiredService<ILogger<QrCamera>>()));

var app = builder.Build();
var db = app.Services.GetRequiredService<NpgsqlDataSource>();
// shortcut: idempotent schema instead of migrations; switch to migrations once a released schema has to change
await db.Exec(Engine.Schema);

app.MapPut("/rooms/{id}", async (string id, HttpRequest request) =>
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
    var errors = room?.Validate().ToArray() ?? ["empty definition"];
    if (errors.Length > 0) return Results.BadRequest(new { errors });
    await db.Exec("""
        insert into rooms (id, definition) values ($1, $2::jsonb)
        on conflict (id) do update set definition = excluded.definition, updated_at = now()
        """, id, JsonSerializer.Serialize(room, Engine.Json));
    return Results.NoContent();
});

app.MapGet("/rooms/{id}", async (string id) =>
    await db.Scalar("select definition from rooms where id = $1", id) is string json
        ? Results.Text(json, "application/json")
        : Results.NotFound());

app.MapPost("/rooms/{id}/start", async (string id, Engine engine) =>
    await engine.Start(id) is { } session ? Results.Ok(new { session }) : Results.NotFound());

app.MapPost("/rooms/{id}/stop", async (string id, Engine engine) =>
    await engine.Stop(id) ? Results.NoContent() : Results.NotFound());

app.MapPost("/rooms/{id}/puzzles/{puzzle}/solve", async (string id, string puzzle, Engine engine) =>
    await engine.Solve(id, puzzle) ? Results.NoContent() : Results.NotFound());

app.MapGet("/rooms/{id}/session", async (string id, Engine engine) =>
    await engine.Status(id) is { } status ? Results.Ok(status) : Results.NotFound());

app.Run();
