using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class SalesPayment
{
    private SalesPayment() { }

    public SalesPayment(Guid tenantId, Guid branchId, Guid salesOrderId, decimal amount,
        PaymentMethod method, string? reference = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (salesOrderId == Guid.Empty) throw new ArgumentException("Sales order is required.", nameof(salesOrderId));
        if (amount <= 0m || decimal.Round(amount, 2) != amount)
            throw new ArgumentOutOfRangeException(nameof(amount));
        if (!Enum.IsDefined(method) || method == PaymentMethod.OnAccount)
            throw new ArgumentOutOfRangeException(nameof(method));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        SalesOrderId = salesOrderId;
        Amount = amount;
        Method = method;
        Reference = string.IsNullOrWhiteSpace(reference) ? null : reference.Trim();
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public Guid SalesOrderId { get; private set; }
    public decimal Amount { get; private set; }
    public PaymentMethod Method { get; private set; }
    public string? Reference { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
}
