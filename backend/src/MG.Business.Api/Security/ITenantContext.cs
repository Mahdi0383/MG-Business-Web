namespace MG.Business.Api.Security;

public interface ITenantContext
{
    Guid? TenantId { get; }
    Guid? BranchId { get; }
    Guid? UserId { get; }
    string? Role { get; }
    bool IsResolved { get; }
}

public sealed class TenantContext : ITenantContext
{
    public Guid? TenantId { get; internal set; }
    public Guid? BranchId { get; internal set; }
    public Guid? UserId { get; internal set; }
    public string? Role { get; internal set; }
    public bool IsResolved => TenantId.HasValue && UserId.HasValue;
}
