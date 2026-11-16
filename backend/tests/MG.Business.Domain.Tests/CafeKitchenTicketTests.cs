using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class CafeKitchenTicketTests
{
    [Fact]
    public void TicketFollowsTheKitchenWorkflow()
    {
        var ticket = new CafeKitchenTicket(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), CafeServiceType.DineIn, "T4");

        ticket.TransitionTo(KitchenTicketStatus.Preparing);
        Assert.NotNull(ticket.PreparingAtUtc);
        ticket.TransitionTo(KitchenTicketStatus.Ready);
        Assert.NotNull(ticket.ReadyAtUtc);
        ticket.TransitionTo(KitchenTicketStatus.Completed);
        Assert.NotNull(ticket.CompletedAtUtc);
        Assert.Equal(KitchenTicketStatus.Completed, ticket.Status);
    }

    [Fact]
    public void TicketRejectsInvalidStateTransitions()
    {
        var ticket = new CafeKitchenTicket(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), CafeServiceType.Takeaway);

        Assert.Throws<InvalidOperationException>(() => ticket.TransitionTo(KitchenTicketStatus.Ready));
        ticket.TransitionTo(KitchenTicketStatus.Cancelled);
        Assert.Throws<InvalidOperationException>(() => ticket.TransitionTo(KitchenTicketStatus.Preparing));
    }
}
