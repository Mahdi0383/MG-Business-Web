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

public sealed class SalesApiIntegrationTests
{
    [Fact]
    public async Task SaleAtomicallyCreatesOrderPaymentInventoryAndBalancedAccountingEntries()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sales Store",
            slug = $"sales-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var chartResponse = await client.PostAsync("/api/accounting/accounts/initialize-default-chart", null);
        Assert.Equal(HttpStatusCode.OK, chartResponse.StatusCode);
        var chart = await chartResponse.Content.ReadFromJsonAsync<AccountResponse[]>();
        var cash = chart!.Single(x => x.Code == "1000");
        var receivable = chart.Single(x => x.Code == "1100");
        var inventory = chart.Single(x => x.Code == "1200");
        var revenue = chart.Single(x => x.Code == "4000");
        var cogs = chart.Single(x => x.Code == "5000");

        var customerResponse = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-100",
            name = "Test Customer",
            phone = "09120000000",
            email = (string?)null,
            address = (string?)null,
            notes = (string?)null
        });
        var customer = await customerResponse.Content.ReadFromJsonAsync<CustomerResponse>();
        Assert.Equal(HttpStatusCode.Created, customerResponse.StatusCode);

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "COFFEE-01",
            name = "Coffee Beans",
            baseUnit = "kg",
            tracksInventory = true,
            defaultSalePrice = 20m
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);

        var receipt = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 10m,
            unitCost = 5m,
            reference = "PUR-1"
        });
        Assert.Equal(HttpStatusCode.Created, receipt.StatusCode);

        var saleResponse = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = customer!.Id,
            discount = 0m,
            amountPaid = 20m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = receivable.Id,
            inventoryAccountId = inventory.Id,
            costOfGoodsSoldAccountId = cogs.Id,
            paymentReference = "CASH-001",
            lines = new[]
            {
                new { productId = product.Id, quantity = 2m, discount = 0m }
            }
        });

        Assert.Equal(HttpStatusCode.Created, saleResponse.StatusCode);
        var sale = await saleResponse.Content.ReadFromJsonAsync<SaleResponse>();
        Assert.NotNull(sale);
        Assert.Equal(40m, sale!.Total);
        Assert.Equal(20m, sale.PaidAmount);
        Assert.Equal(20m, sale.BalanceDue);
        Assert.Equal(10m, sale.CostOfGoodsSold);
        Assert.Equal(30m, sale.GrossProfit);

        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Single(stock!);
        Assert.Equal(8m, stock[0].QuantityOnHand);
        Assert.Equal(40m, stock[0].InventoryValue);

        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.NotNull(trial);
        Assert.Equal(50m, trial!.TotalDebit);
        Assert.Equal(50m, trial.TotalCredit);
        Assert.True(trial.IsBalanced);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Single(orders!);
        Assert.Equal(sale.Id, orders[0].Id);
    }

    [Fact]
    public async Task SaleRejectsInsufficientStockWithoutCreatingFinancialEntries()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-stock-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "No Stock Store",
            slug = $"no-stock-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "EMPTY-01",
            name = "Out of Stock",
            baseUnit = "unit",
            tracksInventory = true
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        var response = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = (Guid?)null,
            discount = 0m,
            amountPaid = 10m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = Guid.NewGuid(),
            receivableAccountId = (Guid?)null,
            inventoryAccountId = (Guid?)null,
            costOfGoodsSoldAccountId = (Guid?)null,
            paymentReference = (string?)null,
            lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 10m, discount = 0m } }
        });

        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(0m, trial!.TotalDebit);
        Assert.Equal(0m, trial.TotalCredit);
    }


    [Fact]
    public async Task SaleRejectsOverpaymentAndUnassignedPartialPaymentAsValidationErrors()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-settlement-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Settlement Validation Store",
            slug = $"settlement-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "SERVICE-SETTLEMENT",
            name = "Service",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 10m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        async Task<HttpResponseMessage> CreateInvalidSale(decimal amountPaid, string reference)
        {
            return await client.PostAsJsonAsync("/api/sales/orders", new
            {
                customerId = (Guid?)null,
                discount = 0m,
                amountPaid,
                paymentMethod = PaymentMethod.Cash,
                revenueAccountId = revenue.Id,
                paymentAccountId = Guid.NewGuid(),
                receivableAccountId = (Guid?)null,
                inventoryAccountId = (Guid?)null,
                costOfGoodsSoldAccountId = (Guid?)null,
                paymentReference = reference,
                lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 10m, discount = 0m } }
            });
        }

        using var overpayment = await CreateInvalidSale(11m, "OVERPAY");
        using var unassignedPartial = await CreateInvalidSale(5m, "PARTIAL");
        Assert.Equal(HttpStatusCode.BadRequest, overpayment.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, unassignedPartial.StatusCode);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Empty(orders!);
        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(0m, trial!.TotalDebit);
        Assert.Equal(0m, trial.TotalCredit);
    }

    [Fact]
    public async Task BranchScopedCashierListsOnlySalesFromAssignedBranch()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"sales-owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Branch Sales",
            slug = $"branch-sales-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var branchResponse = await owner.PostAsJsonAsync("/api/branches", new { name = "Second", code = "B02" });
        Assert.Equal(HttpStatusCode.Created, branchResponse.StatusCode);
        var branch = await branchResponse.Content.ReadFromJsonAsync<BranchCreated>();

        var cash = await CreateAccountAsync(owner, "1000", "Cash", AccountType.Asset);
        var revenue = await CreateAccountAsync(owner, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await owner.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "SERVICE-01",
            name = "Service Item",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 25m
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        async Task<SaleResponse> CreateSaleAsync(string reference)
        {
            var response = await owner.PostAsJsonAsync("/api/sales/orders", new
            {
                customerId = (Guid?)null,
                discount = 0m,
                amountPaid = 25m,
                paymentMethod = PaymentMethod.Cash,
                revenueAccountId = revenue.Id,
                paymentAccountId = cash.Id,
                receivableAccountId = (Guid?)null,
                inventoryAccountId = (Guid?)null,
                costOfGoodsSoldAccountId = (Guid?)null,
                paymentReference = reference,
                lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 25m, discount = 0m } }
            });
            Assert.Equal(HttpStatusCode.Created, response.StatusCode);
            return (await response.Content.ReadFromJsonAsync<SaleResponse>())!;
        }

        var mainSale = await CreateSaleAsync("MAIN-SALE");
        owner.DefaultRequestHeaders.Remove("X-MG-Branch");
        owner.DefaultRequestHeaders.Add("X-MG-Branch", branch!.Id.ToString());
        var secondSale = await CreateSaleAsync("SECOND-SALE");

        var staffEmail = $"cashier-{Guid.NewGuid():N}@mg.test";
        using var cashier = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = TenantRole.Cashier,
            branchId = branch.Id
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        cashier.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        cashier.DefaultRequestHeaders.Add("X-MG-Branch", branch.Id.ToString());
        var orders = await cashier.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");

        Assert.Single(orders!);
        Assert.Equal(secondSale.Id, orders[0].Id);
        Assert.NotEqual(mainSale.Id, orders[0].Id);
    }

    [Fact]
    public async Task InventoryRejectsInvalidMovementPrecisionAndOutboundCostBeforePersistence()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"inventory-validation-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Inventory Validation Store",
            slug = $"inventory-validation-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "INV-VALIDATION-01",
            name = "Validation Item",
            baseUnit = "unit",
            tracksInventory = true,
            defaultSalePrice = 5m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        using var zeroQuantity = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 0m,
            unitCost = 2m,
            reference = "INVALID-ZERO"
        });
        using var excessivePrecision = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Receipt,
            quantity = 1.0001m,
            unitCost = 2m,
            reference = "INVALID-PRECISION"
        });
        using var outboundWithCost = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Issue,
            quantity = 1m,
            unitCost = 2m,
            reference = "INVALID-OUTBOUND-COST"
        });

        Assert.Equal(HttpStatusCode.BadRequest, zeroQuantity.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, excessivePrecision.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, outboundWithCost.StatusCode);
        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Empty(stock!);
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
    private sealed record BranchCreated(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
    private sealed record AccountResponse(Guid Id, string Code, string Name, string Type, bool IsActive);
    private sealed record CustomerResponse(Guid Id, string Code, string Name, string? Phone, string? Email, string? Address, string? Notes, bool IsActive);
    private sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, decimal DefaultSalePrice, bool IsActive);
    private sealed record SaleResponse(Guid Id, string Number, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, decimal CostOfGoodsSold, decimal GrossProfit, string Status, DateTimeOffset CreatedAtUtc);
    private sealed record StockResponse(Guid ProductId, Guid BranchId, decimal QuantityOnHand, decimal AverageUnitCost, decimal InventoryValue, int MovementCount);
    private sealed record TrialBalanceResponse(TrialAccount[] Accounts, decimal TotalDebit, decimal TotalCredit, bool IsBalanced);
    private sealed record TrialAccount(Guid Id, string Code, string Name, string Type, decimal Debit, decimal Credit, decimal NetDebit, decimal NetCredit);
    private sealed record SalesOrderResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, string Status, DateTimeOffset CreatedAtUtc);
}
