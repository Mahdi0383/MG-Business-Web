using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class InventoryValuationTests
{
    [Fact]
    public void CalculatesWeightedAverageAndReducesIssueAtCurrentAverageCost()
    {
        var tenantId = Guid.NewGuid();
        var branchId = Guid.NewGuid();
        var productId = Guid.NewGuid();
        var movements = new[]
        {
            new StockMovement(tenantId, branchId, productId, StockMovementType.Receipt, 10m, 5m),
            new StockMovement(tenantId, branchId, productId, StockMovementType.Receipt, 10m, 10m),
            new StockMovement(tenantId, branchId, productId, StockMovementType.Sale, 5m, 7.5m)
        };

        var valuation = InventoryValuation.Calculate(movements);

        Assert.Equal(15m, valuation.QuantityOnHand);
        Assert.Equal(112.50m, valuation.InventoryValue);
        Assert.Equal(7.5m, valuation.AverageUnitCost);
    }

    [Fact]
    public void RejectsHistoricalOutboundMovementThatWouldMakeStockNegative()
    {
        var movement = new StockMovement(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(),
            StockMovementType.Issue, 1m, 0m);

        Assert.Throws<InvalidOperationException>(() => InventoryValuation.Calculate([movement]));
    }

    [Fact]
    public void IncrementalValuationMatchesBatchValuationAndRejectsNegativeHistory()
    {
        var tenantId = Guid.NewGuid();
        var branchId = Guid.NewGuid();
        var productId = Guid.NewGuid();
        var movements = new[]
        {
            new StockMovement(tenantId, branchId, productId, StockMovementType.Receipt, 10m, 5m),
            new StockMovement(tenantId, branchId, productId, StockMovementType.Receipt, 10m, 10m),
            new StockMovement(tenantId, branchId, productId, StockMovementType.Sale, 5m, 7.5m)
        }.OrderBy(x => x.CreatedAtUtc).ThenBy(x => x.Id).ToArray();

        var accumulator = new InventoryValuationAccumulator();
        foreach (var movement in movements)
            accumulator.Apply(movement);

        Assert.Equal(InventoryValuation.Calculate(movements), accumulator.ToValuation());

        var invalid = new InventoryValuationAccumulator();
        Assert.Throws<InvalidOperationException>(() => invalid.Apply(
            new StockMovement(tenantId, branchId, productId, StockMovementType.Issue, 1m, 0m)));
    }

    [Fact]
    public void FullyIssuedStockHasZeroValue()
    {
        var tenantId = Guid.NewGuid();
        var branchId = Guid.NewGuid();
        var productId = Guid.NewGuid();
        var movements = new[]
        {
            new StockMovement(tenantId, branchId, productId, StockMovementType.Receipt, 4m, 12.5m),
            new StockMovement(tenantId, branchId, productId, StockMovementType.Issue, 4m, 12.5m)
        };

        var valuation = InventoryValuation.Calculate(movements);

        Assert.Equal(0m, valuation.QuantityOnHand);
        Assert.Equal(0m, valuation.InventoryValue);
        Assert.Equal(0m, valuation.AverageUnitCost);
    }
}
