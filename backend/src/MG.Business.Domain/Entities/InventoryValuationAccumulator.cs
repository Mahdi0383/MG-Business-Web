using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

/// <summary>
/// Incremental weighted-average inventory valuation for movements supplied in
/// CreatedAtUtc/Id order. Keeps memory proportional to product count, not history size.
/// </summary>
public sealed class InventoryValuationAccumulator
{
    private decimal quantity;
    private decimal value;

    public void Apply(StockMovement movement)
    {
        ArgumentNullException.ThrowIfNull(movement);
        switch (movement.Type)
        {
            case StockMovementType.Receipt:
            case StockMovementType.SalesReturn:
            case StockMovementType.AdjustmentIncrease:
                quantity += movement.Quantity;
                value += movement.Quantity * movement.UnitCost;
                break;
            case StockMovementType.Issue:
            case StockMovementType.Sale:
            case StockMovementType.PurchaseReturn:
            case StockMovementType.AdjustmentDecrease:
                if (movement.Quantity > quantity)
                    throw new InvalidOperationException("Stock history contains an outbound movement greater than the available quantity.");
                var averageCost = quantity == 0m ? 0m : value / quantity;
                quantity -= movement.Quantity;
                value -= movement.Quantity * averageCost;
                break;
            default:
                throw new InvalidOperationException("Unsupported stock movement type.");
        }
    }

    public InventoryValuation ToValuation()
    {
        var inventoryValue = quantity == 0m ? 0m : decimal.Round(value, 2);
        return new InventoryValuation(quantity, inventoryValue,
            quantity == 0m ? 0m : decimal.Round(inventoryValue / quantity, 4));
    }
}
