using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class TenantSecurityIntegrationTests
{
    [Fact]
    public async Task TenantContext_RequiresAuthenticationAndActiveMembership()
    {
        using var factory = CreateFactory();
        using var anonymousClient = factory.CreateClient();

        var anonymousResponse = await anonymousClient.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Unauthorized, anonymousResponse.StatusCode);

        var tenant = new Tenant("Security Test", $"security-{Guid.NewGuid():N}");
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
            db.Tenants.Add(tenant);
            await db.SaveChangesAsync();
        }

        using var client = await RegisterAndLoginAsync(factory, "security-user@mg.test");
        var missingTenant = await client.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, missingTenant.StatusCode);

        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.Id.ToString());
        var noMembership = await client.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, noMembership.StatusCode);

        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            var user = await db.Users.SingleAsync(item => item.Email == "security-user@mg.test");
            db.TenantMemberships.Add(new TenantMembership(tenant.Id, user.Id, TenantRole.Cashier));
            await db.SaveChangesAsync();
        }

        var allowed = await client.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.OK, allowed.StatusCode);
        var payload = await allowed.Content.ReadFromJsonAsync<TenantContextPayload>();
        Assert.Equal(tenant.Id, payload!.TenantId);

        client.DefaultRequestHeaders.Remove("X-MG-Tenant");
        client.DefaultRequestHeaders.Add("X-MG-Tenant", Guid.NewGuid().ToString());
        var otherTenant = await client.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, otherTenant.StatusCode);
    }

    [Fact]
    public async Task TenantContext_RejectsInactiveMembership()
    {
        using var factory = CreateFactory();
        var tenant = new Tenant("Inactive Test", $"inactive-{Guid.NewGuid():N}");
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
            db.Tenants.Add(tenant);
            await db.SaveChangesAsync();
        }

        using var client = await RegisterAndLoginAsync(factory, "inactive-user@mg.test");
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            var user = await db.Users.SingleAsync(item => item.Email == "inactive-user@mg.test");
            db.TenantMemberships.Add(new TenantMembership(tenant.Id, user.Id, TenantRole.Viewer));
            await db.SaveChangesAsync();
            var membership = await db.TenantMemberships.SingleAsync();
            membership.Deactivate();
            await db.SaveChangesAsync();
        }
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.Id.ToString());
        var response = await client.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
    }

    private static async Task<HttpClient> RegisterAndLoginAsync(WebApplicationFactory<Program> factory, string email)
    {
        var client = factory.CreateClient();
        var register = await client.PostAsJsonAsync("/auth/register", new
        {
            email,
            password = "Strong!Passw0rd"
        });
        Assert.Equal(HttpStatusCode.OK, register.StatusCode);

        var login = await client.PostAsJsonAsync("/auth/login", new
        {
            email,
            password = "Strong!Passw0rd",
            useCookies = false
        });
        Assert.Equal(HttpStatusCode.OK, login.StatusCode);

        var tokens = await login.Content.ReadFromJsonAsync<TokenResponse>();
        Assert.False(string.IsNullOrWhiteSpace(tokens!.AccessToken));
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", tokens.AccessToken);
        return client;
    }

    private static WebApplicationFactory<Program> CreateFactory()
    {
        var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=:memory:");
        connection.Open();
        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<DbContextOptions<BusinessDbContext>>();
                services.RemoveAll<IDbContextOptionsConfiguration<BusinessDbContext>>();
                services.RemoveAll<BusinessDbContext>();
                services.AddSingleton(connection);
                services.AddDbContext<BusinessDbContext>((provider, options) =>
                    options.UseSqlite(provider.GetRequiredService<Microsoft.Data.Sqlite.SqliteConnection>()));
            }));
    }

    private static MG.Business.Infrastructure.Identity.ApplicationUser NewUser(string email) => new()
    {
        Id = Guid.NewGuid(),
        UserName = email,
        NormalizedUserName = email.ToUpperInvariant(),
        Email = email,
        NormalizedEmail = email.ToUpperInvariant(),
        DisplayName = "MG Test User",
        EmailConfirmed = true
    };

    private sealed record TokenResponse(string TokenType, string AccessToken, int ExpiresIn, string RefreshToken);
    private sealed record TenantContextPayload(Guid? TenantId, Guid? BranchId, Guid? UserId, string? Role);
}
