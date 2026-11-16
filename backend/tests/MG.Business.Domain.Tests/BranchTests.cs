using MG.Business.Domain.Entities;

namespace MG.Business.Domain.Tests;

public sealed class BranchTests
{
    [Fact]
    public void Constructor_BindsBranchToTenant_AndNormalizesCode()
    {
        var tenantId = Guid.NewGuid();
        var branch = new Branch(tenantId, "مشهد مرکزی", "  mhd-01 ");

        Assert.Equal(tenantId, branch.TenantId);
        Assert.Equal("مشهد مرکزی", branch.Name);
        Assert.Equal("MHD-01", branch.Code);
        Assert.True(branch.IsActive);
    }

    [Fact]
    public void Constructor_RejectsEmptyTenantId()
    {
        Assert.Throws<ArgumentException>(() => new Branch(Guid.Empty, "Main", "MAIN"));
    }
}