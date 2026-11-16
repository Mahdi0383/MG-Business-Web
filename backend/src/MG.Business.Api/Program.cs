using MG.Business.Api.Endpoints;
using MG.Business.Api.Security;
using MG.Business.Infrastructure.Identity;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.AspNetCore.RateLimiting;
using System.Threading.RateLimiting;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddProblemDetails();
 builder.Services.AddHealthChecks();

var allowedOrigins = builder.Configuration.GetSection("Cors:AllowedOrigins").Get<string[]>() ?? [];
 builder.Services.AddCors(options => options.AddPolicy("frontend", policy =>
{
    if (allowedOrigins.Length > 0)
        policy.WithOrigins(allowedOrigins).AllowAnyHeader().AllowAnyMethod();
}));

var dataProtection = builder.Services.AddDataProtection().SetApplicationName("MG.Business");
var dataProtectionKeysDirectory = builder.Configuration["DataProtection:KeysDirectory"];
if (!string.IsNullOrWhiteSpace(dataProtectionKeysDirectory))
{
    Directory.CreateDirectory(dataProtectionKeysDirectory);
    dataProtection.PersistKeysToFileSystem(new DirectoryInfo(dataProtectionKeysDirectory));
}

builder.Services.AddRateLimiter(options =>
{
    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.AddPolicy("auth", httpContext =>
        RateLimitPartition.GetFixedWindowLimiter(
            httpContext.Connection.RemoteIpAddress?.ToString() ?? "unknown",
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = 10,
                Window = TimeSpan.FromMinutes(1),
                QueueLimit = 0,
                AutoReplenishment = true
            }));
});

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

builder.Services.AddIdentityApiEndpoints<ApplicationUser>(options =>
{
    options.User.RequireUniqueEmail = true;
    options.Password.RequiredLength = 12;
    options.Password.RequireDigit = true;
    options.Password.RequireLowercase = true;
    options.Password.RequireUppercase = true;
    options.Password.RequireNonAlphanumeric = true;
    options.Lockout.AllowedForNewUsers = true;
    options.Lockout.MaxFailedAccessAttempts = 5;
    options.Lockout.DefaultLockoutTimeSpan = TimeSpan.FromMinutes(15);
}).AddEntityFrameworkStores<BusinessDbContext>();

builder.Services.AddScoped<TenantContext>();
builder.Services.AddScoped<ITenantContext>(services => services.GetRequiredService<TenantContext>());
builder.Services.AddScoped<IAuthorizationHandler, TenantResolvedHandler>();
builder.Services.AddScoped<IAuthorizationHandler, TenantRoleHandler>();
builder.Services.AddAuthorization(options =>
{
    options.AddPolicy("TenantMember", policy =>
    {
        policy.RequireAuthenticatedUser();
        policy.AddRequirements(new TenantResolvedRequirement());
    });
});

var app = builder.Build();

app.UseExceptionHandler();
if (app.Environment.IsProduction())
{
    app.UseHsts();
    app.UseHttpsRedirection();
}
app.UseRouting();
if (allowedOrigins.Length > 0)
    app.UseCors("frontend");
app.UseRateLimiter();
app.UseAuthentication();
app.UseMiddleware<TenantResolutionMiddleware>();
app.UseAuthorization();

app.MapGroup("/auth").RequireRateLimiting("auth").MapIdentityApi<ApplicationUser>();
app.MapTenantContextEndpoints();
app.MapTenantEndpoints();
app.MapInventoryEndpoints();
app.MapAccountingEndpoints();

app.MapGet("/health/live", () => Results.Ok(new { status = "live", service = "mg-business-api" }))
   .WithName("Liveness");
app.MapGet("/health/ready", async (BusinessDbContext db, ILogger<Program> logger, CancellationToken cancellationToken) =>
{
    try
    {
        var connected = await db.Database.CanConnectAsync(cancellationToken);
        return connected
            ? Results.Ok(new { status = "ready", database = "connected" })
            : Results.Problem(title: "Database unavailable", statusCode: StatusCodes.Status503ServiceUnavailable);
    }
    catch (Exception exception)
    {
        logger.LogError(exception, "Database readiness check failed.");
        return Results.Problem(title: "Database unavailable", statusCode: StatusCodes.Status503ServiceUnavailable);
    }
}).WithName("Readiness");

app.Run();

public partial class Program { }
