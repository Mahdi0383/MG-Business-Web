namespace MG.Business.Domain.Entities;

public sealed class DeviceAuthorityLease
{
    private DeviceAuthorityLease() { }

    public DeviceAuthorityLease(Guid id, Guid tenantId, string scopeKey, Guid? branchId,
        Guid deviceId, DateTimeOffset issuedAtUtc, DateTimeOffset expiresAtUtc)
    {
        if (id == Guid.Empty || tenantId == Guid.Empty || deviceId == Guid.Empty)
            throw new ArgumentException("Authority lease identifiers are required.");
        if (string.IsNullOrWhiteSpace(scopeKey) || scopeKey.Length > 36)
            throw new ArgumentException("A valid authority scope key is required.", nameof(scopeKey));
        if (expiresAtUtc <= issuedAtUtc || expiresAtUtc - issuedAtUtc > TimeSpan.FromMinutes(5))
            throw new ArgumentOutOfRangeException(nameof(expiresAtUtc), "Authority leases may not exceed five minutes.");
        Id = id;
        LeaseId = Guid.NewGuid();
        TenantId = tenantId;
        ScopeKey = scopeKey;
        BranchId = branchId;
        DeviceId = deviceId;
        Epoch = 1;
        IssuedAtUtc = issuedAtUtc.ToUniversalTime();
        ExpiresAtUtc = expiresAtUtc.ToUniversalTime();
    }

    public bool IsActiveAt(DateTimeOffset nowUtc)
        => RevokedAtUtc is null && nowUtc.ToUniversalTime() < ExpiresAtUtc;

    public bool TryAcquire(Guid deviceId, DateTimeOffset nowUtc, DateTimeOffset expiresAtUtc)
    {
        if (deviceId == Guid.Empty)
            throw new ArgumentException("A device ID is required.", nameof(deviceId));
        var now = nowUtc.ToUniversalTime();
        if (IsActiveAt(now) && DeviceId != deviceId)
            return false;
        if (!IsActiveAt(now))
        {
            Epoch = checked(Epoch + 1);
            LeaseId = Guid.NewGuid();
            DeviceId = deviceId;
            RevokedAtUtc = null;
        }
        IssuedAtUtc = now;
        ExpiresAtUtc = expiresAtUtc.ToUniversalTime();
        SignatureBase64 = string.Empty;
        return true;
    }

    public void SetSignature(string signatureBase64)
    {
        if (string.IsNullOrWhiteSpace(signatureBase64) || signatureBase64.Length > 256)
            throw new ArgumentException("A valid authority lease signature is required.", nameof(signatureBase64));
        SignatureBase64 = signatureBase64;
    }

    public void Revoke(DateTimeOffset nowUtc)
    {
        if (RevokedAtUtc is null)
        {
            RevokedAtUtc = nowUtc.ToUniversalTime();
            SignatureBase64 = string.Empty;
        }
    }

    public Guid Id { get; private set; }
    public Guid LeaseId { get; private set; }
    public Guid TenantId { get; private set; }
    public string ScopeKey { get; private set; } = string.Empty;
    public Guid? BranchId { get; private set; }
    public Guid DeviceId { get; private set; }
    public long Epoch { get; private set; }
    public DateTimeOffset IssuedAtUtc { get; private set; }
    public DateTimeOffset ExpiresAtUtc { get; private set; }
    public DateTimeOffset? RevokedAtUtc { get; private set; }
    public string SignatureBase64 { get; private set; } = string.Empty;
}
