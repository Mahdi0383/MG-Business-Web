namespace MG.Business.Domain.Entities;

public sealed class JournalEntry
{
    private readonly List<JournalEntryLine> _lines = [];
    private JournalEntry() { }

    public JournalEntry(Guid tenantId, Guid branchId, DateOnly entryDate, string description,
        IEnumerable<JournalEntryLine> lines, string? reference = null)
    {
        if (tenantId == Guid.Empty) throw new ArgumentException("Tenant is required.", nameof(tenantId));
        if (branchId == Guid.Empty) throw new ArgumentException("Branch is required.", nameof(branchId));
        if (string.IsNullOrWhiteSpace(description)) throw new ArgumentException("Description is required.", nameof(description));
        ArgumentNullException.ThrowIfNull(lines);

        var materialized = lines.ToList();
        if (materialized.Count < 2) throw new ArgumentException("A journal entry requires at least two lines.", nameof(lines));
        if (materialized.Any(x => x is null)) throw new ArgumentException("Journal lines cannot contain null values.", nameof(lines));

        var debitTotal = materialized.Sum(x => x.Debit);
        var creditTotal = materialized.Sum(x => x.Credit);
        if (debitTotal <= 0m || debitTotal != creditTotal)
            throw new ArgumentException("Journal entry debits and credits must be equal and greater than zero.", nameof(lines));

        Id = Guid.NewGuid();
        TenantId = tenantId;
        BranchId = branchId;
        EntryDate = entryDate;
        Description = description.Trim();
        Reference = string.IsNullOrWhiteSpace(reference) ? null : reference.Trim();
        CreatedAtUtc = DateTimeOffset.UtcNow;
        _lines.AddRange(materialized);
    }

    public Guid Id { get; private set; }
    public Guid TenantId { get; private set; }
    public Guid BranchId { get; private set; }
    public DateOnly EntryDate { get; private set; }
    public string Description { get; private set; } = string.Empty;
    public string? Reference { get; private set; }
    public DateTimeOffset CreatedAtUtc { get; private set; }
    public IReadOnlyCollection<JournalEntryLine> Lines => _lines.AsReadOnly();
    public decimal TotalDebit => _lines.Sum(x => x.Debit);
    public decimal TotalCredit => _lines.Sum(x => x.Credit);
}
