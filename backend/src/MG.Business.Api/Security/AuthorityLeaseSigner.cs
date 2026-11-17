using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using MG.Business.Domain.Entities;

namespace MG.Business.Api.Security;

public sealed class AuthorityLeaseSigner
{
    private readonly byte[]? _privateKeyPkcs8;

    public AuthorityLeaseSigner(IConfiguration configuration)
    {
        var configured = configuration["SyncSecurity:AuthoritySigningPrivateKeyPkcs8Base64"];
        if (string.IsNullOrWhiteSpace(configured))
            return;

        try
        {
            _privateKeyPkcs8 = Convert.FromBase64String(configured);
            using var key = ECDsa.Create();
            key.ImportPkcs8PrivateKey(_privateKeyPkcs8, out var bytesRead);
            if (bytesRead != _privateKeyPkcs8.Length || key.KeySize != 256
                || key.ExportParameters(false).Curve.Oid.Value != "1.2.840.10045.3.1.7")
                throw new InvalidOperationException("Authority signing key must be a valid ECDSA P-256 PKCS#8 key.");
            PublicKeySpkiBase64 = Convert.ToBase64String(key.ExportSubjectPublicKeyInfo());
        }
        catch (FormatException exception)
        {
            throw new InvalidOperationException("SyncSecurity:AuthoritySigningPrivateKeyPkcs8Base64 must be valid Base64.", exception);
        }
        catch (CryptographicException exception)
        {
            throw new InvalidOperationException("The configured authority signing key is not a valid ECDSA P-256 PKCS#8 key.", exception);
        }
    }

    public bool IsConfigured => _privateKeyPkcs8 is { Length: > 0 };
    public string? PublicKeySpkiBase64 { get; }

    public string Sign(DeviceAuthorityLease lease)
    {
        if (_privateKeyPkcs8 is not { Length: > 0 })
            throw new InvalidOperationException("Signed authority lease issuance is not configured.");
        using var key = ECDsa.Create();
        key.ImportPkcs8PrivateKey(_privateKeyPkcs8, out _);
        var signature = key.SignData(
            Encoding.UTF8.GetBytes(Canonicalize(lease)),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        return Convert.ToBase64String(signature);
    }

    public static string Canonicalize(DeviceAuthorityLease lease)
        => string.Join("\n",
            "MG-AUTHORITY-LEASE-V1",
            lease.TenantId.ToString("D"),
            lease.BranchId?.ToString("D") ?? string.Empty,
            lease.ScopeKey,
            lease.DeviceId.ToString("D"),
            lease.LeaseId.ToString("D"),
            lease.Epoch.ToString(CultureInfo.InvariantCulture),
            lease.IssuedAtUtc.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture),
            lease.ExpiresAtUtc.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture));
}
