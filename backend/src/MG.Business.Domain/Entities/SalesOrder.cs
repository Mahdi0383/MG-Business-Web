using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class SalesOrder
{
    private readonly List<SalesOrderLine> _lines = [];
    private SalesOrder() { }

    public SalesOrder(Guid tenantId, Guid branchId, string number, Guid? customerId,
        IEnumerable<SalesOrderLine> lines, decimal discount)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (string.IsNullOrWhiteSpace(number)) throw new ArgumentException("Order number is required.", nameof(number));
        if (customerId == Guid.Empty) throw new ArgumentException("Customer ID cannot be empty.", nameof(customerId));
        ArgumentNullException.ThrowIfNull(lines);

        var materialized = lines.ToList();
        if (materialized.Count == 0) throw new ArgumentException("A sales order requires at least one line.", nameof(lines));
        if (materialized.Any(x => x.TenantId != tenantId))
            throw new ArgumentException("All order lines must belong to the same tenant.", nameof(lines));
        if (discount < 0m || decimal.Round(discount, 2) != discount)
            throw new ArgumentOutOfRangeException(nameof(discount));

        var subtotal = materialized.Sum(x => x.Quantity * x.UnitPrice);
        var lineDiscount = materialized.Sum(x => x.Discount);
        if (discount + lineDiscount > subtotal)
            throw new ArgumentException("Discount cannot exceed the subtotal.", nameof(discount));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        Number = number.Trim();
        CustomerId = customerId;
        Discount = discount;
        Subtotal = decimal.Round(subtotal, 2);
        LineDiscountTotal = decimal.Round(lineDiscount, 2);
        Total = decimal.Round(Subtotal - LineDiscountTotal - Discount, 2);
        if (Total <= 0m) throw new ArgumentException("Sales order total must be greater than zero.", nameof(lines));

        Status = SalesOrderStatus.Completed;
        CreatedAtUtc = DateTimeOffset.UtcNow;
        CompletedAtUtc = CreatedAtUtc;
        _lines.AddRange(materialized);
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public string Number { get; private set; } = string.Empty;
    public Guid? CustomerId { get; private set; }
    public decimal Subtotal { get; private set; }
    public decimal LineDiscountTotal { get; private set; }
    public decimal Discount { get; private set; }
    public decimal Total { get; private set; }
    public decimal PaidAmount { get; private set; }
    public decimal BalanceDue => Total - PaidAmount;
    public SalesOrderStatus Status { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public DateTimeOffset CompletedAtUtc { get; private set; }
    public IReadOnlyCollection<SalesOrderLine> Lines => _lines.AsReadOnly();

    public void SetPaidAmount(decimal paidAmount)
    {
        if (paidAmount < 0m || paidAmount > Total || decimal.Round(paidAmount, 2) != paidAmount)
            throw new ArgumentOutOfRangeException(nameof(paidAmount));
        if (paidAmount < Total && CustomerId is null)
            throw new ArgumentException("A customer is required when a sale has an unpaid balance.", nameof(paidAmount));
        PaidAmount = paidAmount;
    }
}
