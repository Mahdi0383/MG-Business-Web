using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Entities;

public sealed class TenantMembership
{
    private TenantMembership() { }

    public TenantMembership(Guid tenantId, Guid userId, TenantRole role, Guid? branchId = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (userId == Guid.Empty) throw new ArgumentException("User is required.", nameof(userId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch ID cannot be empty.", nameof(branchId));
        if (!Enum.IsDefined(role)) throw new ArgumentOutOfRangeException(nameof(role));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        UserId = userId;
        BranchId = branchId;
        Role = role;
        IsActive = true;
        CreatedAtUtc = DateTimeOffset.UtcNow;
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid UserId { get; private set; }
    public Guid? BranchId { get; private set; }
    public TenantRole Role { get; private set; }
    public bool IsActive { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }

    public void Deactivate() => IsActive = false;
    public void Reactivate() => IsActive = true;

    public void ChangeRole(TenantRole role)
    {
        if (!Enum.IsDefined(role)) throw new ArgumentOutOfRangeException(nameof(role));
        Role = role;
    }

    public void AssignBranch(Guid? branchId)
    {
        if (branchId == Guid.Empty) throw new ArgumentException("Branch ID cannot be empty.", nameof(branchId));
        BranchId = branchId;
    }
}
