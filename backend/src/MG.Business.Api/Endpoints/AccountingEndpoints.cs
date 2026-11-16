using System.Data;
using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class AccountingEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Viewer"];
    private static readonly string[] AccountReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"];
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
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(AccountReadRoles)));

        group.MapPost("/accounts/initialize-default-chart", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();

            var defaults = new (string Code, string Name, AccountType Type)[]
            {
                ("1000", "صندوق", AccountType.Asset),
                ("1010", "بانک و کارتخوان", AccountType.Asset),
                ("1100", "حساب‌های دریافتنی", AccountType.Asset),
                ("1200", "موجودی کالا", AccountType.Asset),
                ("2000", "حساب‌های پرداختنی", AccountType.Liability),
                ("3000", "سرمایه مالک", AccountType.Equity),
                ("4000", "درآمد فروش", AccountType.Revenue),
                ("5000", "بهای تمام‌شده کالای فروش‌رفته", AccountType.Expense),
                ("6000", "هزینه‌های عملیاتی", AccountType.Expense)
            };

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var existingCodes = await db.Accounts.Where(x => x.TenantId == tenantId)
                    .Select(x => x.Code).ToListAsync(cancellationToken);
                var missing = defaults.Where(item => !existingCodes.Contains(item.Code))
                    .Select(item => new Account(tenantId, item.Code, item.Name, item.Type)).ToArray();
                if (missing.Length > 0)
                    db.Accounts.AddRange(missing);

                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);

                var accounts = await db.Accounts.AsNoTracking()
                    .Where(x => x.TenantId == tenantId && x.IsActive)
                    .OrderBy(x => x.Code)
                    .Select(x => new AccountResponse(x.Id, x.Code, x.Name, x.Type.ToString(), x.IsActive))
                    .ToListAsync(cancellationToken);
                return Results.Ok(accounts);
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "ایجاد سرفصل‌های پایه با تغییر هم‌زمان دیگری تداخل پیدا کرد؛ دوباره تلاش کنید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

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
            if (request.Lines is null || request.Lines.Count < 2 || request.Lines.Any(line => line is null))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["سند باید حداقل دو ردیف معتبر داشته باشد و ردیف خالی مجاز نیست."] });

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
            try
            {
                // EF Core persists the entry and all lines in one SaveChanges transaction.
                // Convert provider/concurrency failures into a safe retryable response;
                // never expose database details or report a partially posted journal.
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateConcurrencyException)
            {
                return Results.Conflict(new
                {
                    code = "journal_entry_concurrency_conflict",
                    message = "اطلاعات حساب‌ها هم‌زمان تغییر کرده است؛ سند را تازه‌سازی و دوباره ثبت کنید."
                });
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new
                {
                    code = "journal_entry_persistence_conflict",
                    message = "ثبت سند به دلیل تعارض یا محدودیت اطلاعات پایگاه داده انجام نشد."
                });
            }
            catch (DbException)
            {
                return Results.Conflict(new
                {
                    code = "journal_entry_provider_conflict",
                    message = "پایگاه داده نتوانست سند را با اطمینان ثبت کند؛ وضعیت سند را بررسی کنید."
                });
            }

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
            var entries = await db.JournalEntryLines.AsNoTracking()
                .Where(line => line.TenantId == tenant.TenantId)
                .Join(db.JournalEntries.AsNoTracking().Where(entry =>
                        entry.TenantId == tenant.TenantId
                        && (tenant.BranchId == null || entry.BranchId == tenant.BranchId)),
                    line => new { line.TenantId, line.JournalEntryId },
                    entry => new { entry.TenantId, JournalEntryId = entry.Id },
                    (line, entry) => line)
                .Join(db.Accounts.AsNoTracking().Where(account => account.TenantId == tenant.TenantId),
                    line => new { line.TenantId, line.AccountId },
                    account => new { account.TenantId, AccountId = account.Id },
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
