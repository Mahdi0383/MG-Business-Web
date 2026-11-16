namespace MG.Business.Domain.Entities;

public sealed class SyncDeviceCursor
{
    private SyncDeviceCursor() { }

    public SyncDeviceCursor(Guid tenantId, Guid deviceId, Guid actorId, long sequence, DateTimeOffset nowUtc)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (deviceId == Guid.Empty) throw new ArgumentException("Device is required.", nameof(deviceId));
        if (actorId == Guid.Empty) throw new ArgumentException("Actor is required.", nameof(actorId));
        if (sequence < 0) throw new ArgumentOutOfRangeException(nameof(sequence));
        TenantId = tenantId;
        DeviceId = deviceId;
        ActorId = actorId;
        LastAcknowledgedSequence = sequence;
        UpdatedAtUtc = nowUtc.ToUniversalTime();
    }

    public bool Advance(Guid actorId, long sequence, DateTimeOffset nowUtc)
    {
        if (actorId == Guid.Empty) throw new ArgumentException("Actor is required.", nameof(actorId));
        if (sequence < LastAcknowledgedSequence) return false;
        ActorId = actorId;
        LastAcknowledgedSequence = sequence;
        UpdatedAtUtc = nowUtc.ToUniversalTime();
        return true;
    }

    public Guid TenantId { get; private set; }
    public Guid DeviceId { get; private set; }
    public Guid ActorId { get; private set; }
    public long LastAcknowledgedSequence { get; private set; }
    public DateTimeOffset UpdatedAtUtc { get; private set; }
}
