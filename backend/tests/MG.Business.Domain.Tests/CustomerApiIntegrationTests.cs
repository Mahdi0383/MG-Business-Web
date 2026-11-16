using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class CustomerApiIntegrationTests
{
    [Fact]
    public async Task CustomerCrudIsTenantScopedAndRoleProtected()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"customer-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Store",
            slug = $"customer-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        var create = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-001",
            name = "مشتری اول",
            phone = "09120000000",
            email = "customer@example.com",
            address = "مشهد",
            notes = "تست"
        });
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var customer = await create.Content.ReadFromJsonAsync<CustomerResponse>();
        Assert.NotNull(customer);

        var list = await client.GetFromJsonAsync<CustomerResponse[]>("/api/customers?search=0912");
        Assert.Single(list!);

        client.DefaultRequestHeaders.Remove("X-MG-Tenant");
        client.DefaultRequestHeaders.Add("X-MG-Tenant", Guid.NewGuid().ToString());
        var crossTenant = await client.GetAsync($"/api/customers/{customer!.Id}");
        Assert.Equal(HttpStatusCode.Forbidden, crossTenant.StatusCode);
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
    private sealed record CustomerResponse(Guid Id, string Code, string Name, string? Phone, string? Email, string? Address, string? Notes, bool IsActive);
}
