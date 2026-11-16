using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class Account
{
    private Account() { }

    public Account(Guid tenantId, string code, string name, AccountType type)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (string.IsNullOrWhiteSpace(code)) throw new ArgumentException("Account code is required.", nameof(code));
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Account name is required.", nameof(name));
        if (!Enum.IsDefined(type)) throw new ArgumentOutOfRangeException(nameof(type));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        Code = code.Trim();
        Name = name.Trim();
        Type = type;
        IsActive = true;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public string Code { get; private set; } = string.Empty;
    public string Name { get; private set; } = string.Empty;
    public AccountType Type { get; private set; }
    public bool IsActive { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
}
