using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class StockMovement
{
    private StockMovement() { }

    public StockMovement(Guid tenantId, Guid branchId, Guid productId, StockMovementType type,
        decimal quantity, decimal unitCost, string? reference = null,
        string? idempotencyKey = null, string? requestFingerprint = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (productId == Guid.Empty) throw new ArgumentException("Product is required.", nameof(productId));
        if (!Enum.IsDefined(type)) throw new ArgumentOutOfRangeException(nameof(type));
        if (quantity <= 0m || decimal.Round(quantity, 3) != quantity)
            throw new ArgumentOutOfRangeException(nameof(quantity), "Quantity must be positive and support at most three decimal places.");
        if (unitCost < 0m || decimal.Round(unitCost, 2) != unitCost)
            throw new ArgumentOutOfRangeException(nameof(unitCost), "Unit cost cannot be negative and supports at most two decimal places.");

        if ((idempotencyKey is null) != (requestFingerprint is null))
            throw new ArgumentException("Idempotency key and request fingerprint must be supplied together.");
        if (idempotencyKey is not null && (idempotencyKey.Length is < 1 or > 128 || idempotencyKey.Any(ch => ch < 0x21 || ch > 0x7e)))
            throw new ArgumentException("Idempotency key must contain 1 to 128 visible ASCII characters.", nameof(idempotencyKey));
        if (requestFingerprint is not null && (requestFingerprint.Length != 64 || requestFingerprint.Any(ch => !Uri.IsHexDigit(ch))))
            throw new ArgumentException("Request fingerprint must be a SHA-256 hex digest.", nameof(requestFingerprint));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        ProductId = productId;
        Type = type;
        Quantity = quantity;
        UnitCost = unitCost;
        Reference = string.IsNullOrWhiteSpace(reference) ? null : reference.Trim();
        IdempotencyKey = idempotencyKey;
        RequestFingerprint = requestFingerprint?.ToLowerInvariant();
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public Guid ProductId { get; private set; }
    public StockMovementType Type { get; private set; }
    public decimal Quantity { get; private set; }
    public decimal UnitCost { get; private set; }
    public string? Reference { get; private set; }
    public string? IdempotencyKey { get; private set; }
    public string? RequestFingerprint { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }

    public decimal SignedQuantity => Type switch
    {
        StockMovementType.Receipt or StockMovementType.SalesReturn or StockMovementType.AdjustmentIncrease => Quantity,
        StockMovementType.Issue or StockMovementType.Sale or StockMovementType.PurchaseReturn or StockMovementType.AdjustmentDecrease => -Quantity,
        _ => throw new InvalidOperationException("Unsupported stock movement type.")
    };
}
