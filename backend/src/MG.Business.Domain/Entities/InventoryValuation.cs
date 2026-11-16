using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed record InventoryValuation(decimal QuantityOnHand, decimal InventoryValue, decimal AverageUnitCost)
{
    public static InventoryValuation Calculate(IEnumerable<StockMovement> movements)
    {
        ArgumentNullException.ThrowIfNull(movements);

        decimal quantity = 0m;
        decimal value = 0m;

        foreach (var movement in movements.OrderBy(x => x.CreatedAtUtc).ThenBy(x => x.Id))
        {
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

        if (quantity == 0m)
            value = 0m;

        return new InventoryValuation(quantity, decimal.Round(value, 2), quantity == 0m ? 0m : decimal.Round(value / quantity, 4));
    }
}
