namespace MG.Business.Domain.Entities;

public sealed class Customer
{
    private Customer() { }

    public Customer(Guid tenantId, string code, string name, string? phone = null, string? email = null, string? address = null, string? notes = null)
        : this(Guid.NewGuid(), tenantId, code, name, phone, email, address, notes)
    {
    }

    public Customer(Guid id, Guid tenantId, string code, string name, string? phone = null, string? email = null, string? address = null, string? notes = null)
    {
        if (id == Guid.Empty) throw new ArgumentException("Customer ID is required.", nameof(id));
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (string.IsNullOrWhiteSpace(code)) throw new ArgumentException("Customer code is required.", nameof(code));
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Customer name is required.", nameof(name));

        Id = id;
        TenantId = tenantId;
        Code = code.Trim().ToUpperInvariant();
        Name = name.Trim();
        Phone = NormalizeOptional(phone, 40);
        Email = NormalizeOptional(email, 254)?.ToLowerInvariant();
        Address = NormalizeOptional(address, 500);
        Notes = NormalizeOptional(notes, 1000);
        IsActive = true;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public string Code { get; private set; } = string.Empty;
    public string Name { get; private set; } = string.Empty;
    public string? Phone { get; private set; }
    public string? Email { get; private set; }
    public string? Address { get; private set; }
    public string? Notes { get; private set; }
    public bool IsActive { get; private set; }
    public int Version { get; private set; } = 1;
    public DateTimeOffset CreatedAtUtc { get; private set; }

    public void UpdateContact(string name, string? phone, string? email, string? address, string? notes)
    {
        if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("Customer name is required.", nameof(name));
        Name = name.Trim();
        Phone = NormalizeOptional(phone, 40);
        Email = NormalizeOptional(email, 254)?.ToLowerInvariant();
        Address = NormalizeOptional(address, 500);
        Notes = NormalizeOptional(notes, 1000);
        checked { Version++; }
    }

    public void Deactivate()
    {
        if (!IsActive) return;
        IsActive = false;
        checked { Version++; }
    }

    private static string? NormalizeOptional(string? value, int maxLength)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        var normalized = value.Trim();
        if (normalized.Length > maxLength)
            throw new ArgumentException($"Value cannot exceed {maxLength} characters.");
        return normalized;
    }
}
