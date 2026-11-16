using MG.Business.Domain.Entities;

namespace MG.Business.Domain.Tests;

public sealed class TenantTests
{
    [Fact]
    public void Constructor_NormalizesSlug_AndCreatesActiveTenant()
    {
        var tenant = new Tenant("  MG Cafe  ", "  MG-CAFE ");

        Assert.Equal("MG Cafe", tenant.Name);
        Assert.Equal("mg-cafe", tenant.Slug);
        Assert.True(tenant.IsActive);
        Assert.NotEqual(Guid.Empty, tenant.Id);
    }

    [Theory]
    [InlineData("", "mg")]
    [InlineData("   ", "mg")]
    [InlineData("MG", "")]
    [InlineData("MG", "   ")]
    public void Constructor_RejectsMissingRequiredValues(string name, string slug)
    {
        Assert.Throws<ArgumentException>(() => new Tenant(name, slug));
    }
}