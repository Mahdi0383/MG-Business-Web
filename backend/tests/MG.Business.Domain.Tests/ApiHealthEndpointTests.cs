using MG.Business.Infrastructure.Persistence;
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
        using var client = factory.CreateClient();

        var response = await client.GetAsync("/health/ready");

        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
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
