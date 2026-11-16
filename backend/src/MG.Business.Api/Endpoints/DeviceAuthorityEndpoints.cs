using System.Data;
using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class DeviceAuthorityEndpoints
{
    private static readonly TimeSpan LeaseLifetime = TimeSpan.FromMinutes(2);

    public static IEndpointRouteBuilder MapDeviceAuthorityEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapGet("/api/sync/authority-lease", async (
            ITenantContext tenant,
            BusinessDbContext db,
            AuthorityLeaseSigner signer,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            if (!signer.IsConfigured || string.IsNullOrWhiteSpace(signer.PublicKeySpkiBase64))
                return Results.Problem(
                    title: "Authority signing is not configured",
                    statusCode: StatusCodes.Status503ServiceUnavailable);
            var scopeKey = tenant.BranchId?.ToString("N") ?? "tenant-wide";
            var lease = await db.DeviceAuthorityLeases.AsNoTracking().SingleOrDefaultAsync(
                x => x.TenantId == tenantId && x.ScopeKey == scopeKey,
                cancellationToken);
            if (lease is null || lease.RevokedAtUtc is not null || string.IsNullOrWhiteSpace(lease.SignatureBase64))
                return Results.NotFound(new { code = "AUTHORITY_LEASE_NOT_FOUND" });
            return Results.Ok(new
            {
                leaseId = lease.LeaseId,
                tenantId = lease.TenantId,
                branchId = lease.BranchId,
                scopeKey = lease.ScopeKey,
                deviceId = lease.DeviceId,
                epoch = lease.Epoch,
                issuedAtUtc = lease.IssuedAtUtc,
                expiresAtUtc = lease.ExpiresAtUtc,
                signatureBase64 = lease.SignatureBase64,
                publicKeySpkiBase64 = signer.PublicKeySpkiBase64
            });
        }).RequireAuthorization("TenantMember");

        endpoints.MapPost("/api/sync/authority-lease", async (
            HttpContext httpContext,
            ITenantContext tenant,
            BusinessDbContext db,
            AuthorityLeaseSigner signer,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid
                || tenant.Role is not ("Owner" or "Manager"))
                return Results.Forbid();
            if (httpContext.Items[DeviceRequestSignatureMiddleware.DeviceIdItemKey] is not Guid deviceId)
                return Results.Unauthorized();
            if (!signer.IsConfigured || string.IsNullOrWhiteSpace(signer.PublicKeySpkiBase64))
                return Results.Problem(
                    title: "Authority signing is not configured",
                    statusCode: StatusCodes.Status503ServiceUnavailable);

            var branchId = tenant.BranchId;
            var scopeKey = branchId?.ToString("N") ?? "tenant-wide";
            var now = DateTimeOffset.UtcNow;
            var expiresAt = now.Add(LeaseLifetime);
            await using var transaction = await db.Database.BeginTransactionAsync(
                IsolationLevel.Serializable, cancellationToken);

            var lease = await db.DeviceAuthorityLeases.SingleOrDefaultAsync(
                x => x.TenantId == tenantId && x.ScopeKey == scopeKey,
                cancellationToken);
            var created = lease is null;
            if (created)
            {
                lease = new DeviceAuthorityLease(
                    Guid.NewGuid(), tenantId, scopeKey, branchId, deviceId, now, expiresAt);
                db.DeviceAuthorityLeases.Add(lease);
            }
            else if (!lease!.TryAcquire(deviceId, now, expiresAt))
            {
                return Results.Conflict(new
                {
                    code = "AUTHORITY_LEASE_HELD",
                    message = "Another device currently holds the authority lease for this scope.",
                    holderDeviceId = lease.DeviceId,
                    epoch = lease.Epoch,
                    expiresAtUtc = lease.ExpiresAtUtc
                });
            }

            lease!.SetSignature(signer.Sign(lease));
            try
            {
                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                db.ChangeTracker.Clear();
                return Results.Conflict(new
                {
                    code = "AUTHORITY_LEASE_RACE",
                    message = "Authority changed concurrently. Retry after refreshing device state."
                });
            }
            catch (DbException)
            {
                db.ChangeTracker.Clear();
                return Results.Conflict(new
                {
                    code = "AUTHORITY_LEASE_RACE",
                    message = "Authority changed concurrently. Retry after refreshing device state."
                });
            }

            return Results.Ok(new
            {
                leaseId = lease.LeaseId,
                tenantId = lease.TenantId,
                branchId = lease.BranchId,
                scopeKey = lease.ScopeKey,
                deviceId = lease.DeviceId,
                epoch = lease.Epoch,
                issuedAtUtc = lease.IssuedAtUtc,
                expiresAtUtc = lease.ExpiresAtUtc,
                signatureBase64 = lease.SignatureBase64,
                publicKeySpkiBase64 = signer.PublicKeySpkiBase64
            });
        }).RequireAuthorization("TenantMember").RequireRateLimiting("auth");

        return endpoints;
    }
}
