namespace MG.Business.Domain.Entities;

public sealed class DeviceEnrollmentChallenge
{
    private DeviceEnrollmentChallenge() { }

    public DeviceEnrollmentChallenge(Guid id, Guid tenantId, Guid actorId, Guid? branchId,
        Guid deviceId, string challengeSha256, DateTimeOffset createdAtUtc, DateTimeOffset expiresAtUtc)
    {
        if (id == Guid.Empty || tenantId == Guid.Empty || actorId == Guid.Empty || deviceId == Guid.Empty)
            throw new ArgumentException("Enrollment identifiers are required.");
        if (string.IsNullOrWhiteSpace(challengeSha256) || challengeSha256.Length != 64)
            throw new ArgumentException("A SHA-256 challenge digest is required.", nameof(challengeSha256));
        if (expiresAtUtc <= createdAtUtc || expiresAtUtc - createdAtUtc > TimeSpan.FromMinutes(5))
            throw new ArgumentOutOfRangeException(nameof(expiresAtUtc), "Enrollment challenges must expire within five minutes.");
        Id = id;
        TenantId = tenantId;
        ActorId = actorId;
        BranchId = branchId;
        DeviceId = deviceId;
        ChallengeSha256 = challengeSha256;
        CreatedAtUtc = createdAtUtc.ToUniversalTime();
        ExpiresAtUtc = expiresAtUtc.ToUniversalTime();
    }

    public bool TryConsume(Guid tenantId, Guid actorId, Guid? branchId, Guid deviceId,
        string challengeSha256, DateTimeOffset nowUtc)
    {
        if (ConsumedAtUtc is not null || nowUtc.ToUniversalTime() >= ExpiresAtUtc
            || TenantId != tenantId || ActorId != actorId || BranchId != branchId || DeviceId != deviceId
            || !CryptographicOperations.FixedTimeEquals(
                System.Text.Encoding.ASCII.GetBytes(ChallengeSha256),
                System.Text.Encoding.ASCII.GetBytes(challengeSha256)))
            return false;
        ConsumedAtUtc = nowUtc.ToUniversalTime();
        return true;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid ActorId { get; private set; }
    public Guid? BranchId { get; private set; }
    public Guid DeviceId { get; private set; }
    public string ChallengeSha256 { get; private set; } = string.Empty;
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public DateTimeOffset ExpiresAtUtc { get; private set; }
    public DateTimeOffset? ConsumedAtUtc { get; private set; }
}
