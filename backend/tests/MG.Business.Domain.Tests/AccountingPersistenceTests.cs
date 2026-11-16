using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Domain.Tests;

public sealed class AccountingPersistenceTests
{
    [Fact]
    public async Task Sqlite_CreatesAndPersistsTenantScopedJournal()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenant = new Tenant("MG Cafe", "mg-cafe");
        var branch = new Branch(tenant.Id, "Central", "MAIN");
        var cash = new Account(tenant.Id, "1001", "Cash", AccountType.Asset);
        var sales = new Account(tenant.Id, "4001", "Sales", AccountType.Revenue);
        var entry = new JournalEntry(tenant.Id, branch.Id, new DateOnly(2026, 11, 16), "Cash sale",
        [
            new JournalEntryLine(tenant.Id, cash.Id, 125m, 0m),
            new JournalEntryLine(tenant.Id, sales.Id, 0m, 125m)
        ]);

        db.AddRange(tenant, branch, cash, sales, entry);
        await db.SaveChangesAsync();

        var persisted = await db.JournalEntries.Include(x => x.Lines).SingleAsync();
        Assert.Equal(125m, persisted.TotalDebit);
        Assert.Equal(125m, persisted.TotalCredit);
        Assert.Equal(2, persisted.Lines.Count);
    }

    [Fact]
    public async Task Sqlite_RejectsJournalEntryLinkedToAnotherTenantsBranch()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenantA = new Tenant("Tenant A", "tenant-a");
        var tenantB = new Tenant("Tenant B", "tenant-b");
        var branchB = new Branch(tenantB.Id, "Tenant B Main", "MAIN");
        var accountA = new Account(tenantA.Id, "1001", "Cash", AccountType.Asset);
        var accountB = new Account(tenantA.Id, "4001", "Sales", AccountType.Revenue);
        var invalidEntry = new JournalEntry(tenantA.Id, branchB.Id, new DateOnly(2026, 11, 16), "Cross-tenant branch",
        [
            new JournalEntryLine(tenantA.Id, accountA.Id, 10m, 0m),
            new JournalEntryLine(tenantA.Id, accountB.Id, 0m, 10m)
        ]);

        db.AddRange(tenantA, tenantB, branchB, accountA, accountB, invalidEntry);
        await Assert.ThrowsAsync<DbUpdateException>(() => db.SaveChangesAsync());
    }
}
