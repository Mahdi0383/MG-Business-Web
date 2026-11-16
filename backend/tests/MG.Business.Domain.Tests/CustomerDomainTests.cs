using MG.Business.Domain.Entities;

namespace MG.Business.Domain.Tests;

public sealed class CustomerDomainTests
{
    [Fact]
    public void CustomerNormalizesContactDetailsAndCode()
    {
        var customer = new Customer(Guid.NewGuid(), " c-100 ", "  Mahdi  ", " 09120000000 ", " USER@EXAMPLE.COM ");

        Assert.Equal("C-100", customer.Code);
        Assert.Equal("Mahdi", customer.Name);
        Assert.Equal("09120000000", customer.Phone);
        Assert.Equal("user@example.com", customer.Email);
    }

    [Fact]
    public void CustomerRequiresTenantCodeAndName()
    {
        Assert.Throws<ArgumentException>(() => new Customer(Guid.Empty, "C1", "Customer"));
        Assert.Throws<ArgumentException>(() => new Customer(Guid.NewGuid(), "", "Customer"));
        Assert.Throws<ArgumentException>(() => new Customer(Guid.NewGuid(), "C1", ""));
    }
}
