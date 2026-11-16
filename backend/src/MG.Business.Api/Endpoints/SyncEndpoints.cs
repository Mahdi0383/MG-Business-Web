using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
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

        group.MapPost("/acknowledgements", async (
            SyncAcknowledgementRequest request,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid actorId)
                return Results.Forbid();
            if (request.DeviceId == Guid.Empty || request.Sequence < 0)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["acknowledgement"] = ["A valid device ID and non-negative sequence are required."]
                });
            if (request.BranchId != tenant.BranchId)
                return Results.Forbid();

            var enrolledDevice = await db.DevicePresences.AsNoTracking().AnyAsync(
                x => x.TenantId == tenantId && x.DeviceId == request.DeviceId && x.ActorId == actorId,
                cancellationToken);
            if (!enrolledDevice)
                return Results.Forbid();

            if (request.Sequence > 0 && !await db.SyncOperations.AsNoTracking()
                .AnyAsync(x => x.TenantId == tenantId && x.Sequence == request.Sequence, cancellationToken))
                return Results.BadRequest(new { message = "Acknowledgement sequence does not belong to this tenant journal." });

            var latestTenantSequence = await db.SyncOperations.AsNoTracking()
                .Where(x => x.TenantId == tenantId)
                .Select(x => (long?)x.Sequence)
                .MaxAsync(cancellationToken) ?? 0;
            if (request.Sequence > latestTenantSequence)
                return Results.BadRequest(new { message = "Acknowledgement cannot advance beyond the tenant journal head." });

            var now = DateTimeOffset.UtcNow;
            if (request.BranchId is Guid branchId)
            {
                var cursor = await db.SyncDeviceBranchCursors.SingleOrDefaultAsync(
                    x => x.TenantId == tenantId && x.DeviceId == request.DeviceId && x.BranchId == branchId,
                    cancellationToken);
                var isNewCursor = cursor is null;
                if (isNewCursor)
                {
                    cursor = new SyncDeviceBranchCursor(tenantId, request.DeviceId, branchId, actorId, request.Sequence, now);
                    db.SyncDeviceBranchCursors.Add(cursor);
                }
                else if (cursor!.ActorId != actorId)
                {
                    return Results.Forbid();
                }
                else if (!cursor!.Advance(actorId, request.Sequence, now))
                {
                    return Results.Ok(new { deviceId = request.DeviceId, branchId, sequence = cursor.LastAcknowledgedSequence, staleAcknowledgement = true });
                }

                try
                {
                    await db.SaveChangesAsync(cancellationToken);
                }
                catch (DbUpdateException) when (isNewCursor)
                {
                    db.Entry(cursor!).State = EntityState.Detached;
                    var concurrent = await db.SyncDeviceBranchCursors.SingleOrDefaultAsync(
                        x => x.TenantId == tenantId && x.DeviceId == request.DeviceId && x.BranchId == branchId,
                        cancellationToken);
                    if (concurrent is null) throw;
                    if (concurrent.ActorId != actorId) return Results.Forbid();
                    if (!concurrent.Advance(actorId, request.Sequence, now))
                        return Results.Ok(new { deviceId = request.DeviceId, branchId, sequence = concurrent.LastAcknowledgedSequence, staleAcknowledgement = true });
                    await db.SaveChangesAsync(cancellationToken);
                    cursor = concurrent;
                }
                catch (DbUpdateConcurrencyException) when (!isNewCursor)
                {
                    db.Entry(cursor!).State = EntityState.Detached;
                    var latest = await db.SyncDeviceBranchCursors.SingleAsync(
                        x => x.TenantId == tenantId && x.DeviceId == request.DeviceId && x.BranchId == branchId,
                        cancellationToken);
                    if (latest.ActorId != actorId) return Results.Forbid();
                    if (!latest.Advance(actorId, request.Sequence, now))
                        return Results.Ok(new { deviceId = request.DeviceId, branchId, sequence = latest.LastAcknowledgedSequence, staleAcknowledgement = true });
                    await db.SaveChangesAsync(cancellationToken);
                    cursor = latest;
                }

                return Results.Ok(new { deviceId = request.DeviceId, branchId, sequence = cursor!.LastAcknowledgedSequence, staleAcknowledgement = false });
            }

            // A null branch context is the explicit all-branches cursor for Owner/Manager views.
            if (tenant.Role is not ("Owner" or "Manager"))
                return Results.Forbid();
            var tenantCursor = await db.SyncDeviceCursors.SingleOrDefaultAsync(
                x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
            var isNewTenantCursor = tenantCursor is null;
            if (isNewTenantCursor)
            {
                tenantCursor = new SyncDeviceCursor(tenantId, request.DeviceId, actorId, request.Sequence, now);
                db.SyncDeviceCursors.Add(tenantCursor);
            }
            else if (tenantCursor!.ActorId != actorId)
            {
                return Results.Forbid();
            }
            else if (!tenantCursor!.Advance(actorId, request.Sequence, now))
            {
                return Results.Ok(new { deviceId = request.DeviceId, sequence = tenantCursor.LastAcknowledgedSequence, staleAcknowledgement = true });
            }

            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException) when (isNewTenantCursor)
            {
                db.Entry(tenantCursor!).State = EntityState.Detached;
                var concurrent = await db.SyncDeviceCursors.SingleOrDefaultAsync(
                    x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
                if (concurrent is null) throw;
                if (concurrent.ActorId != actorId) return Results.Forbid();
                if (!concurrent.Advance(actorId, request.Sequence, now))
                    return Results.Ok(new { deviceId = request.DeviceId, sequence = concurrent.LastAcknowledgedSequence, staleAcknowledgement = true });
                await db.SaveChangesAsync(cancellationToken);
                tenantCursor = concurrent;
            }
            catch (DbUpdateConcurrencyException) when (!isNewTenantCursor)
            {
                db.Entry(tenantCursor!).State = EntityState.Detached;
                var latest = await db.SyncDeviceCursors.SingleAsync(
                    x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
                if (latest.ActorId != actorId) return Results.Forbid();
                if (!latest.Advance(actorId, request.Sequence, now))
                    return Results.Ok(new { deviceId = request.DeviceId, sequence = latest.LastAcknowledgedSequence, staleAcknowledgement = true });
                await db.SaveChangesAsync(cancellationToken);
                tenantCursor = latest;
            }

            return Results.Ok(new { deviceId = request.DeviceId, sequence = tenantCursor!.LastAcknowledgedSequence, staleAcknowledgement = false });
        });

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
            var isNew = presence is null;
            if (isNew)
            {
                presence = new DevicePresence(tenantId, request.DeviceId, actorId, branchId,
                    request.DeviceName ?? "Browser device", request.Platform ?? "Web",
                    request.ClientVersion ?? "unknown", now);
                db.DevicePresences.Add(presence);
            }
            else
            {
                if (presence!.ActorId != actorId) return Results.Forbid();
                presence.Heartbeat(actorId, branchId, request.DeviceName ?? "Browser device",
                    request.Platform ?? "Web", request.ClientVersion ?? "unknown", now);
            }

            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException) when (isNew)
            {
                db.Entry(presence!).State = EntityState.Detached;
                var concurrent = await db.DevicePresences.SingleOrDefaultAsync(
                    x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
                if (concurrent is null) throw;
                if (concurrent.ActorId != actorId) return Results.Forbid();
                concurrent.Heartbeat(actorId, branchId, request.DeviceName ?? "Browser device",
                    request.Platform ?? "Web", request.ClientVersion ?? "unknown", now);
                await db.SaveChangesAsync(cancellationToken);
            }
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
                .Where(x => x.TenantId == tenantId
                    && (tenant.Role == "Owner" || tenant.BranchId == null
                        || x.BranchId == tenant.BranchId || x.BranchId == null))
                .Select(x => new
                {
                    x.DeviceId, x.ActorId, x.BranchId, x.DeviceName, x.Platform,
                    x.ClientVersion, x.FirstSeenAtUtc, x.LastSeenAtUtc
                })
                .ToListAsync(cancellationToken);
            var acknowledgedSequences = await db.SyncDeviceCursors.AsNoTracking()
                .Where(x => x.TenantId == tenantId)
                .ToDictionaryAsync(x => x.DeviceId, x => x.LastAcknowledgedSequence, cancellationToken);
            var branchAcknowledgedSequences = await db.SyncDeviceBranchCursors.AsNoTracking()
                .Where(x => x.TenantId == tenantId)
                .ToDictionaryAsync(x => (x.DeviceId, x.BranchId), x => x.LastAcknowledgedSequence, cancellationToken);
            var latestJournalSequence = await db.SyncOperations.AsNoTracking()
                .Where(x => x.TenantId == tenantId)
                .Select(x => (long?)x.Sequence)
                .MaxAsync(cancellationToken) ?? 0;
            var pendingCounts = await (
                from presence in db.DevicePresences.AsNoTracking()
                join membership in db.TenantMemberships.AsNoTracking()
                    on new { presence.TenantId, UserId = presence.ActorId }
                    equals new { membership.TenantId, membership.UserId }
                where presence.TenantId == tenantId && membership.IsActive
                select new
                {
                    presence.DeviceId,
                    Count = db.SyncOperations.AsNoTracking().LongCount(operation =>
                        operation.TenantId == tenantId
                        && operation.Sequence > (presence.BranchId.HasValue
                            ? db.SyncDeviceBranchCursors.AsNoTracking()
                                .Where(cursor => cursor.TenantId == tenantId && cursor.DeviceId == presence.DeviceId
                                    && cursor.BranchId == presence.BranchId)
                                .Select(cursor => (long?)cursor.LastAcknowledgedSequence).FirstOrDefault() ?? 0
                            : db.SyncDeviceCursors.AsNoTracking()
                                .Where(cursor => cursor.TenantId == tenantId && cursor.DeviceId == presence.DeviceId)
                                .Select(cursor => (long?)cursor.LastAcknowledgedSequence).FirstOrDefault() ?? 0)
                        && (presence.BranchId.HasValue
                            ? operation.BranchId == presence.BranchId
                                || ((membership.Role == TenantRole.Owner || membership.Role == TenantRole.Manager)
                                    && operation.BranchId == null)
                            : ((membership.Role == TenantRole.Owner || membership.Role == TenantRole.Manager)
                                && operation.BranchId == null)))
                })
                .ToDictionaryAsync(x => x.DeviceId, x => x.Count, cancellationToken);

            var inventory = new List<object>(devices.Count);
            foreach (var device in devices.OrderByDescending(device => device.LastSeenAtUtc))
            {
                var lastAcknowledgedSequence = device.BranchId is Guid activeBranchId
                    ? branchAcknowledgedSequences.GetValueOrDefault((device.DeviceId, activeBranchId), 0)
                    : acknowledgedSequences.GetValueOrDefault(device.DeviceId, 0);
                // Branch-scoped acknowledgements are independent; switching branches cannot hide older unseen rows.
                var pendingOperationCount = pendingCounts.GetValueOrDefault(device.DeviceId, 0);

                inventory.Add(new
                {
                    device.DeviceId, device.ActorId, device.BranchId, device.DeviceName,
                    device.Platform, device.ClientVersion, device.FirstSeenAtUtc, device.LastSeenAtUtc,
                    lastAcknowledgedSequence,
                    latestJournalSequence,
                    pendingOperationCount,
                    isOnline = device.LastSeenAtUtc >= cutoff,
                    alertCode = device.LastSeenAtUtc >= cutoff ? null : "DEVICE_HEARTBEAT_MISSED"
                });
            }

            return Results.Ok(inventory);
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
            if (request.Operations.Any(item => item is null))
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["operations"] = ["Null operation entries are not allowed."]
                });

            // No signed authority-lease service is deployed yet. Fail closed rather than
            // trusting a client-supplied non-zero fencing epoch.
            if (request.Operations.Any(item => item.AuthorityEpoch != 0))
                return Results.Conflict(new
                {
                    code = "AUTHORITY_LEASE_NOT_CONFIGURED",
                    message = "Only the initial authority epoch is accepted until signed lease and fencing validation are enabled."
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

                try
                {
                    using var payloadDocument = JsonDocument.Parse(item.PayloadJson);
                    if (payloadDocument.RootElement.ValueKind != JsonValueKind.Object)
                        return Results.ValidationProblem(new Dictionary<string, string[]>
                        {
                            ["payloadJson"] = ["Payload must be a JSON object."]
                        });
                }
                catch (JsonException)
                {
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["payloadJson"] = ["Payload must contain valid JSON."]
                    });
                }

                if (!string.IsNullOrWhiteSpace(item.PayloadSha256)
                    && (item.PayloadSha256.Length != 64 || !item.PayloadSha256.All(Uri.IsHexDigit)))
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["payloadSha256"] = ["Payload hash must be a 64-character hexadecimal SHA-256 value."]
                    });

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

            // A client cannot invent or impersonate a device ID. Every writer must
            // first enroll through the authenticated heartbeat endpoint.
            var deviceIds = prepared.Select(x => x.Request.DeviceId).Distinct().ToArray();
            var enrolledDevices = await db.DevicePresences.AsNoTracking()
                .Where(x => x.TenantId == tenantId && deviceIds.Contains(x.DeviceId))
                .ToDictionaryAsync(x => x.DeviceId, cancellationToken);
            if (enrolledDevices.Count != deviceIds.Length
                || enrolledDevices.Values.Any(device => device.ActorId != actorId))
                return Results.Forbid();

            // Branch-scoped members may only publish operations for their selected
            // branch (or tenant-wide operations with no branch). Never trust a
            // client-supplied branch belonging to another active context.
            if (tenant.BranchId is Guid activeBranchId
                && prepared.Any(item => item.Request.BranchId.HasValue
                    && item.Request.BranchId != activeBranchId))
                return Results.Forbid();

            // A member without an active branch cannot publish into arbitrary
            // branches. Only owners/managers may operate across branches from the
            // tenant-wide context.
            if (tenant.BranchId is null && tenant.Role is not ("Owner" or "Manager")
                && prepared.Any(item => item.Request.BranchId.HasValue))
                return Results.Forbid();

            // Tenant-wide journal entries affect every branch and are reserved for
            // owners/managers even when they have selected the tenant-wide context.
            if (tenant.Role is not ("Owner" or "Manager")
                && prepared.Any(item => item.Request.BranchId is null))
                return Results.Forbid();

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

                if (item.Request.OperationType == "customer.profile.created")
                {
                    if (item.Request.SchemaVersion != 1 || item.Request.BranchId is null)
                        return Results.ValidationProblem(new Dictionary<string, string[]>
                        {
                            ["customer"] = ["Customer creation requires schema version 1 and an explicit originating branch."]
                        });

                    CustomerCreatedCommand? command;
                    try
                    {
                        command = JsonSerializer.Deserialize<CustomerCreatedCommand>(
                            item.Request.PayloadJson, new JsonSerializerOptions(JsonSerializerDefaults.Web));
                    }
                    catch (JsonException)
                    {
                        return Results.ValidationProblem(new Dictionary<string, string[]>
                        {
                            ["customer"] = ["Customer creation payload is invalid."]
                        });
                    }

                    if (command is null || command.CustomerId == Guid.Empty || command.BaseVersion != 0)
                        return Results.ValidationProblem(new Dictionary<string, string[]>
                        {
                            ["customer"] = ["Customer creation requires a valid ID and baseVersion 0."]
                        });

                    try
                    {
                        db.Customers.Add(new Customer(command.CustomerId, tenantId, command.Code, command.Name,
                            command.Phone, command.Email, command.Address, command.Notes));
                    }
                    catch (ArgumentException exception)
                    {
                        return Results.ValidationProblem(new Dictionary<string, string[]>
                        {
                            ["customer"] = [exception.Message]
                        });
                    }
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
                // A concurrent request may have committed the same idempotent operations first.
                // Detach failed inserts, reload the winners, and acknowledge only exact matches.
                foreach (var operation in inserted)
                    db.Entry(operation).State = EntityState.Detached;

                var concurrent = await db.SyncOperations.AsNoTracking()
                    .Where(x => x.TenantId == tenantId && operationIds.Contains(x.OperationId))
                    .ToDictionaryAsync(x => x.OperationId, cancellationToken);

                foreach (var item in prepared)
                {
                    if (!concurrent.TryGetValue(item.Request.OperationId, out var prior))
                        return Results.Conflict(new { operationId = item.Request.OperationId, message = "The batch conflicted with another write. Retry with the same operation IDs." });

                    if (!string.Equals(prior.PayloadSha256, item.Hash, StringComparison.Ordinal)
                        || prior.ActorId != actorId
                        || prior.DeviceId != item.Request.DeviceId
                        || prior.BranchId != item.Request.BranchId
                        || prior.OperationType != item.Request.OperationType
                        || prior.SchemaVersion != item.Request.SchemaVersion
                        || prior.AuthorityEpoch != item.Request.AuthorityEpoch
                        || prior.OccurredAtUtc != item.Request.OccurredAtUtc)
                        return Results.Conflict(new { operationId = item.Request.OperationId, message = "Operation ID was concurrently committed with different content or ownership." });

                    acknowledgements[item.Request.OperationId] =
                        new SyncOperationAck(prior.OperationId, prior.Sequence, true);
                }

                var retried = prepared.Select(x => acknowledgements[x.Request.OperationId]).ToArray();
                return Results.Accepted("/api/sync/operations", new { accepted = retried });
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
            // Scan the tenant journal in sequence order, but expose only operations
            // visible to the active branch. Advance over hidden rows as well so a
            // branch-scoped device cannot get stuck behind another branch's entries.
            var journalWindow = await db.SyncOperations.AsNoTracking()
                .Where(x => x.TenantId == tenantId && x.Sequence > afterSequence)
                .OrderBy(x => x.Sequence)
                .Take(take)
                .ToListAsync(cancellationToken);

            var canReadTenantWide = tenant.Role is "Owner" or "Manager";
            var operations = journalWindow
                .Where(x => (tenant.BranchId.HasValue && x.BranchId == tenant.BranchId)
                    || (canReadTenantWide && x.BranchId is null))
                .Select(x => new
                {
                    x.Sequence, x.OperationId, x.ActorId, x.DeviceId, x.BranchId,
                    x.OperationType, x.SchemaVersion, x.AuthorityEpoch,
                    x.OccurredAtUtc, x.ReceivedAtUtc, x.PayloadJson, x.PayloadSha256
                })
                .ToList();

            return Results.Ok(new
            {
                operations,
                nextSequence = journalWindow.Count == 0 ? afterSequence : journalWindow[^1].Sequence,
                hasMore = journalWindow.Count == take
            });
        });

        return endpoints;
    }

    public sealed record SyncOperationAck(Guid OperationId, long Sequence, bool Duplicate);
    public sealed record CustomerCreatedCommand(
        Guid CustomerId, string Code, string Name, string? Phone, string? Email,
        string? Address, string? Notes, int BaseVersion);

    public sealed record SyncAcknowledgementRequest(Guid DeviceId, long Sequence, Guid? BranchId = null);
    public sealed record DeviceHeartbeatRequest(Guid DeviceId, string? DeviceName, string? Platform, string? ClientVersion);
    public sealed record PushOperationsRequest(List<PushOperationRequest> Operations);
    public sealed record PushOperationRequest(
        Guid OperationId, Guid DeviceId, Guid? BranchId, string OperationType,
        int SchemaVersion, long AuthorityEpoch, DateTimeOffset OccurredAtUtc,
        string PayloadJson, string? PayloadSha256);
}
