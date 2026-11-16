using System.Security.Cryptography;
using System.Text;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class DeviceEnrollmentEndpoints
{
    private static readonly TimeSpan ChallengeLifetime = TimeSpan.FromMinutes(5);
    private const int MaxPublicKeyBytes = 1024;

    public static IEndpointRouteBuilder MapDeviceEnrollmentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/sync/enrollment")
            .RequireAuthorization("TenantMember")
            .RequireRateLimiting("auth");

        group.MapPost("/challenges", async (
            DeviceEnrollmentChallengeRequest request,
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

            var now = DateTimeOffset.UtcNow;
            var challengeBytes = RandomNumberGenerator.GetBytes(32);
            var challenge = Convert.ToBase64String(challengeBytes);
            var challengeId = Guid.NewGuid();
            var digest = Convert.ToHexString(SHA256.HashData(challengeBytes)).ToLowerInvariant();
            db.DeviceEnrollmentChallenges.Add(new DeviceEnrollmentChallenge(
                challengeId, tenantId, actorId, tenant.BranchId, request.DeviceId,
                digest, now, now.Add(ChallengeLifetime)));
            await db.SaveChangesAsync(cancellationToken);

            return Results.Ok(new
            {
                challengeId,
                challenge,
                tenantId,
                branchId = tenant.BranchId,
                actorId,
                deviceId = request.DeviceId,
                expiresAtUtc = now.Add(ChallengeLifetime),
                algorithm = "ECDSA-P256-SHA256"
            });
        });

        group.MapPost("/complete", async (
            DeviceEnrollmentCompleteRequest request,
            ITenantContext tenant,
            BusinessDbContext db,
            CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid actorId)
                return Results.Forbid();
            if (request.ChallengeId == Guid.Empty || request.DeviceId == Guid.Empty
                || string.IsNullOrWhiteSpace(request.Challenge) || request.Challenge.Length > 128
                || string.IsNullOrWhiteSpace(request.PublicKeySpkiBase64) || request.PublicKeySpkiBase64.Length > 2048
                || string.IsNullOrWhiteSpace(request.SignatureBase64) || request.SignatureBase64.Length > 256)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["enrollment"] = ["A valid challenge, device ID, public key, and signature are required."]
                });

            byte[] challengeBytes;
            byte[] publicKeyBytes;
            byte[] signatureBytes;
            try
            {
                challengeBytes = Convert.FromBase64String(request.Challenge);
                publicKeyBytes = Convert.FromBase64String(request.PublicKeySpkiBase64);
                signatureBytes = Convert.FromBase64String(request.SignatureBase64);
            }
            catch (FormatException)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["enrollment"] = ["Enrollment values must be valid Base64."]
                });
            }

            if (challengeBytes.Length != 32 || publicKeyBytes.Length is < 64 or > MaxPublicKeyBytes
                || signatureBytes.Length != 64)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["enrollment"] = ["Challenge, P-256 public key, or signature has an invalid size."]
                });

            using var publicKey = ECDsa.Create();
            try
            {
                publicKey.ImportSubjectPublicKeyInfo(publicKeyBytes, out var bytesRead);
                if (bytesRead != publicKeyBytes.Length || publicKey.KeySize != 256
                    || publicKey.ExportParameters(false).Curve.Oid.Value != "1.2.840.10045.3.1.7"
                    || !publicKey.ExportSubjectPublicKeyInfo().AsSpan().SequenceEqual(publicKeyBytes))
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["publicKeySpkiBase64"] = ["Only canonical ECDSA P-256 SubjectPublicKeyInfo keys are accepted."]
                    });
            }
            catch (CryptographicException)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["publicKeySpkiBase64"] = ["The public key is not a valid ECDSA P-256 key."]
                });
            }

            var now = DateTimeOffset.UtcNow;
            await using var transaction = await db.Database.BeginTransactionAsync(
                System.Data.IsolationLevel.Serializable, cancellationToken);

            var challengeRecord = await db.DeviceEnrollmentChallenges.SingleOrDefaultAsync(
                x => x.Id == request.ChallengeId && x.TenantId == tenantId
                    && x.ActorId == actorId && x.DeviceId == request.DeviceId,
                cancellationToken);
            if (challengeRecord is null)
                return Results.Forbid();

            var branchText = tenant.BranchId?.ToString("D") ?? string.Empty;
            var signedMessage = string.Join("\n",
                "MG-DEVICE-ENROLLMENT-V1",
                tenantId.ToString("D"),
                branchText,
                actorId.ToString("D"),
                request.DeviceId.ToString("D"),
                request.ChallengeId.ToString("D"),
                request.Challenge);
            if (!publicKey.VerifyData(Encoding.UTF8.GetBytes(signedMessage), signatureBytes,
                HashAlgorithmName.SHA256, DSASignatureFormat.IeeeP1363FixedFieldConcatenation))
                return Results.Unauthorized();

            var digest = Convert.ToHexString(SHA256.HashData(challengeBytes)).ToLowerInvariant();
            if (!challengeRecord.TryConsume(tenantId, actorId, tenant.BranchId,
                request.DeviceId, digest, now))
                return Results.Conflict(new { code = "ENROLLMENT_CHALLENGE_INVALID", message = "The enrollment challenge is expired, consumed, or bound to another context." });

            var existing = await db.DeviceCredentials.SingleOrDefaultAsync(
                x => x.TenantId == tenantId && x.DeviceId == request.DeviceId, cancellationToken);
            if (existing is not null
                && (!existing.IsActive || existing.ActorId != actorId
                    || !string.Equals(existing.PublicKeySpkiBase64, request.PublicKeySpkiBase64, StringComparison.Ordinal)))
                return Results.Conflict(new { code = "DEVICE_CREDENTIAL_EXISTS", message = "This device ID belongs to another key or actor, or has been revoked." });

            // A client persists its non-extractable key before completing enrollment.
            // If the response is lost after commit, the same proof-of-possession key
            // can safely rotate the one-time credential and recover without lockout.
            var credentialBytes = RandomNumberGenerator.GetBytes(32);
            var credential = Convert.ToBase64String(credentialBytes);
            var credentialDigest = Convert.ToHexString(SHA256.HashData(credentialBytes)).ToLowerInvariant();
            if (existing is not null)
            {
                existing.RotateCredential(credentialDigest, now);
                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);
                return Results.Ok(new
                {
                    deviceId = request.DeviceId,
                    keyVersion = existing.KeyVersion,
                    credential,
                    enrolledAtUtc = existing.CreatedAtUtc,
                    credentialStorage = "one-time-return; server-stores-sha256-digest",
                    recovered = true
                });
            }

            db.DeviceCredentials.Add(new DeviceCredential(
                tenantId, actorId, null, request.DeviceId, request.PublicKeySpkiBase64,
                credentialDigest, now));
            await db.SaveChangesAsync(cancellationToken);
            await transaction.CommitAsync(cancellationToken);

            return Results.Created("/api/sync/enrollment/status", new
            {
                deviceId = request.DeviceId,
                keyVersion = 1,
                credential,
                enrolledAtUtc = now,
                credentialStorage = "one-time-return; server-stores-sha256-digest",
                recovered = false
            });
        });

        return endpoints;
    }
}

public sealed record DeviceEnrollmentChallengeRequest(Guid DeviceId);
public sealed record DeviceEnrollmentCompleteRequest(
    Guid ChallengeId,
    Guid DeviceId,
    string Challenge,
    string PublicKeySpkiBase64,
    string SignatureBase64);
