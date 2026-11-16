using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using MG.Business.Api.Security;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Security;

public sealed class DeviceRequestSignatureMiddleware
{
    private const int MaxSignedBodyBytes = 1_100_000;
    private static readonly TimeSpan AllowedClockSkew = TimeSpan.FromMinutes(2);
    private readonly RequestDelegate _next;
    private readonly IConfiguration _configuration;
    private readonly ILogger<DeviceRequestSignatureMiddleware> _logger;

    public DeviceRequestSignatureMiddleware(
        RequestDelegate next,
        IConfiguration configuration,
        ILogger<DeviceRequestSignatureMiddleware> logger)
    {
        _next = next;
        _configuration = configuration;
        _logger = logger;
    }

    public async Task InvokeAsync(HttpContext context, ITenantContext tenant, BusinessDbContext db)
    {
        if (!_configuration.GetValue<bool>("SyncSecurity:RequireDeviceSignatures")
            || !context.Request.Path.StartsWithSegments("/api/sync")
            || context.Request.Path.StartsWithSegments("/api/sync/enrollment"))
        {
            await _next(context);
            return;
        }

        if (tenant.TenantId is not Guid tenantId || tenant.UserId is not Guid actorId
            || context.User.Identity?.IsAuthenticated != true)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_AUTH_REQUIRED");
            return;
        }

