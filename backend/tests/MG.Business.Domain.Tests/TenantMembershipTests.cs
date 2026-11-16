using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class TenantMembershipTests
{
    [Fact]
    public void Membership_RequiresTenantAndUser_AndStartsActive()
    {
        var membership = new TenantMembership(Guid.NewGuid(), Guid.NewGuid(), TenantRole.Cashier);

        Assert.True(membership.IsActive);
        Assert.Equal(TenantRole.Cashier, membership.Role);
        Assert.Null(membership.BranchId);
        Assert.Throws<ArgumentException>(() => new TenantMembership(Guid.Empty, Guid.NewGuid(), TenantRole.Owner));
        Assert.Throws<ArgumentException>(() => new TenantMembership(Guid.NewGuid(), Guid.Empty, TenantRole.Owner));
    }

    [Fact]
    public void Membership_CanBeDeactivatedAndReactivated()
    {
        var membership = new TenantMembership(Guid.NewGuid(), Guid.NewGuid(), TenantRole.Manager);

        membership.Deactivate();
        Assert.False(membership.IsActive);

        membership.Reactivate();
        Assert.True(membership.IsActive);
    }

    [Fact]
    public void Membership_RejectsEmptyBranchId()
    {
        Assert.Throws<ArgumentException>(() =>
            new TenantMembership(Guid.NewGuid(), Guid.NewGuid(), TenantRole.Cashier, Guid.Empty));
    }
}
