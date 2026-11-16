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

        var staffEmail = $"staff-{Guid.NewGuid():N}@mg.test";
        using var staff = await RegisterAndLoginAsync(factory, staffEmail);

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

        var forbiddenBranchCreate = await staff.PostAsJsonAsync("/api/branches", new { name = "Unauthorized Branch", code = "B03" });
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenBranchCreate.StatusCode);
        var forbiddenBranchDisable = await staff.PatchAsJsonAsync($"/api/branches/{branch.Id}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenBranchDisable.StatusCode);

        staff.DefaultRequestHeaders.Remove("X-MG-Branch");
        staff.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var otherBranch = await staff.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, otherBranch.StatusCode);

        var disabled = await owner.PatchAsJsonAsync($"/api/branches/{branch.Id}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.OK, disabled.StatusCode);
        var repeatedDisable = await owner.PatchAsJsonAsync($"/api/branches/{branch.Id}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.OK, repeatedDisable.StatusCode);
        var disableFinalBranch = await owner.PatchAsJsonAsync($"/api/branches/{tenant.BranchId}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.Conflict, disableFinalBranch.StatusCode);
        staff.DefaultRequestHeaders.Remove("X-MG-Branch");
        staff.DefaultRequestHeaders.Add("X-MG-Branch", branch.Id.ToString());
        var inactiveBranch = await staff.GetAsync("/api/context");
        Assert.Equal(HttpStatusCode.Forbidden, inactiveBranch.StatusCode);
    }

    [Fact]
    public async Task TenantMustRetainAtLeastOneActiveOwner()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Owner Safety",
            slug = $"owner-safe-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        var memberships = await owner.GetFromJsonAsync<MembershipResponse[]>("/api/memberships");
        var ownerMembership = Assert.Single(memberships!);

        var demote = await owner.PutAsJsonAsync($"/api/memberships/{ownerMembership.Id}", new
        {
            role = TenantRole.Manager,
            branchId = (Guid?)null
        });
        Assert.Equal(HttpStatusCode.Conflict, demote.StatusCode);

        var remove = await owner.DeleteAsync($"/api/memberships/{ownerMembership.Id}");
        Assert.Equal(HttpStatusCode.BadRequest, remove.StatusCode);
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
    private sealed record MembershipResponse(Guid Id, Guid UserId, string Email, string? DisplayName, string Role, Guid? BranchId);
}
