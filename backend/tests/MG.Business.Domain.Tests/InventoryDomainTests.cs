using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class InventoryDomainTests
{
    [Fact]
    public void Product_NormalizesSkuAndUnit()
    {
        var product = new Product(Guid.NewGuid(), " mg-001 ", " Cola ", " PCS ");

        Assert.Equal("MG-001", product.Sku);
        Assert.Equal("pcs", product.BaseUnit);
        Assert.True(product.TracksInventory);
        Assert.True(product.IsActive);
    }

    [Fact]
    public void StockMovement_RequiresPositiveQuantityAndValidPrecision()
    {
        var tenantId = Guid.NewGuid();
        var movement = new StockMovement(tenantId, Guid.NewGuid(), Guid.NewGuid(),
            StockMovementType.Receipt, 2.125m, 14.50m, " PO-100 ");

        Assert.Equal(2.125m, movement.Quantity);
        Assert.Equal(14.50m, movement.UnitCost);
        Assert.Equal("PO-100", movement.Reference);
        Assert.Throws<ArgumentOutOfRangeException>(() => new StockMovement(
            tenantId, Guid.NewGuid(), Guid.NewGuid(), StockMovementType.Receipt, 0m, 1m));
        Assert.Throws<ArgumentOutOfRangeException>(() => new StockMovement(
            tenantId, Guid.NewGuid(), Guid.NewGuid(), StockMovementType.Receipt, 1.0001m, 1m));
    }

    [Fact]
    public void StockMovement_RejectsNegativeUnitCost()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new StockMovement(
            Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), StockMovementType.Issue, 1m, -0.01m));
    }
}
