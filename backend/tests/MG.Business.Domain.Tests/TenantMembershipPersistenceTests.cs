using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Identity;
using MG.Business.Infrastructure.Persistence;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Domain.Tests;

public sealed class TenantMembershipPersistenceTests
{
    [Fact]
    public async Task Sqlite_PersistsMembershipAgainstAnIdentityUser()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenant = new Tenant("MG Store", "mg-store");
        var user = NewUser("owner@mg.test");
        var membership = new TenantMembership(tenant.Id, user.Id, TenantRole.Owner);

        db.AddRange(tenant, user, membership);
        await db.SaveChangesAsync();

        var persisted = await db.TenantMemberships.SingleAsync();
        Assert.Equal(user.Id, persisted.UserId);
        Assert.Equal(TenantRole.Owner, persisted.Role);
        Assert.True(persisted.IsActive);
    }

    [Fact]
    public async Task Sqlite_RejectsMembershipForAnotherTenantsBranch()
    {
        await using var connection = new SqliteConnection("Data Source=:memory:");
        await connection.OpenAsync();
        var options = new DbContextOptionsBuilder<BusinessDbContext>().UseSqlite(connection).Options;
        await using var db = new BusinessDbContext(options);
        await db.Database.EnsureCreatedAsync();

        var tenantA = new Tenant("Tenant A", "tenant-a");
        var tenantB = new Tenant("Tenant B", "tenant-b");
        var user = NewUser("staff@mg.test");
        var branchB = new Branch(tenantB.Id, "Tenant B Main", "MAIN");
        var invalidMembership = new TenantMembership(tenantA.Id, user.Id, TenantRole.Cashier, branchB.Id);

        db.AddRange(tenantA, tenantB, user, branchB, invalidMembership);
        await Assert.ThrowsAsync<DbUpdateException>(() => db.SaveChangesAsync());
    }

    private static ApplicationUser NewUser(string email) => new()
    {
        Id = Guid.NewGuid(),
        UserName = email,
        NormalizedUserName = email.ToUpperInvariant(),
        Email = email,
        NormalizedEmail = email.ToUpperInvariant(),
        DisplayName = "MG Test User",
        EmailConfirmed = true
    };
}
