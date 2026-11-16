namespace MG.Business.Domain.Entities;

public sealed class DeviceCredential
{
    private DeviceCredential() { }

    public DeviceCredential(Guid tenantId, Guid actorId, Guid? branchId, Guid deviceId, string publicKeySpkiBase64,
        string credentialSha256, DateTimeOffset createdAtUtc)
    {
        if (tenantId == Guid.Empty || actorId == Guid.Empty || deviceId == Guid.Empty)
            throw new ArgumentException("Device credential identifiers are required.");
        if (string.IsNullOrWhiteSpace(publicKeySpkiBase64) || publicKeySpkiBase64.Length > 2048)
            throw new ArgumentException("A valid public key is required.", nameof(publicKeySpkiBase64));
        if (string.IsNullOrWhiteSpace(credentialSha256) || credentialSha256.Length != 64)
            throw new ArgumentException("A SHA-256 credential digest is required.", nameof(credentialSha256));
        TenantId = tenantId;
        ActorId = actorId;
        BranchId = branchId;
        DeviceId = deviceId;
        PublicKeySpkiBase64 = publicKeySpkiBase64;
        CredentialSha256 = credentialSha256;
        KeyVersion = 1;
        CreatedAtUtc = createdAtUtc.ToUniversalTime();
        LastRotatedAtUtc = CreatedAtUtc;
    }

    public void RotateCredential(string credentialSha256, DateTimeOffset nowUtc)
    {
        if (RevokedAtUtc is not null)
            throw new InvalidOperationException("A revoked device credential cannot be rotated.");
        if (string.IsNullOrWhiteSpace(credentialSha256) || credentialSha256.Length != 64
            || !credentialSha256.All(Uri.IsHexDigit))
            throw new ArgumentException("A SHA-256 credential digest is required.", nameof(credentialSha256));
        CredentialSha256 = credentialSha256.ToLowerInvariant();
        KeyVersion = checked(KeyVersion + 1);
        LastRotatedAtUtc = nowUtc.ToUniversalTime();
    }

    public void RecordUse(DateTimeOffset nowUtc)
    {
        if (RevokedAtUtc is null)
            LastUsedAtUtc = nowUtc.ToUniversalTime();
    }

    public void Revoke(DateTimeOffset nowUtc)
    {
        if (RevokedAtUtc is null)
            RevokedAtUtc = nowUtc.ToUniversalTime();
    }

    public bool IsActive => RevokedAtUtc is null;
    public Guid TenantId { get; private set; }
    public Guid DeviceId { get; private set; }
    public Guid ActorId { get; private set; }
    public Guid? BranchId { get; private set; }
    public string PublicKeySpkiBase64 { get; private set; } = string.Empty;
    public string CredentialSha256 { get; private set; } = string.Empty;
    public int KeyVersion { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public DateTimeOffset LastRotatedAtUtc { get; private set; }
    public DateTimeOffset? RevokedAtUtc { get; private set; }
    public DateTimeOffset? LastUsedAtUtc { get; private set; }
}
