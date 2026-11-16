using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Domain.Tests;

public sealed class InventoryPersistenceTests
{
    [Fact]
    public async Task Sqlite_PersistsTenantScopedStockMovements()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenant = new Tenant("MG Store", "mg-store");
        var branch = new Branch(tenant.Id, "Main", "MAIN");
        var product = new Product(tenant.Id, "SKU-001", "Coffee", "kg");
        var movement = new StockMovement(tenant.Id, branch.Id, product.Id,
            StockMovementType.Receipt, 12.500m, 85.25m, "GRN-001");

        db.AddRange(tenant, branch, product, movement);
        await db.SaveChangesAsync();

        var persisted = await db.StockMovements.SingleAsync();
        Assert.Equal(product.Id, persisted.ProductId);
        Assert.Equal(12.500m, persisted.Quantity);
        Assert.Equal(85.25m, persisted.UnitCost);
    }

    [Fact]
    public async Task Sqlite_RejectsStockMovementForAnotherTenantsProduct()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenantA = new Tenant("Tenant A", "tenant-a");
        var tenantB = new Tenant("Tenant B", "tenant-b");
        var branchA = new Branch(tenantA.Id, "Main", "MAIN");
        var productB = new Product(tenantB.Id, "SKU-001", "Other tenant product", "pcs");
        var invalidMovement = new StockMovement(tenantA.Id, branchA.Id, productB.Id,
            StockMovementType.Receipt, 1m, 10m);

        db.AddRange(tenantA, tenantB, branchA, productB, invalidMovement);
        await Assert.ThrowsAsync<DbUpdateException>(() => db.SaveChangesAsync());
    }
}
