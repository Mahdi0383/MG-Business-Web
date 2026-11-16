using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.Configuration;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class ApiHealthEndpointTests
{
    [Fact]
    public async Task LiveEndpoint_ReturnsHealthyResponse()
    {
        using var factory = CreateFactory();
        using var client = factory.CreateClient();

        var response = await client.GetAsync("/health/live");

        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync();
        Assert.Contains("live", body, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task ReadyEndpoint_ReturnsHealthyWhenDatabaseIsReachable()
    {
        using var factory = CreateFactory();
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }
        using var client = factory.CreateClient();

        var response = await client.GetAsync("/health/ready");

        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public void ProductionStartup_RejectsDevelopmentSqliteFallback()
    {
        var keyDirectory = Path.Combine(Path.GetTempPath(), $"mg-business-production-keys-{Guid.NewGuid():N}");
        using var factory = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Production");
            builder.ConfigureAppConfiguration((_, configuration) =>
                configuration.AddInMemoryCollection(new Dictionary<string, string?>
                {
                    ["Database:Provider"] = "Sqlite",
                    ["ConnectionStrings:Business"] = "Data Source=production-misconfigured.db",
                    ["DataProtection:KeysDirectory"] = keyDirectory,
                    ["Cors:AllowedOrigins:0"] = "https://app.example.com",
                    ["ForwardedHeaders:KnownProxies:0"] = "127.0.0.1"
                }));
        });

        var exception = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(exception);
        Assert.Contains("Production deployments must explicitly configure PostgreSQL", exception!.ToString());
        if (Directory.Exists(keyDirectory))
            Directory.Delete(keyDirectory, recursive: true);
    }

    private static WebApplicationFactory<Program> CreateFactory()
    {
        var databasePath = Path.Combine(Path.GetTempPath(), $"mg-business-health-{Guid.NewGuid():N}.db");
        var connectionString = $"Data Source={databasePath}";

        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<IDbContextOptionsConfiguration<BusinessDbContext>>();
                services.RemoveAll<DbContextOptions<BusinessDbContext>>();
                services.RemoveAll<BusinessDbContext>();
                services.AddDbContext<BusinessDbContext>(options => options.UseSqlite(connectionString));
            }));
    }
}
