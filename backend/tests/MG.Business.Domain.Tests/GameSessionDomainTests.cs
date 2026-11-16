using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class GameSessionDomainTests
{
    [Fact]
    public void SessionRoundsUpToBillingIntervalAndLinksSalesOrderOnce()
    {
        var start = DateTimeOffset.UtcNow.AddHours(-1);
        var session = new GameSession(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), null, 120m, 15, start);

        session.Close(start.AddMinutes(16));

        Assert.Equal(30, session.BilledMinutes);
        Assert.Equal(60m, session.TotalAmount);
        Assert.Equal(GameSessionStatus.Closed, session.Status);

        var orderId = Guid.NewGuid();
        session.LinkSalesOrder(orderId);
        Assert.Equal(orderId, session.SalesOrderId);
        Assert.Throws<ArgumentException>(() => session.LinkSalesOrder(Guid.NewGuid()));
    }

    [Fact]
    public void SessionRejectsInvalidCloseTimeAndDoubleClose()
    {
        var start = DateTimeOffset.UtcNow.AddMinutes(-10);
        var session = new GameSession(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), null, 60m, 15, start);

        Assert.Throws<ArgumentOutOfRangeException>(() => session.Close(start));
        session.Close(start.AddMinutes(10));
        Assert.Equal(15, session.BilledMinutes);
        Assert.Throws<InvalidOperationException>(() => session.Close(start.AddMinutes(20)));
    }
}
