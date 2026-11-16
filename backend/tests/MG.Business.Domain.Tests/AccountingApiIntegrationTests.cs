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


    [Fact]
    public async Task BranchScopedAccountantSeesOnlyThatBranchesTrialBalance()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Branch Accounting",
            slug = $"branch-accounting-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var branchResponse = await owner.PostAsJsonAsync("/api/branches", new { name = "Second", code = "B02" });
        Assert.Equal(HttpStatusCode.Created, branchResponse.StatusCode);
        var branch = await branchResponse.Content.ReadFromJsonAsync<BranchCreated>();

        var cash = await CreateAccountAsync(owner, "1000", "Cash", AccountType.Asset);
        var sales = await CreateAccountAsync(owner, "4000", "Sales", AccountType.Revenue);

        var mainEntry = await owner.PostAsJsonAsync("/api/accounting/journal-entries", new
        {
            entryDate = DateOnly.FromDateTime(DateTime.UtcNow),
            description = "Main branch receipt",
            reference = "MAIN-1",
            lines = new[]
            {
                new { accountId = cash.Id, debit = 100m, credit = 0m, description = "Main cash" },
                new { accountId = sales.Id, debit = 0m, credit = 100m, description = "Main sales" }
            }
        });
        Assert.Equal(HttpStatusCode.Created, mainEntry.StatusCode);

        owner.DefaultRequestHeaders.Remove("X-MG-Branch");
        owner.DefaultRequestHeaders.Add("X-MG-Branch", branch!.Id.ToString());
        var secondEntry = await owner.PostAsJsonAsync("/api/accounting/journal-entries", new
        {
            entryDate = DateOnly.FromDateTime(DateTime.UtcNow),
            description = "Second branch receipt",
            reference = "SECOND-1",
            lines = new[]
            {
                new { accountId = cash.Id, debit = 200m, credit = 0m, description = "Second cash" },
                new { accountId = sales.Id, debit = 0m, credit = 200m, description = "Second sales" }
            }
        });
        Assert.Equal(HttpStatusCode.Created, secondEntry.StatusCode);

        var staffEmail = $"accountant-{Guid.NewGuid():N}@mg.test";
        using var accountant = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = TenantRole.Accountant,
            branchId = branch.Id
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        accountant.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        accountant.DefaultRequestHeaders.Add("X-MG-Branch", branch.Id.ToString());
        var trial = await accountant.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");

        Assert.NotNull(trial);
        Assert.Equal(200m, trial!.TotalDebit);
        Assert.Equal(200m, trial.TotalCredit);
        Assert.True(trial.IsBalanced);
    }


    [Fact]
    public async Task DefaultChartInitializationIsIdempotent()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"chart-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Chart Store",
            slug = $"chart-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        var first = await client.PostAsync("/api/accounting/accounts/initialize-default-chart", null);
        Assert.Equal(HttpStatusCode.OK, first.StatusCode);
        var firstAccounts = await first.Content.ReadFromJsonAsync<AccountCreated[]>();
        Assert.Equal(9, firstAccounts!.Length);

        var second = await client.PostAsync("/api/accounting/accounts/initialize-default-chart", null);
        Assert.Equal(HttpStatusCode.OK, second.StatusCode);
        var secondAccounts = await second.Content.ReadFromJsonAsync<AccountCreated[]>();

        Assert.Equal(9, secondAccounts!.Length);
        Assert.Equal(firstAccounts.Select(x => x.Id).OrderBy(x => x),
            secondAccounts.Select(x => x.Id).OrderBy(x => x));
    }


    [Fact]
    public async Task CashierCanReadSettlementAccountsButCannotReadFinancialReports()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"account-owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Cashier Access",
            slug = $"cashier-access-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        var chart = await owner.PostAsync("/api/accounting/accounts/initialize-default-chart", null);
        Assert.Equal(HttpStatusCode.OK, chart.StatusCode);

        var cashierEmail = $"cashier-{Guid.NewGuid():N}@mg.test";
        using var cashier = await RegisterAndLoginAsync(factory, cashierEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = cashierEmail,
            role = TenantRole.Cashier,
            branchId = (Guid?)null
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        cashier.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        var accounts = await cashier.GetAsync("/api/accounting/accounts");
        var trialBalance = await cashier.GetAsync("/api/accounting/trial-balance");

        Assert.Equal(HttpStatusCode.OK, accounts.StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, trialBalance.StatusCode);
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
    private sealed record BranchCreated(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
    private sealed record AccountCreated(Guid Id, string Code, string Name, string Type, bool IsActive);
    private sealed record TrialBalanceResponse(TrialAccount[] Accounts, decimal TotalDebit, decimal TotalCredit, bool IsBalanced);
    private sealed record TrialAccount(Guid Id, string Code, string Name, string Type, decimal Debit, decimal Credit, decimal NetDebit, decimal NetCredit);
}
