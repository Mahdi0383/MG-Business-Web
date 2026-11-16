using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddProblemDetails();
builder.Services.AddHealthChecks();

var provider = builder.Configuration["Database:Provider"] ?? "Sqlite";
var connectionString = builder.Configuration.GetConnectionString("Business")
    ?? "Data Source=mg-business-dev.db";

builder.Services.AddDbContext<BusinessDbContext>(options =>
{
    if (string.Equals(provider, "PostgreSql", StringComparison.OrdinalIgnoreCase))
        options.UseNpgsql(connectionString);
    else
        options.UseSqlite(connectionString);
});

var app = builder.Build();

app.UseExceptionHandler();
app.MapGet("/health/live", () => Results.Ok(new { status = "live", service = "mg-business-api" }))
   .WithName("Liveness");
app.MapGet("/health/ready", async (BusinessDbContext db, CancellationToken cancellationToken) =>
{
    try
    {
        var connected = await db.Database.CanConnectAsync(cancellationToken);
        return connected
            ? Results.Ok(new { status = "ready", database = "connected" })
            : Results.Problem(title: "Database unavailable", statusCode: StatusCodes.Status503ServiceUnavailable);
    }
    catch
    {
        return Results.Problem(title: "Database unavailable", statusCode: StatusCodes.Status503ServiceUnavailable);
    }
}).WithName("Readiness");

app.Run();

public partial class Program { }
