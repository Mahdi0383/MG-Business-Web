namespace MG.Business.Domain.Entities;

public sealed class GameDevice
{
    private GameDevice() { }

    public GameDevice(Guid tenantId, Guid branchId, Guid serviceProductId, string code, string name,
        decimal hourlyRate, int billingIntervalMinutes = 15)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (serviceProductId == Guid.Empty) throw new ArgumentException("Service product is required.", nameof(serviceProductId));
        if (string.IsNullOrWhiteSpace(code)) throw new ArgumentException("Device code is required.", nameof(code));
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Device name is required.", nameof(name));
        if (hourlyRate <= 0m || decimal.Round(hourlyRate, 2) != hourlyRate)
            throw new ArgumentOutOfRangeException(nameof(hourlyRate));
        if (billingIntervalMinutes is < 1 or > 60)
            throw new ArgumentOutOfRangeException(nameof(billingIntervalMinutes));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        ServiceProductId = serviceProductId;
        Code = code.Trim().ToUpperInvariant();
        Name = name.Trim();
        HourlyRate = hourlyRate;
        BillingIntervalMinutes = billingIntervalMinutes;
        IsActive = true;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public Guid ServiceProductId { get; private set; }
    public string Code { get; private set; } = string.Empty;
    public string Name { get; private set; } = string.Empty;
    public decimal HourlyRate { get; private set; }
    public int BillingIntervalMinutes { get; private set; }
    public bool IsActive { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
}
