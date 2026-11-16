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

        group.MapPost("/operations", async (
            PushOperationsRequest request,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
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

            var accepted = new List<object>();
            foreach (var item in prepared)
            {
                if (existing.TryGetValue(item.Request.OperationId, out var prior))
                {
                    if (!string.Equals(prior.PayloadSha256, item.Hash, StringComparison.Ordinal))
                        return Results.Conflict(new { operationId = item.Request.OperationId, message = "Operation ID was previously used with a different payload." });
                    accepted.Add(new { operationId = prior.OperationId, sequence = prior.Sequence, duplicate = true });
                    continue;
                }

                var operation = new SyncOperation(tenantId, item.Request.DeviceId,
                    item.Request.BranchId, item.Request.OperationId, item.Request.OperationType,
                    item.Request.SchemaVersion, item.Request.AuthorityEpoch, item.Request.OccurredAtUtc,
                    item.Request.PayloadJson, item.Hash);
                db.SyncOperations.Add(operation);
                accepted.Add(operation);
            }

            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "A concurrent sync request used an operation ID. Retry the batch; the endpoint is idempotent." });
            }

            return Results.Accepted("/api/sync/operations", new
            {
                accepted = accepted.Select(x => x is SyncOperation op
                    ? new { operationId = op.OperationId, sequence = op.Sequence, duplicate = false }
                    : (dynamic)x).ToArray()
            });
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
                    x.Sequence, x.OperationId, x.DeviceId, x.BranchId,
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

    public sealed record PushOperationsRequest(List<PushOperationRequest> Operations);
    public sealed record PushOperationRequest(
        Guid OperationId, Guid DeviceId, Guid? BranchId, string OperationType,
        int SchemaVersion, long AuthorityEpoch, DateTimeOffset OccurredAtUtc,
        string PayloadJson, string? PayloadSha256);
}
