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

public sealed class AccountingApiIntegrationTests
{
    [Fact]
    public async Task BalancedJournalEntryIsSavedAndAppearsInTrialBalance()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"accountant-{Guid.NewGuid():N}@mg.test");
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Accounting Store",
            slug = $"accounting-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var cash = await CreateAccountAsync(client, "1000", "Cash", AccountType.Asset);
        var sales = await CreateAccountAsync(client, "4000", "Sales", AccountType.Revenue);

        var entryResponse = await client.PostAsJsonAsync("/api/accounting/journal-entries", new
        {
            entryDate = DateOnly.FromDateTime(DateTime.UtcNow),
            description = "Initial sales receipt",
            reference = "POS-1001",
            lines = new[]
            {
                new { accountId = cash.Id, debit = 250m, credit = 0m, description = "Cash received" },
                new { accountId = sales.Id, debit = 0m, credit = 250m, description = "Sales income" }
            }
        });
        Assert.Equal(HttpStatusCode.Created, entryResponse.StatusCode);

        var trialBalance = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.NotNull(trialBalance);
        Assert.Equal(250m, trialBalance!.TotalDebit);
        Assert.Equal(250m, trialBalance.TotalCredit);
        Assert.True(trialBalance.IsBalanced);
        Assert.Equal(2, trialBalance.Accounts.Length);
    }

    [Fact]
    public async Task UnbalancedJournalEntryIsRejected()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"accountant-{Guid.NewGuid():N}@mg.test");
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Balanced Store",
            slug = $"balanced-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var cash = await CreateAccountAsync(client, "1000", "Cash", AccountType.Asset);
        var sales = await CreateAccountAsync(client, "4000", "Sales", AccountType.Revenue);

        var response = await client.PostAsJsonAsync("/api/accounting/journal-entries", new
        {
            entryDate = DateOnly.FromDateTime(DateTime.UtcNow),
            description = "Unbalanced journal",
            reference = "BAD-1",
            lines = new[]
            {
                new { accountId = cash.Id, debit = 250m, credit = 0m, description = (string?)null },
                new { accountId = sales.Id, debit = 0m, credit = 200m, description = (string?)null }
            }
        });

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    private static async Task<AccountCreated> CreateAccountAsync(HttpClient client, string code, string name, AccountType type)
    {
        var response = await client.PostAsJsonAsync("/api/accounting/accounts", new { code, name, type });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        return (await response.Content.ReadFromJsonAsync<AccountCreated>())!;
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
    private sealed record AccountCreated(Guid Id, string Code, string Name, string Type, bool IsActive);
    private sealed record TrialBalanceResponse(TrialAccount[] Accounts, decimal TotalDebit, decimal TotalCredit, bool IsBalanced);
    private sealed record TrialAccount(Guid Id, string Code, string Name, string Type, decimal Debit, decimal Credit, decimal NetDebit, decimal NetCredit);
}
