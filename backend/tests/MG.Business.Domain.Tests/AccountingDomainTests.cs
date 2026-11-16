using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;

namespace MG.Business.Domain.Tests;

public sealed class AccountingDomainTests
{
    [Fact]
    public void Account_RequiresTenantAndNormalizesText()
    {
        var account = new Account(Guid.NewGuid(), " 1001 ", " Cash ", AccountType.Asset);

        Assert.Equal("1001", account.Code);
        Assert.Equal("Cash", account.Name);
        Assert.True(account.IsActive);
        Assert.Throws<ArgumentException>(() => new Account(Guid.Empty, "1001", "Cash", AccountType.Asset));
    }

    [Fact]
    public void JournalEntry_AcceptsBalancedLines()
    {
        var entry = new JournalEntry(
            Guid.NewGuid(), Guid.NewGuid(), new DateOnly(2026, 11, 16), "Cash sale",
            [
                new JournalEntryLine(Guid.NewGuid(), 125m, 0m),
                new JournalEntryLine(Guid.NewGuid(), 0m, 125m)
            ]);

        Assert.Equal(125m, entry.TotalDebit);
        Assert.Equal(entry.TotalDebit, entry.TotalCredit);
        Assert.Equal(2, entry.Lines.Count);
    }

    [Fact]
    public void JournalEntry_RejectsUnbalancedLines()
    {
        Assert.Throws<ArgumentException>(() => new JournalEntry(
            Guid.NewGuid(), Guid.NewGuid(), new DateOnly(2026, 11, 16), "Unbalanced",
            [
                new JournalEntryLine(Guid.NewGuid(), 100m, 0m),
                new JournalEntryLine(Guid.NewGuid(), 0m, 99m)
            ]));
    }

    [Fact]
    public void JournalLine_RejectsDebitAndCreditOnSameLine()
    {
        Assert.Throws<ArgumentException>(() => new JournalEntryLine(Guid.NewGuid(), 10m, 10m));
    }

    [Fact]
    public void JournalLine_RejectsFractionalPrecisionBeyondTwoDecimals()
    {
        Assert.Throws<ArgumentException>(() => new JournalEntryLine(Guid.NewGuid(), 1.001m, 0m));
    }
}
