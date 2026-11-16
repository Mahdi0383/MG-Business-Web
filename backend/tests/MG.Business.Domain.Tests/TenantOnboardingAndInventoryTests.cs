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

public sealed class TenantOnboardingAndInventoryTests
{
    [Fact]
    public async Task OwnerCanCreateTenantAndInventoryIsTenantScoped()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"owner-{Guid.NewGuid():N}@mg.test");

        var created = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "MG Demo Store",
            slug = $"mg-demo-{Guid.NewGuid():N}",
            firstBranchName = "شعبه مرکزی"
        });
        Assert.Equal(HttpStatusCode.Created, created.StatusCode);
        var tenant = await created.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);

        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "ESP-001",
            name = "اسپرسو",
            baseUnit = "cup",
            tracksInventory = true
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductCreated>();
        Assert.NotNull(product);

        var products = await client.GetFromJsonAsync<ProductCreated[]>("/api/inventory/products");
        Assert.Single(products!);

        var secondProduct = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "TEA-002",
            name = "چای",
            baseUnit = "cup",
            tracksInventory = true
        });
        Assert.Equal(HttpStatusCode.Created, secondProduct.StatusCode);

        var catalog = await client.GetFromJsonAsync<ProductCatalogPage>(
            "/api/inventory/products/catalog?page=1&pageSize=1&search=چای&activeOnly=true");
        Assert.NotNull(catalog);
        Assert.Equal(1, catalog.TotalCount);
        Assert.Equal(1, catalog.TotalPages);
        Assert.Single(catalog.Items);
        Assert.Equal("TEA-002", catalog.Items[0].Sku);

        var skuSearch = await client.GetFromJsonAsync<ProductCatalogPage>(
            "/api/inventory/products/catalog?page=1&pageSize=10&search=ESP-001&activeOnly=false");
        Assert.NotNull(skuSearch);
        Assert.Equal(1, skuSearch.TotalCount);
        Assert.Equal("ESP-001", Assert.Single(skuSearch.Items).Sku);

        var secondPage = await client.GetFromJsonAsync<ProductCatalogPage>(
            "/api/inventory/products/catalog?page=2&pageSize=1&activeOnly=false");
        Assert.NotNull(secondPage);
        Assert.Equal(2, secondPage.TotalCount);
        Assert.Equal(2, secondPage.TotalPages);
        Assert.Single(secondPage.Items);

        var invalidCatalog = await client.GetAsync("/api/inventory/products/catalog?page=0&pageSize=201");
        Assert.Equal(HttpStatusCode.BadRequest, invalidCatalog.StatusCode);

        client.DefaultRequestHeaders.Remove("X-MG-Tenant");
        client.DefaultRequestHeaders.Add("X-MG-Tenant", Guid.NewGuid().ToString());
        var crossTenant = await client.GetAsync("/api/inventory/products");
        Assert.Equal(HttpStatusCode.Forbidden, crossTenant.StatusCode);
    }

    [Fact]
    public async Task StockMovementRequiresAValidBranchAndTrackedProduct()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"stock-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Stock Store",
            slug = $"stock-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);

        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "NONSTOCK-01",
            name = "خدمت",
            baseUnit = "service",
            tracksInventory = false
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductCreated>();

        var missingBranch = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 5m,
            unitCost = 100m,
            reference = "opening"
        });
        Assert.Equal(HttpStatusCode.BadRequest, missingBranch.StatusCode);

        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant!.BranchId.ToString());
        var untracked = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Receipt,
            quantity = 5m,
            unitCost = 100m,
            reference = "opening"
        });
        Assert.Equal(HttpStatusCode.NotFound, untracked.StatusCode);
    }

    [Fact]
    public async Task StockMovementsUseMovingAverageAndPreventNegativeInventory()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"stock-value-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Valuation Store",
            slug = $"valuation-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "VAL-001",
            name = "Beans",
            baseUnit = "kg",
            tracksInventory = true
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductCreated>();
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);

        foreach (var receipt in new[] { (quantity: 10m, cost: 5m), (quantity: 10m, cost: 10m) })
        {
            var receiptResponse = await client.PostAsJsonAsync("/api/inventory/movements", new
            {
                productId = product!.Id,
                type = StockMovementType.Receipt,
                quantity = receipt.quantity,
                unitCost = receipt.cost,
                reference = "purchase"
            });
            Assert.Equal(HttpStatusCode.Created, receiptResponse.StatusCode);
        }

        var sale = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Sale,
            quantity = 5m,
            unitCost = (decimal?)null,
            reference = "sale"
        });
        Assert.Equal(HttpStatusCode.Created, sale.StatusCode);

        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Single(stock!);
        Assert.Equal(15m, stock[0].QuantityOnHand);
        Assert.Equal(7.5m, stock[0].AverageUnitCost);
        Assert.Equal(112.5m, stock[0].InventoryValue);

        var oversell = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Sale,
            quantity = 16m,
            unitCost = (decimal?)null,
            reference = "oversell"
        });
        Assert.Equal(HttpStatusCode.Conflict, oversell.StatusCode);

        var firstPage = await client.GetFromJsonAsync<MovementPage>(
            "/api/inventory/movements?page=1&pageSize=2");
        Assert.NotNull(firstPage);
        Assert.Equal(1, firstPage.Page);
        Assert.Equal(2, firstPage.PageSize);
        Assert.Equal(3, firstPage.TotalCount);
        Assert.Equal(2, firstPage.TotalPages);
        Assert.Equal(2, firstPage.Items.Length);

        var secondPage = await client.GetFromJsonAsync<MovementPage>(
            "/api/inventory/movements?page=2&pageSize=2");
        Assert.NotNull(secondPage);
        Assert.Single(secondPage.Items);

        var invalidPage = await client.GetAsync("/api/inventory/movements?page=2147483647&pageSize=200");
        Assert.Equal(HttpStatusCode.BadRequest, invalidPage.StatusCode);
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
    private sealed record ProductCreated(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, bool IsActive);
    private sealed record ProductCatalogPage(int Page, int PageSize, int TotalCount, int TotalPages, ProductCatalogItem[] Items);
    private sealed record ProductCatalogItem(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, decimal DefaultSalePrice, bool IsActive);
    private sealed record StockResponse(Guid ProductId, Guid BranchId, decimal QuantityOnHand, decimal AverageUnitCost, decimal InventoryValue, int MovementCount);
    private sealed record MovementPage(int Page, int PageSize, int TotalCount, int TotalPages, MovementItem[] Items);
    private sealed record MovementItem(Guid Id, Guid ProductId, Guid BranchId, string Type, decimal Quantity, decimal UnitCost, decimal SignedQuantity, string? Reference, DateTimeOffset CreatedAtUtc);
}
