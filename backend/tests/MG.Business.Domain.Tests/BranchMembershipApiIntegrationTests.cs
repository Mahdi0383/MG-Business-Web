using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class BranchMembershipApiIntegrationTests
{
    [Fact]
    public async Task OwnerCanCreateBranchAndAssignUserToThatBranchOnly()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Multi Branch",
            slug = $"multi-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        var branchResponse = await owner.PostAsJsonAsync("/api/branches", new { name = "Second Branch", code = "B02" });
        Assert.Equal(HttpStatusCode.Created, branchResponse.StatusCode);
        var branch = await branchResponse.Content.ReadFromJsonAsync<BranchResponse>();

        using var staff = await RegisterAndLoginAsync(factory, $"staff-{Guid.NewGuid():N}@mg.test");
        var email = await GetEmailFromTokenlessHelper(factory);
        var staffEmail = staff.DefaultRequestHeaders.Contains("X-Staff-Email") ? staff.DefaultRequestHeaders.GetValues("X-Staff-Email").Single() : email;

        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = TenantRole.Cashier,
            branchId = branch!.Id
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        staff.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        staff.DefaultRequestHeaders.Add("X-MG-Branch", branch.Id.ToString());
        var context = await staff.GetFromJsonAsync<TenantContextResponse>("/api/context");
        Assert.Equal("Cashier", context!.Role);
        Assert.Equal(branch.Id, context.BranchId);

        staff.DefaultRequestHeaders.Remove("X-MG-Branch");
        staff.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var otherBranch = await staff.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, otherBranch.StatusCode);
    }

    private static async Task<string> GetEmailFromTokenlessHelper(WebApplicationFactory<Program> factory)
    {
        using var scope = factory.Services.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
        return await db.Users.OrderByDescending(x => x.CreatedAtUtc).Select(x => x.Email!).FirstAsync();
    }

    private static async Task<HttpClient> RegisterAndLoginAsync(WebApplicationFactory<Program> factory, string email)
    {
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }
        var client = factory.CreateClient();
        var register = await client.PostAsJsonAsync("/auth/register", new { email, password = "Strong!Passw0rd" });
        Assert.Equal(HttpStatusCode.OK, register.StatusCode);
        var login = await client.PostAsJsonAsync("/auth/login", new { email, password = "Strong!Passw0rd", useCookies = false });
        Assert.Equal(HttpStatusCode.OK, login.StatusCode);
        var tokens = await login.Content.ReadFromJsonAsync<TokenResponse>();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", tokens!.AccessToken);
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

    private sealed record TokenResponse(string TokenType, string AccessToken, int ExpiresIn, string RefreshToken);
    private sealed record TenantCreated(Guid TenantId, Guid BranchId, string Name, string Slug, string Role);
    private sealed record BranchResponse(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
    private sealed record TenantContextResponse(Guid? TenantId, Guid? BranchId, Guid? UserId, string? Role);
}
