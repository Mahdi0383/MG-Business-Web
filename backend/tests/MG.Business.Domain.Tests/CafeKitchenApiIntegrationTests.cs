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

public sealed class CafeKitchenApiIntegrationTests
{
    [Fact]
    public async Task CafeSaleCanEnterKitchenAndProgressThroughPreparationStates()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"cafe-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Cafe Store",
            slug = $"cafe-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var cash = await CreateAccountAsync(client, "1000", "Cash", AccountType.Asset);
        var revenue = await CreateAccountAsync(client, "4000", "Cafe Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "CAFE-COFFEE",
            name = "Cappuccino",
            baseUnit = "cup",
            tracksInventory = false
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        var saleResponse = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = (Guid?)null,
            discount = 0m,
            amountPaid = 100m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = (Guid?)null,
            inventoryAccountId = (Guid?)null,
            costOfGoodsSoldAccountId = (Guid?)null,
            paymentReference = "CAFE-001",
            lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 100m, discount = 0m } }
        });
        Assert.Equal(HttpStatusCode.Created, saleResponse.StatusCode);
        var sale = await saleResponse.Content.ReadFromJsonAsync<SaleResponse>();

        var ticketResponse = await client.PostAsJsonAsync("/api/cafe/kitchen/tickets", new
        {
            salesOrderId = sale!.Id,
            serviceType = CafeServiceType.DineIn,
            tableLabel = "T-04",
            notes = "بدون شکر"
        });
        Assert.Equal(HttpStatusCode.Created, ticketResponse.StatusCode);
        var ticket = await ticketResponse.Content.ReadFromJsonAsync<TicketResponse>();

        var disableBranch = await client.PatchAsJsonAsync($"/api/branches/{tenant.BranchId}/active", new { isActive = false });
        Assert.Equal(HttpStatusCode.Conflict, disableBranch.StatusCode);

        var invalid = await client.PutAsJsonAsync($"/api/cafe/kitchen/tickets/{ticket!.Id}/status",
            new { status = KitchenTicketStatus.Ready });
        Assert.Equal(HttpStatusCode.Conflict, invalid.StatusCode);

        foreach (var state in new[] { KitchenTicketStatus.Preparing, KitchenTicketStatus.Ready, KitchenTicketStatus.Completed })
        {
            var update = await client.PutAsJsonAsync($"/api/cafe/kitchen/tickets/{ticket.Id}/status", new { status = state });
            Assert.Equal(HttpStatusCode.OK, update.StatusCode);
        }

        var tickets = await client.GetFromJsonAsync<TicketResponse[]>("/api/cafe/kitchen/tickets?status=Completed");
        Assert.Single(tickets!);
        Assert.Equal("Completed", tickets[0].Status);
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
    private sealed record SaleResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, string Status, DateTimeOffset CreatedAtUtc);
    private sealed record TicketResponse(Guid Id, Guid SalesOrderId, string ServiceType, string? TableLabel, string? Notes, string Status, DateTimeOffset CreatedAtUtc, DateTimeOffset? PreparingAtUtc, DateTimeOffset? ReadyAtUtc);
}
