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

public sealed class GameNetApiIntegrationTests
{
    [Fact]
    public async Task GameSessionCloseCreatesSalesOrderAndRevenueJournal()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"game-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Game Store",
            slug = $"game-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Game Revenue", AccountType.Revenue);
        var cash = await CreateAccountAsync(client, "1000", "Cash", AccountType.Asset);

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "GAME-HOUR",
            name = "PC Gaming Session",
            baseUnit = "session",
            tracksInventory = false
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);

        var deviceResponse = await client.PostAsJsonAsync("/api/gamenet/devices", new
        {
            serviceProductId = product!.Id,
            code = "PC-01",
            name = "Gaming PC 01",
            hourlyRate = 60m,
            billingIntervalMinutes = 15
        });
        var device = await deviceResponse.Content.ReadFromJsonAsync<DeviceResponse>();
        Assert.Equal(HttpStatusCode.Created, deviceResponse.StatusCode);

        var started = await client.PostAsJsonAsync("/api/gamenet/sessions", new
        {
            deviceId = device!.Id,
            customerId = (Guid?)null
        });
        Assert.Equal(HttpStatusCode.Created, started.StatusCode);
        var session = await started.Content.ReadFromJsonAsync<StartedSession>();

        var duplicate = await client.PostAsJsonAsync("/api/gamenet/sessions", new
        {
            deviceId = device.Id,
            customerId = (Guid?)null
        });
        Assert.Equal(HttpStatusCode.Conflict, duplicate.StatusCode);

        var quote = await client.GetFromJsonAsync<SessionQuote>($"/api/gamenet/sessions/{session!.Id}/quote");
        Assert.Equal(15, quote!.BilledMinutes);
        Assert.Equal(15m, quote.TotalAmount);
        var settlementAccounts = await client.GetFromJsonAsync<SettlementAccountResponse[]>("/api/gamenet/settlement-accounts");
        Assert.Contains(settlementAccounts!, account => account.Id == cash.Id);
        Assert.Contains(settlementAccounts!, account => account.Id == revenue.Id);

        var invalidOnAccount = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session.Id}/close", new
        {
            amountPaid = 0m,
            paymentMethod = PaymentMethod.OnAccount,
            revenueAccountId = revenue.Id,
            paymentAccountId = (Guid?)null,
            receivableAccountId = cash.Id,
            paymentReference = "GAME-CREDIT"
        });
        Assert.Equal(HttpStatusCode.BadRequest, invalidOnAccount.StatusCode);

        using var negativePayment = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session.Id}/close", new
        {
            amountPaid = -1m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            paymentReference = "GAME-NEGATIVE"
        });
        using var excessivePrecisionPayment = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session.Id}/close", new
        {
            amountPaid = 1.001m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            paymentReference = "GAME-PRECISION"
        });
        Assert.Equal(HttpStatusCode.BadRequest, negativePayment.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, excessivePrecisionPayment.StatusCode);

        var disableBranch = await client.PatchAsJsonAsync($"/api/branches/{tenant.BranchId}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.Conflict, disableBranch.StatusCode);

        var closed = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session!.Id}/close", new
        {
            amountPaid = 15m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            paymentReference = "GAME-CASH"
        });
        Assert.Equal(HttpStatusCode.OK, closed.StatusCode);
        var result = await closed.Content.ReadFromJsonAsync<ClosedSession>();
        Assert.Equal(15, result!.BilledMinutes);
        Assert.Equal(15m, result.TotalAmount);
        Assert.NotEqual(Guid.Empty, result.SalesOrderId);

        using var repeatedClose = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session.Id}/close", new
        {
            amountPaid = 15m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            paymentReference = "GAME-CASH"
        });
        Assert.Equal(HttpStatusCode.OK, repeatedClose.StatusCode);
        var repeatedResult = await repeatedClose.Content.ReadFromJsonAsync<ClosedSession>();
        Assert.NotNull(repeatedResult);
        Assert.True(repeatedResult!.Replayed);
        Assert.Equal(result.SalesOrderId, repeatedResult.SalesOrderId);
        Assert.Equal(result.PaidAmount, repeatedResult.PaidAmount);
        Assert.Equal(result.BalanceDue, repeatedResult.BalanceDue);

        using var changedClose = await client.PostAsJsonAsync($"/api/gamenet/sessions/{session.Id}/close", new
        {
            amountPaid = 14m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            paymentReference = "GAME-CASH"
        });
        Assert.Equal(HttpStatusCode.Conflict, changedClose.StatusCode);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Single(orders!);
        Assert.Equal(result.SalesOrderId, orders[0].Id);

        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(15m, trial!.TotalDebit);
        Assert.Equal(15m, trial.TotalCredit);
        Assert.True(trial.IsBalanced);
    }

    private static async Task<AccountResponse> CreateAccountAsync(HttpClient client, string code, string name, AccountType type)
    {
        var response = await client.PostAsJsonAsync("/api/accounting/accounts", new { code, name, type });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        return (await response.Content.ReadFromJsonAsync<AccountResponse>())!;
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
    private sealed record AccountResponse(Guid Id, string Code, string Name, string Type, bool IsActive);
    private sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, bool IsActive);
    private sealed record DeviceResponse(Guid Id, string Code, string Name, decimal HourlyRate, int BillingIntervalMinutes, Guid BranchId, bool HasActiveSession);
    private sealed record StartedSession(Guid Id, Guid DeviceId, Guid? CustomerId, DateTimeOffset StartedAtUtc, decimal HourlyRate, int BillingIntervalMinutes);
    private sealed record ClosedSession(Guid Id, Guid DeviceId, DateTimeOffset StartedAtUtc, DateTimeOffset EndedAtUtc, int BilledMinutes, decimal HourlyRate, decimal TotalAmount, Guid SalesOrderId, decimal PaidAmount, decimal BalanceDue, bool Replayed = false);
    private sealed record SessionQuote(Guid SessionId, Guid? CustomerId, DateTimeOffset QuotedAtUtc, int BilledMinutes, decimal TotalAmount);
    private sealed record SettlementAccountResponse(Guid Id, string Code, string Name, string Type);
    private sealed record SalesOrderResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, string Status, DateTimeOffset CreatedAtUtc);
    private sealed record TrialBalanceResponse(TrialAccount[] Accounts, decimal TotalDebit, decimal TotalCredit, bool IsBalanced);
    private sealed record TrialAccount(Guid Id, string Code, string Name, string Type, decimal Debit, decimal Credit, decimal NetDebit, decimal NetCredit);
}
