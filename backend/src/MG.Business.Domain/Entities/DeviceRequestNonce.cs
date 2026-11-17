namespace MG.Business.Domain.Entities;

public sealed class DeviceRequestNonce
{
    private DeviceRequestNonce() { }

    public DeviceRequestNonce(Guid tenantId, Guid deviceId, string nonce,
        DateTimeOffset createdAtUtc, DateTimeOffset expiresAtUtc)
    {
        if (tenantId == Guid.Empty || deviceId == Guid.Empty)
            throw new ArgumentException("Device request nonce scope is required.");
        if (!Guid.TryParseExact(nonce, "D", out _))
            throw new ArgumentException("Request nonce must be a UUID.", nameof(nonce));
        if (expiresAtUtc <= createdAtUtc)
            throw new ArgumentOutOfRangeException(nameof(expiresAtUtc));
        TenantId = tenantId;
        DeviceId = deviceId;
        Nonce = nonce.ToLowerInvariant();
        CreatedAtUtc = createdAtUtc.ToUniversalTime();
        ExpiresAtUtc = expiresAtUtc.ToUniversalTime();
    }

    public Guid TenantId { get; private set; }
    public Guid DeviceId { get; private set; }
    public string Nonce { get; private set; } = string.Empty;
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public DateTimeOffset ExpiresAtUtc { get; private set; }
}
