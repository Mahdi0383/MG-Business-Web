using MG.Business.Domain.Entities;

namespace MG.Business.Domain.Tests;

public sealed class ProductDomainTests
{
    [Fact]
    public void ProductStoresDefaultSalePriceWithCurrencyPrecision()
    {
        var product = new Product(Guid.NewGuid(), "coffee-1", "Coffee", "cup", false, 125.50m);

        Assert.Equal("COFFEE-1", product.Sku);
        Assert.Equal(125.50m, product.DefaultSalePrice);
        Assert.False(product.TracksInventory);
    }

    [Fact]
    public void ProductRejectsNegativeOrOverPrecisionSalePrice()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            new Product(Guid.NewGuid(), "coffee-1", "Coffee", "cup", false, -1m));
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            new Product(Guid.NewGuid(), "coffee-1", "Coffee", "cup", false, 1.001m));
    }
}