        if (!TryHeaderGuid(context, "X-MG-Device-Id", out var deviceId)
            || !TryHeaderInt(context, "X-MG-Device-Key-Version", out var keyVersion)
            || !TryHeaderLong(context, "X-MG-Device-Timestamp", out var timestamp)
            || !TryHeaderGuid(context, "X-MG-Device-Nonce", out var nonce)
            || !TryHeader(context, "X-MG-Device-Credential", out var credentialText)
            || !TryHeader(context, "X-MG-Device-Signature", out var signatureText))
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_HEADERS_REQUIRED");
            return;
        }

        var now = DateTimeOffset.UtcNow;
        DateTimeOffset signedAt;
        try { signedAt = DateTimeOffset.FromUnixTimeMilliseconds(timestamp); }
        catch (ArgumentOutOfRangeException)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_TIMESTAMP_INVALID");
            return;
        }

        if ((now - signedAt).Duration() > AllowedClockSkew)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_EXPIRED");
            return;
        }

        var credential = await db.DeviceCredentials.SingleOrDefaultAsync(
            x => x.TenantId == tenantId && x.DeviceId == deviceId, context.RequestAborted);
        if (credential is null || !credential.IsActive || credential.ActorId != actorId
            || (credential.BranchId is Guid credentialBranch && credentialBranch != tenant.BranchId)
            || credential.KeyVersion != keyVersion)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_CREDENTIAL_INVALID");
            return;
        }

        byte[] credentialBytes;
        byte[] signatureBytes;
        byte[] publicKeyBytes;
        try
        {
            credentialBytes = Convert.FromBase64String(credentialText);
            signatureBytes = Convert.FromBase64String(signatureText);
            publicKeyBytes = Convert.FromBase64String(credential.PublicKeySpkiBase64);
        }
        catch (FormatException)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_ENCODING_INVALID");
            return;
        }

        if (credentialBytes.Length != 32 || signatureBytes.Length != 64
            || !CryptographicOperations.FixedTimeEquals(
                SHA256.HashData(credentialBytes),
                Convert.FromHexString(credential.CredentialSha256)))
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_CREDENTIAL_INVALID");
            return;
        }

        context.Request.EnableBuffering();
        await using var bodyBuffer = new MemoryStream();
        var chunk = new byte[16 * 1024];
        int read;
        while ((read = await context.Request.Body.ReadAsync(chunk, context.RequestAborted)) > 0)
        {
            if (bodyBuffer.Length + read > MaxSignedBodyBytes)
            {
                await Reject(context, StatusCodes.Status413PayloadTooLarge, "DEVICE_SIGNED_BODY_TOO_LARGE");
                return;
            }
            await bodyBuffer.WriteAsync(chunk.AsMemory(0, read), context.RequestAborted);
        }
        context.Request.Body.Position = 0;
        var bodyHash = Convert.ToHexString(SHA256.HashData(bodyBuffer.ToArray())).ToLowerInvariant();
        var branchText = tenant.BranchId?.ToString("D") ?? string.Empty;
        var canonical = string.Join("\n",
            "MG-DEVICE-REQUEST-V1",
            context.Request.Method.ToUpperInvariant(),
            $"{context.Request.PathBase}{context.Request.Path}{context.Request.QueryString}",
            tenantId.ToString("D"),
            branchText,
            actorId.ToString("D"),
            deviceId.ToString("D"),
            keyVersion.ToString(CultureInfo.InvariantCulture),
            timestamp.ToString(CultureInfo.InvariantCulture),
            nonce.ToString("D"),
            bodyHash);

        using var publicKey = ECDsa.Create();
        try
        {
            publicKey.ImportSubjectPublicKeyInfo(publicKeyBytes, out var bytesRead);
            if (bytesRead != publicKeyBytes.Length || publicKey.KeySize != 256
                || publicKey.ExportParameters(false).Curve.Oid.Value != "1.2.840.10045.3.1.7"
                || !publicKey.VerifyData(Encoding.UTF8.GetBytes(canonical), signatureBytes,
                    HashAlgorithmName.SHA256, DSASignatureFormat.IeeeP1363FixedFieldConcatenation))
            {
                await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_INVALID");
                return;
            }
        }
        catch (CryptographicException)
        {
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_PUBLIC_KEY_INVALID");
            return;
        }

        try
        {
            // Retain replay nonces only for their acceptance window. Cleanup is
            // scoped to the active device so idle devices need no background job.
            var deviceNonces = await db.DeviceRequestNonces
                .Where(x => x.TenantId == tenantId && x.DeviceId == deviceId)
                .ToListAsync(context.RequestAborted);
            var nonceValue = nonce.ToString("D");
            if (deviceNonces.Any(x => x.Nonce == nonceValue && x.ExpiresAtUtc > now))
            {
                await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_REPLAYED");
                return;
            }
            db.DeviceRequestNonces.RemoveRange(deviceNonces.Where(x => x.ExpiresAtUtc <= now));
            db.DeviceRequestNonces.Add(new(
                tenantId, deviceId, nonceValue, now, now.AddMinutes(5)));
            credential.RecordUse(now);
            await db.SaveChangesAsync(context.RequestAborted);
        }
        catch (DbUpdateException)
        {
            db.ChangeTracker.Clear();
            await Reject(context, StatusCodes.Status401Unauthorized, "DEVICE_SIGNATURE_REPLAYED");
            return;
        }

        await _next(context);
    }

    private static bool TryHeader(HttpContext context, string name, out string value)
    {
        value = context.Request.Headers[name].ToString();
        return !string.IsNullOrWhiteSpace(value) && value.Length <= 4096;
    }

    private static bool TryHeaderGuid(HttpContext context, string name, out Guid value)
        => Guid.TryParseExact(context.Request.Headers[name].ToString(), "D", out value);

    private static bool TryHeaderInt(HttpContext context, string name, out int value)
        => int.TryParse(context.Request.Headers[name].ToString(), NumberStyles.None,
            CultureInfo.InvariantCulture, out value) && value > 0;

    private static bool TryHeaderLong(HttpContext context, string name, out long value)
        => long.TryParse(context.Request.Headers[name].ToString(), NumberStyles.None,
            CultureInfo.InvariantCulture, out value) && value > 0;

    private static async Task Reject(HttpContext context, int status, string code)
    {
        context.Response.StatusCode = status;
        await context.Response.WriteAsJsonAsync(new { code, message = "Device request authentication failed." });
    }
}
