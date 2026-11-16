using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class SyncEndpoints
{
    private const int MaxBatchSize = 100;
    private const int MaxPayloadBytes = 64 * 1024;

    public static IEndpointRouteBuilder MapSyncEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/sync").RequireAuthorization("TenantMember");

        group.MapPost("/heartbeat", async (
            DeviceHeartbeatRequest request,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid actorId)
                return Results.Forbid();
            if (request.DeviceId == Guid.Empty)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["deviceId"] = ["A valid device ID is required."]
                });

            var branchId = tenant.BranchId;
            var now = DateTimeOffset.UtcNow;
            var presence = await db.DevicePresences
                .SingleOrDefaultAsync(x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
            if (presence is null)
            {
                presence = new DevicePresence(tenantId, request.DeviceId, actorId, branchId,
                    request.DeviceName ?? "Browser device", request.Platform ?? "Web",
                    request.ClientVersion ?? "unknown", now);
                db.DevicePresences.Add(presence);
            }
            else
            {
                presence.Heartbeat(actorId, branchId, request.DeviceName ?? "Browser device",
                    request.Platform ?? "Web", request.ClientVersion ?? "unknown", now);
            }

            await db.SaveChangesAsync(cancellationToken);
            return Results.Ok(new { deviceId = request.DeviceId, lastSeenAtUtc = now, status = "online" });
        });

        group.MapGet("/devices", async (
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            if (tenant.Role is not ("Owner" or "Manager"))
                return Results.Forbid();

            var cutoff = DateTimeOffset.UtcNow.AddSeconds(-90);
            var devices = await db.DevicePresences.AsNoTracking()
                .Where(x => x.TenantId == tenantId)
                .OrderByDescending(x => x.LastSeenAtUtc)
                .Select(x => new
                {
                    x.DeviceId, x.ActorId, x.BranchId, x.DeviceName, x.Platform,
                    x.ClientVersion, x.FirstSeenAtUtc, x.LastSeenAtUtc
                })
                .ToListAsync(cancellationToken);

            return Results.Ok(devices.Select(device => new
            {
                device.DeviceId, device.ActorId, device.BranchId, device.DeviceName,
                device.Platform, device.ClientVersion, device.FirstSeenAtUtc, device.LastSeenAtUtc,
                isOnline = device.LastSeenAtUtc >= cutoff,
                alertCode = device.LastSeenAtUtc >= cutoff ? null : "DEVICE_HEARTBEAT_MISSED"
            }));
        });

        group.MapPost("/operations", async (
            PushOperationsRequest request,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid actorId)
                return Results.Forbid();
            if (request.Operations is null || request.Operations.Count is < 1 or > MaxBatchSize)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["operations"] = [$"Batch size must be between 1 and {MaxBatchSize}."]
                });

            var prepared = new List<(PushOperationRequest Request, string Hash)>();
            foreach (var item in request.Operations)
            {
                if (item.DeviceId == Guid.Empty || item.OperationId == Guid.Empty
                    || item.SchemaVersion < 1 || item.AuthorityEpoch < 0
                    || string.IsNullOrWhiteSpace(item.OperationType) || item.OperationType.Length > 100
                    || string.IsNullOrWhiteSpace(item.PayloadJson)
                    || Encoding.UTF8.GetByteCount(item.PayloadJson) > MaxPayloadBytes)
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["operations"] = ["An operation has invalid identifiers, version, type or payload size."]
                    });

                try { using var _ = JsonDocument.Parse(item.PayloadJson); }
                catch (JsonException)
                {
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["payloadJson"] = ["Payload must contain valid JSON."]
                    });
                }

                var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(item.PayloadJson)));
                if (!string.IsNullOrWhiteSpace(item.PayloadSha256)
                    && !CryptographicOperations.FixedTimeEquals(
                        Encoding.ASCII.GetBytes(hash),
                        Encoding.ASCII.GetBytes(item.PayloadSha256.ToUpperInvariant())))
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["payloadSha256"] = ["Payload hash does not match the payload."]
                    });

                prepared.Add((item, hash));
            }

            if (prepared.Select(x => x.Request.OperationId).Distinct().Count() != prepared.Count)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["operations"] = ["Duplicate operation IDs in one batch are not allowed."]
                });

            var branchIds = prepared.Where(x => x.Request.BranchId.HasValue)
                .Select(x => x.Request.BranchId!.Value).Distinct().ToArray();
            if (branchIds.Length > 0)
            {
                var validBranches = await db.Branches.AsNoTracking()
                    .Where(x => x.TenantId == tenantId && branchIds.Contains(x.Id))
                    .Select(x => x.Id).ToListAsync(cancellationToken);
                if (validBranches.Count != branchIds.Length)
                    return Results.BadRequest(new { message = "One or more branches do not belong to this tenant." });
            }

            var operationIds = prepared.Select(x => x.Request.OperationId).ToArray();
            var existing = await db.SyncOperations.AsNoTracking()
                .Where(x => x.TenantId == tenantId && operationIds.Contains(x.OperationId))
                .ToDictionaryAsync(x => x.OperationId, cancellationToken);

            var acknowledgements = new Dictionary<Guid, SyncOperationAck>();
            var inserted = new List<SyncOperation>();
            foreach (var item in prepared)
            {
                if (existing.TryGetValue(item.Request.OperationId, out var prior))
                {
                    if (!string.Equals(prior.PayloadSha256, item.Hash, StringComparison.Ordinal)
                        || prior.ActorId != actorId
                        || prior.DeviceId != item.Request.DeviceId
                        || prior.BranchId != item.Request.BranchId
                        || prior.OperationType != item.Request.OperationType
                        || prior.SchemaVersion != item.Request.SchemaVersion
                        || prior.AuthorityEpoch != item.Request.AuthorityEpoch
                        || prior.OccurredAtUtc != item.Request.OccurredAtUtc)
                        return Results.Conflict(new { operationId = item.Request.OperationId, message = "Operation ID was previously used with a different payload." });
                    acknowledgements.Add(prior.OperationId, new SyncOperationAck(prior.OperationId, prior.Sequence, true));
                    continue;
                }

                var operation = new SyncOperation(tenantId, actorId, item.Request.DeviceId,
                    item.Request.BranchId, item.Request.OperationId, item.Request.OperationType,
                    item.Request.SchemaVersion, item.Request.AuthorityEpoch, item.Request.OccurredAtUtc,
                    item.Request.PayloadJson, item.Hash);
                db.SyncOperations.Add(operation);
                inserted.Add(operation);
            }

            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "A concurrent sync request used an operation ID. Retry the batch; the endpoint is idempotent." });
            }

            foreach (var operation in inserted)
                acknowledgements.Add(operation.OperationId, new SyncOperationAck(operation.OperationId, operation.Sequence, false));
            var accepted = prepared.Select(x => acknowledgements[x.Request.OperationId]).ToArray();
            return Results.Accepted("/api/sync/operations", new { accepted });
        });

        group.MapGet("/operations", async (
            long afterSequence,
            int? limit,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            if (afterSequence < 0)
                return Results.BadRequest(new { message = "afterSequence cannot be negative." });

            var take = Math.Clamp(limit ?? 100, 1, 200);
            var operations = await db.SyncOperations.AsNoTracking()
                .Where(x => x.TenantId == tenantId && x.Sequence > afterSequence)
                .OrderBy(x => x.Sequence)
                .Take(take)
                .Select(x => new
                {
                    x.Sequence, x.OperationId, x.ActorId, x.DeviceId, x.BranchId,
                    x.OperationType, x.SchemaVersion, x.AuthorityEpoch,
                    x.OccurredAtUtc, x.ReceivedAtUtc, x.PayloadJson, x.PayloadSha256
                })
                .ToListAsync(cancellationToken);

            return Results.Ok(new
            {
                operations,
                nextSequence = operations.Count == 0 ? afterSequence : operations[^1].Sequence,
                hasMore = operations.Count == take
            });
        });

        return endpoints;
    }

    public sealed record SyncOperationAck(Guid OperationId, long Sequence, bool Duplicate);
    public sealed record DeviceHeartbeatRequest(Guid DeviceId, string? DeviceName, string? Platform, string? ClientVersion);\n    public sealed record PushOperationsRequest(List<PushOperationRequest> Operations);
    public sealed record PushOperationRequest(
        Guid OperationId, Guid DeviceId, Guid? BranchId, string OperationType,
        int SchemaVersion, long AuthorityEpoch, DateTimeOffset OccurredAtUtc,
        string PayloadJson, string? PayloadSha256);
}
