namespace MG.Business.Domain.Entities;

public sealed class SyncOperation
{
    private SyncOperation() { }

    public SyncOperation(Guid tenantId, Guid actorId, Guid deviceId, Guid? branchId, Guid operationId,
        string operationType, int schemaVersion, long authorityEpoch,
        DateTimeOffset occurredAtUtc, string payloadJson, string payloadSha256)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (actorId == Guid.Empty) throw new ArgumentException("Actor is required.", nameof(actorId));
        if (deviceId == Guid.Empty) throw new ArgumentException("Device is required.", nameof(deviceId));
        if (operationId == Guid.Empty) throw new ArgumentException("Operation ID is required.", nameof(operationId));
        if (string.IsNullOrWhiteSpace(operationType) || operationType.Length > 100)
            throw new ArgumentException("Operation type must be between 1 and 100 characters.", nameof(operationType));
        if (schemaVersion < 1) throw new ArgumentOutOfRangeException(nameof(schemaVersion));
        if (authorityEpoch < 0) throw new ArgumentOutOfRangeException(nameof(authorityEpoch));
        if (string.IsNullOrWhiteSpace(payloadJson)) throw new ArgumentException("Payload is required.", nameof(payloadJson));
        if (string.IsNullOrWhiteSpace(payloadSha256) || payloadSha256.Length != 64)
            throw new ArgumentException("A SHA-256 payload hash is required.", nameof(payloadSha256));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        ActorId = actorId;
        DeviceId = deviceId;
        BranchId = branchId;
        OperationId = operationId;
        OperationType = operationType.Trim();
        SchemaVersion = schemaVersion;
        AuthorityEpoch = authorityEpoch;
        OccurredAtUtc = occurredAtUtc.ToUniversalTime();
        PayloadJson = payloadJson;
        PayloadSha256 = payloadSha256;
        ReceivedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public long Sequence { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid ActorId { get; private set; }
    public Guid DeviceId { get; private set; }
    public Guid? BranchId { get; private set; }
    public Guid OperationId { get; private set; }
    public string OperationType { get; private set; } = string.Empty;
    public int SchemaVersion { get; private set; }
    public long AuthorityEpoch { get; private set; }
    public DateTimeOffset OccurredAtUtc { get; private set; }
    public DateTimeOffset ReceivedAtUtc { get; private set; }
    public string PayloadJson { get; private set; } = string.Empty;
    public string PayloadSha256 { get; private set; } = string.Empty;
}
