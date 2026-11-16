namespace MG.Business.Domain.Entities;

public sealed class Product
{
    private Product() { }

    public Product(Guid tenantId, string sku, string name, string baseUnit, bool tracksInventory = true, decimal defaultSalePrice = 0m)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (string.IsNullOrWhiteSpace(sku)) throw new ArgumentException("SKU is required.", nameof(sku));
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Product name is required.", nameof(name));
        if (string.IsNullOrWhiteSpace(baseUnit)) throw new ArgumentException("Base unit is required.", nameof(baseUnit));
        if (defaultSalePrice < 0m || decimal.Round(defaultSalePrice, 2) != defaultSalePrice) throw new ArgumentOutOfRangeException(nameof(defaultSalePrice));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        Sku = sku.Trim().ToUpperInvariant();
        Name = name.Trim();
        BaseUnit = baseUnit.Trim().ToLowerInvariant();
        TracksInventory = tracksInventory;
        DefaultSalePrice = defaultSalePrice;
        IsActive = true;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public string Sku { get; private set; } = string.Empty;
    public string Name { get; private set; } = string.Empty;
    public string BaseUnit { get; private set; } = string.Empty;
    public bool TracksInventory { get; private set; }
    public decimal DefaultSalePrice { get; private set; }
    public bool IsActive { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
}
