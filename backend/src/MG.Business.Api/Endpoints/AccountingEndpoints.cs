using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class AccountingEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "Accountant"];

    public static IEndpointRouteBuilder MapAccountingEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/accounting").RequireAuthorization("TenantMember");

        group.MapGet("/accounts", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var accounts = await db.Accounts.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.IsActive)
                .OrderBy(x => x.Code)
                .Select(x => new AccountResponse(x.Id, x.Code, x.Name, x.Type.ToString(), x.IsActive))
                .ToListAsync(cancellationToken);
            return Results.Ok(accounts);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/accounts", async (CreateAccountRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            if (!Enum.IsDefined(request.Type))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["type"] = ["نوع حساب معتبر نیست."] });

            var account = new Account(tenantId, request.Code, request.Name, request.Type);
            db.Accounts.Add(account);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "کد حساب تکراری است یا اطلاعات با محدودیت پایگاه داده سازگار نیست." });
            }

            return Results.Created($"/api/accounting/accounts/{account.Id}",
                new AccountResponse(account.Id, account.Code, account.Name, account.Type.ToString(), account.IsActive));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapPost("/journal-entries", async (CreateJournalEntryRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا کسب‌وکار و شعبه فعال را انتخاب کنید." });
            if (request.Lines is null || request.Lines.Count < 2)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["سند باید حداقل دو ردیف داشته باشد."] });

            var accountIds = request.Lines.Select(x => x.AccountId).Distinct().ToArray();
            var validAccountCount = await db.Accounts.CountAsync(x =>
                x.TenantId == tenantId && x.IsActive && accountIds.Contains(x.Id), cancellationToken);
            if (validAccountCount != accountIds.Length)
                return Results.BadRequest(new { message = "یک یا چند حساب نامعتبر یا متعلق به کسب‌وکار دیگری است." });

            JournalEntry entry;
            try
            {
                var lines = request.Lines.Select(line =>
                    new JournalEntryLine(tenantId, line.AccountId, line.Debit, line.Credit, line.Description));
                entry = new JournalEntry(tenantId, branchId, request.EntryDate, request.Description, lines, request.Reference);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["entry"] = [exception.Message] });
            }

            db.JournalEntries.Add(entry);
            await db.SaveChangesAsync(cancellationToken);

            return Results.Created($"/api/accounting/journal-entries/{entry.Id}", new
            {
                entry.Id,
                entry.EntryDate,
                entry.Description,
                entry.Reference,
                entry.TotalDebit,
                entry.TotalCredit,
                lineCount = entry.Lines.Count
            });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/trial-balance", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var entries = await db.JournalEntries.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId)
                .SelectMany(x => x.Lines)
                .Join(db.Accounts.AsNoTracking(), line => line.AccountId, account => account.Id,
                    (line, account) => new { line, account })
                .ToListAsync(cancellationToken);

            var balances = entries.GroupBy(x => new
            {
                x.account.Id,
                x.account.Code,
                x.account.Name,
                Type = x.account.Type.ToString()
            }).Select(grouping => new
            {
                grouping.Key.Id,
                grouping.Key.Code,
                grouping.Key.Name,
                grouping.Key.Type,
                Debit = grouping.Sum(x => x.line.Debit),
                Credit = grouping.Sum(x => x.line.Credit),
                NetDebit = Math.Max(0m, grouping.Sum(x => x.line.Debit - x.line.Credit)),
                NetCredit = Math.Max(0m, grouping.Sum(x => x.line.Credit - x.line.Debit))
            }).OrderBy(x => x.Code).ToList();

            return Results.Ok(new
            {
                accounts = balances,
                totalDebit = balances.Sum(x => x.Debit),
                totalCredit = balances.Sum(x => x.Credit),
                isBalanced = balances.Sum(x => x.Debit) == balances.Sum(x => x.Credit)
            });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        return endpoints;
    }

    public sealed record CreateAccountRequest(string Code, string Name, AccountType Type);
    public sealed record AccountResponse(Guid Id, string Code, string Name, string Type, bool IsActive);
    public sealed record CreateJournalEntryRequest(DateOnly EntryDate, string Description, string? Reference, List<JournalLineRequest> Lines);
    public sealed record JournalLineRequest(Guid AccountId, decimal Debit, decimal Credit, string? Description);
}
