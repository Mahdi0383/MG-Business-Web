namespace MG.Business.Domain.Entities;

public sealed class JournalEntryLine
{
    private JournalEntryLine() { }

    public JournalEntryLine(Guid accountId, decimal debit, decimal credit, string? description = null)
    {
        if (accountId == Guid.Empty) throw new ArgumentException("Account is required.", nameof(accountId));
        if (debit < 0m || credit < 0m) throw new ArgumentOutOfRangeException(nameof(debit), "Amounts cannot be negative.");
        if ((debit > 0m) == (credit > 0m))
            throw new ArgumentException("A journal line must have either a debit or a credit amount, but not both.");
        if (decimal.Round(debit, 2) != debit || decimal.Round(credit, 2) != credit)
            throw new ArgumentException("Journal amounts support at most two decimal places.");

        AccountId = accountId;
        Debit = debit;
        Credit = credit;
        Description = string.IsNullOrWhiteSpace(description) ? null : description.Trim();
    }

    public Guid Id { get; private set; }
    public Guid JournalEntryId { get; private set; }
    public Guid AccountId { get; private set; }
    public decimal Debit { get; private set; }
    public decimal Credit { get; private set; }
    public string? Description { get; private set; }
}
