using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class BranchAndMembershipTests
{
    [Fact]
    public void BranchCanBeUpdatedAndDisabled()
    {
        var branch = new Branch(Guid.NewGuid(), "Main", "main");
        branch.UpdateDetails("Central", "central");
        branch.SetActive(false);

        Assert.Equal("Central", branch.Name);
        Assert.Equal("CENTRAL", branch.Code);
        Assert.False(branch.IsActive);
    }

    [Fact]
    public void MembershipCanChangeRoleAndBranchScope()
    {
        var membership = new TenantMembership(Guid.NewGuid(), Guid.NewGuid(), TenantRole.Cashier);
        var branchId = Guid.NewGuid();

        membership.ChangeRole(TenantRole.InventoryManager);
        membership.AssignBranch(branchId);

        Assert.Equal(TenantRole.InventoryManager, membership.Role);
        Assert.Equal(branchId, membership.BranchId);
    }
}
