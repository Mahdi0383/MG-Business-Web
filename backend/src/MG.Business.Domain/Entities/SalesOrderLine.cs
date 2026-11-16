namespace MG.Business.Domain.Entities;

public sealed class SalesOrderLine
{
    private SalesOrderLine() { }

    public SalesOrderLine(Guid tenantId, Guid productId, string productName, string sku,
        decimal quantity, decimal unitPrice, decimal discount, decimal unitCost)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (productId == Guid.Empty) throw new ArgumentException("Product is required.", nameof(productId));
        if (string.IsNullOrWhiteSpace(productName)) throw new ArgumentException("Product name is required.", nameof(productName));
        if (string.IsNullOrWhiteSpace(sku)) throw new ArgumentException("Product SKU is required.", nameof(sku));
        if (quantity <= 0m || decimal.Round(quantity, 3) != quantity)
            throw new ArgumentOutOfRangeException(nameof(quantity), "Quantity must be positive with at most three decimals.");
        if (unitPrice < 0m || decimal.Round(unitPrice, 2) != unitPrice)
            throw new ArgumentOutOfRangeException(nameof(unitPrice), "Unit price must be non-negative with at most two decimals.");
        if (discount < 0m || decimal.Round(discount, 2) != discount || discount > quantity * unitPrice)
            throw new ArgumentOutOfRangeException(nameof(discount), "Line discount is invalid.");
        if (unitCost < 0m || decimal.Round(unitCost, 2) != unitCost)
            throw new ArgumentOutOfRangeException(nameof(unitCost), "Unit cost is invalid.");

        Id = Guid.NewGuid();
        TenantId = tenantId;
        ProductId = productId;
        ProductName = productName.Trim();
        Sku = sku.Trim();
        Quantity = quantity;
        UnitPrice = unitPrice;
        Discount = discount;
        UnitCost = unitCost;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid SalesOrderId { get; private set; }
    public Guid ProductId { get; private set; }
    public string ProductName { get; private set; } = string.Empty;
    public string Sku { get; private set; } = string.Empty;
    public decimal Quantity { get; private set; }
    public decimal UnitPrice { get; private set; }
    public decimal Discount { get; private set; }
    public decimal UnitCost { get; private set; }
    public decimal LineTotal => decimal.Round(Quantity * UnitPrice - Discount, 2);
    public decimal CostTotal => decimal.Round(Quantity * UnitCost, 2);
}
