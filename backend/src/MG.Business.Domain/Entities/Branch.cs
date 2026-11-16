namespace MG.Business.Domain.Entities;

public sealed class Branch
{
    private Branch() { }

    public Branch(Guid tenantId, string name, string code)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Branch name is required.", nameof(name));
        if (string.IsNullOrWhiteSpace(code)) throw new ArgumentException("Branch code is required.", nameof(code));
        Id = Guid.NewGuid();
        TenantId = tenantId;
        Name = name.Trim();
        Code = code.Trim().ToUpperInvariant();
        CreatedAtUtc = DateTimeOffset.UtcNow;
        IsActive = true;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public string Name { get; private set; } = string.Empty;
    public string Code { get; private set; } = string.Empty;
    public bool IsActive { get; private set; }

    public void UpdateDetails(string name, string code)
    {
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Branch name is required.", nameof(name));
        if (string.IsNullOrWhiteSpace(code)) throw new ArgumentException("Branch code is required.", nameof(code));
        Name = name.Trim();
        Code = code.Trim().ToUpperInvariant();
    }

    public void SetActive(bool isActive) => IsActive = isActive;
    public DateTimeOffset CreatedAtUtc { get; private set; }
}