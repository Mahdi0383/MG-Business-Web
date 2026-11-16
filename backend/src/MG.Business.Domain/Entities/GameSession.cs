using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class GameSession
{
    private GameSession() { }

    public GameSession(Guid tenantId, Guid branchId, Guid deviceId, Guid? customerId,
        decimal hourlyRate, int billingIntervalMinutes, DateTimeOffset? startedAtUtc = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (deviceId == Guid.Empty) throw new ArgumentException("Device is required.", nameof(deviceId));
        if (customerId == Guid.Empty) throw new ArgumentException("Customer ID cannot be empty.", nameof(customerId));
        if (hourlyRate <= 0m || decimal.Round(hourlyRate, 2) != hourlyRate)
            throw new ArgumentOutOfRangeException(nameof(hourlyRate));
        if (billingIntervalMinutes is < 1 or > 60)
            throw new ArgumentOutOfRangeException(nameof(billingIntervalMinutes));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        DeviceId = deviceId;
        CustomerId = customerId;
        HourlyRate = hourlyRate;
        BillingIntervalMinutes = billingIntervalMinutes;
        StartedAtUtc = startedAtUtc ?? DateTimeOffset.UtcNow;
        Status = GameSessionStatus.Active;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public Guid DeviceId { get; private set; }
    public Guid? CustomerId { get; private set; }
    public Guid? SalesOrderId { get; private set; }
    public decimal HourlyRate { get; private set; }
    public int BillingIntervalMinutes { get; private set; }
    public DateTimeOffset StartedAtUtc { get; private set; }
    public DateTimeOffset? EndedAtUtc { get; private set; }
    public int? BilledMinutes { get; private set; }
    public decimal? TotalAmount { get; private set; }
    public GameSessionStatus Status { get; private set; }

    public void Close(DateTimeOffset endedAtUtc)
    {
        if (Status != GameSessionStatus.Active)
            throw new InvalidOperationException("Only active sessions can be closed.");
        if (endedAtUtc <= StartedAtUtc)
            throw new ArgumentOutOfRangeException(nameof(endedAtUtc), "End time must be after start time.");

        var elapsedMinutes = (decimal)(endedAtUtc - StartedAtUtc).TotalMinutes;
        var billedMinutes = (int)(Math.Ceiling(elapsedMinutes / BillingIntervalMinutes) * BillingIntervalMinutes);
        billedMinutes = Math.Max(BillingIntervalMinutes, billedMinutes);

        BilledMinutes = billedMinutes;
        TotalAmount = decimal.Round(HourlyRate * billedMinutes / 60m, 2, MidpointRounding.AwayFromZero);
        EndedAtUtc = endedAtUtc;
        Status = GameSessionStatus.Closed;
    }

    public void LinkSalesOrder(Guid salesOrderId)
    {
        if (Status != GameSessionStatus.Closed)
            throw new InvalidOperationException("Close the session before linking a sales order.");
        if (salesOrderId == Guid.Empty || SalesOrderId.HasValue)
            throw new ArgumentException("A valid sales order can only be linked once.", nameof(salesOrderId));
        SalesOrderId = salesOrderId;
    }
}
