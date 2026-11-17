namespace MG.Business.Domain.Entities;

public sealed class DevicePresence
{
    private DevicePresence() { }

    public DevicePresence(Guid tenantId, Guid deviceId, Guid actorId, Guid? branchId,
        string deviceName, string platform, string clientVersion, DateTimeOffset nowUtc)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (deviceId == Guid.Empty) throw new ArgumentException("Device is required.", nameof(deviceId));
        if (actorId == Guid.Empty) throw new ArgumentException("Actor is required.", nameof(actorId));
        TenantId = tenantId;
        DeviceId = deviceId;
        ActorId = actorId;
        BranchId = branchId;
        DeviceName = Normalize(deviceName, 100, "Browser device");
        Platform = Normalize(platform, 40, "Web");
        ClientVersion = Normalize(clientVersion, 40, "unknown");
        FirstSeenAtUtc = nowUtc.ToUniversalTime();
        LastSeenAtUtc = FirstSeenAtUtc;
    }

    public void Heartbeat(Guid actorId, Guid? branchId, string deviceName, string platform,
        string clientVersion, DateTimeOffset nowUtc)
    {
        if (actorId == Guid.Empty) throw new ArgumentException("Actor is required.", nameof(actorId));
        if (actorId != ActorId) throw new UnauthorizedAccessException("A device presence record is bound to its enrolling actor.");
        BranchId = branchId;
        DeviceName = Normalize(deviceName, 100, DeviceName);
        Platform = Normalize(platform, 40, Platform);
        ClientVersion = Normalize(clientVersion, 40, ClientVersion);
        LastSeenAtUtc = nowUtc.ToUniversalTime();
    }

    private static string Normalize(string? value, int max, string fallback)
    {
        var normalized = string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        return normalized.Length <= max ? normalized : normalized[..max];
    }

    public Guid TenantId { get; private set; }
    public Guid DeviceId { get; private set; }
    public Guid ActorId { get; private set; }
    public Guid? BranchId { get; private set; }
    public string DeviceName { get; private set; } = string.Empty;
    public string Platform { get; private set; } = string.Empty;
    public string ClientVersion { get; private set; } = string.Empty;
    public DateTimeOffset FirstSeenAtUtc { get; private set; }
    public DateTimeOffset LastSeenAtUtc { get; private set; }
}
