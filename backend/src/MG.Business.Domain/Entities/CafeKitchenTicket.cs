using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class CafeKitchenTicket
{
    private CafeKitchenTicket() { }

    public CafeKitchenTicket(Guid tenantId, Guid branchId, Guid salesOrderId, CafeServiceType serviceType,
        string? tableLabel = null, string? notes = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (salesOrderId == Guid.Empty) throw new ArgumentException("Sales order is required.", nameof(salesOrderId));
        if (!Enum.IsDefined(serviceType)) throw new ArgumentOutOfRangeException(nameof(serviceType));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        SalesOrderId = salesOrderId;
        ServiceType = serviceType;
        TableLabel = Normalize(tableLabel, 40);
        Notes = Normalize(notes, 500);
        Status = KitchenTicketStatus.Pending;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public Guid SalesOrderId { get; private set; }
    public CafeServiceType ServiceType { get; private set; }
    public string? TableLabel { get; private set; }
    public string? Notes { get; private set; }
    public KitchenTicketStatus Status { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public DateTimeOffset? PreparingAtUtc { get; private set; }
    public DateTimeOffset? ReadyAtUtc { get; private set; }
    public DateTimeOffset? CompletedAtUtc { get; private set; }

    public void TransitionTo(KitchenTicketStatus next)
    {
        var valid = (Status, next) switch
        {
            (KitchenTicketStatus.Pending, KitchenTicketStatus.Preparing) => true,
            (KitchenTicketStatus.Pending, KitchenTicketStatus.Cancelled) => true,
            (KitchenTicketStatus.Preparing, KitchenTicketStatus.Ready) => true,
            (KitchenTicketStatus.Preparing, KitchenTicketStatus.Cancelled) => true,
            (KitchenTicketStatus.Ready, KitchenTicketStatus.Completed) => true,
            _ => false
        };

        if (!valid)
            throw new InvalidOperationException($"Cannot transition kitchen ticket from {Status} to {next}.");

        var now = DateTimeOffset.UtcNow;
        Status = next;
        if (next == KitchenTicketStatus.Preparing) PreparingAtUtc = now;
        if (next == KitchenTicketStatus.Ready) ReadyAtUtc = now;
        if (next is KitchenTicketStatus.Completed or KitchenTicketStatus.Cancelled) CompletedAtUtc = now;
    }

    private static string? Normalize(string? value, int maxLength)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        var normalized = value.Trim();
        if (normalized.Length > maxLength) throw new ArgumentException($"Value cannot exceed {maxLength} characters.");
        return normalized;
    }
}
