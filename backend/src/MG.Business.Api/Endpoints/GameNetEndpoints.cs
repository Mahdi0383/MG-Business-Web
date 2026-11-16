using System.Data;
using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class GameNetEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Cashier", "GameOperator", "Accountant", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "GameOperator", "Cashier"];

    public static IEndpointRouteBuilder MapGameNetEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/gamenet").RequireAuthorization("TenantMember");

        group.MapGet("/devices", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var devices = await db.GameDevices.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.IsActive
                    && (tenant.BranchId == null || x.BranchId == tenant.BranchId))
                .OrderBy(x => x.Code)
                .Select(x => new DeviceResponse(x.Id, x.Code, x.Name, x.HourlyRate,
                    x.BillingIntervalMinutes, x.BranchId, db.GameSessions.Any(session =>
                        session.TenantId == x.TenantId && session.DeviceId == x.Id && session.Status == GameSessionStatus.Active)))
                .ToListAsync(cancellationToken);
            return Results.Ok(devices);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/devices", async (CreateDeviceRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });

            var serviceProduct = await db.Products.SingleOrDefaultAsync(x =>
                x.TenantId == tenantId && x.Id == request.ServiceProductId && x.IsActive && !x.TracksInventory, cancellationToken);
            if (serviceProduct is null)
                return Results.BadRequest(new { message = "دستگاه باید به یک کالای خدماتی بدون انبار متصل شود." });

            GameDevice device;
            try
            {
                device = new GameDevice(tenantId, branchId, serviceProduct.Id, request.Code, request.Name,
                    request.HourlyRate, request.BillingIntervalMinutes);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["device"] = [exception.Message] });
            }

            db.GameDevices.Add(device);
            try { await db.SaveChangesAsync(cancellationToken); }
            catch (DbUpdateException) { return Results.Conflict(new { message = "کد دستگاه در این شعبه تکراری است." }); }

            return Results.Created($"/api/gamenet/devices/{device.Id}",
                new DeviceResponse(device.Id, device.Code, device.Name, device.HourlyRate, device.BillingIntervalMinutes, device.BranchId, false));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/sessions/active", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var sessions = await db.GameSessions.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.Status == GameSessionStatus.Active
                    && (tenant.BranchId == null || x.BranchId == tenant.BranchId))
                .Join(db.GameDevices.AsNoTracking(), session => session.DeviceId, device => device.Id,
                    (session, device) => new ActiveSessionResponse(session.Id, session.DeviceId, device.Name,
                        session.CustomerId, session.StartedAtUtc, session.HourlyRate, session.BillingIntervalMinutes))
                .ToListAsync(cancellationToken);
            return Results.Ok(sessions.OrderBy(x => x.StartedAtUtc));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapGet("/settlement-accounts", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var accounts = await db.Accounts.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.IsActive
                    && (x.Type == AccountType.Asset || x.Type == AccountType.Revenue))
                .OrderBy(x => x.Code)
                .Select(x => new SettlementAccountResponse(x.Id, x.Code, x.Name, x.Type.ToString()))
                .ToListAsync(cancellationToken);
            return Results.Ok(accounts);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/sessions/{sessionId:guid}/quote", async (Guid sessionId, ITenantContext tenant,
            BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var session = await db.GameSessions.AsNoTracking().SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == sessionId && x.Status == GameSessionStatus.Active
                && (tenant.BranchId == null || x.BranchId == tenant.BranchId), cancellationToken);
            if (session is null) return Results.NotFound();

            try
            {
                var quotedAt = DateTimeOffset.UtcNow;
                var quote = session.CalculateCharge(quotedAt);
                return Results.Ok(new SessionQuoteResponse(session.Id, session.CustomerId, quotedAt,
                    quote.BilledMinutes, quote.TotalAmount));
            }
            catch (InvalidOperationException exception)
            {
                return Results.Conflict(new { message = exception.Message });
            }
            catch (ArgumentOutOfRangeException exception)
            {
                return Results.Conflict(new { message = exception.Message });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapPost("/sessions", async (StartSessionRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            var device = await db.GameDevices.SingleOrDefaultAsync(x =>
                x.TenantId == tenantId && x.BranchId == branchId && x.Id == request.DeviceId && x.IsActive, cancellationToken);
            if (device is null)
                return Results.NotFound(new { message = "دستگاه فعال پیدا نشد." });

            if (request.CustomerId is Guid customerId &&
                !await db.Customers.AnyAsync(x => x.TenantId == tenantId && x.Id == customerId && x.IsActive, cancellationToken))
                return Results.BadRequest(new { message = "مشتری معتبر نیست." });

            if (await db.GameSessions.AnyAsync(x =>
                x.TenantId == tenantId && x.DeviceId == device.Id && x.Status == GameSessionStatus.Active, cancellationToken))
                return Results.Conflict(new { message = "این دستگاه در حال حاضر جلسه فعال دارد." });

            var session = new GameSession(tenantId, branchId, device.Id, request.CustomerId,
                device.HourlyRate, device.BillingIntervalMinutes);
            db.GameSessions.Add(session);
            try { await db.SaveChangesAsync(cancellationToken); }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "شروع جلسه به دلیل هم‌زمانی یا تعارض داده‌ها انجام نشد." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان پایگاه داده رخ داد؛ وضعیت دستگاه را بررسی و دوباره تلاش کنید." });
            }

            try
            {
                await transaction.CommitAsync(cancellationToken);
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تأیید نهایی شروع جلسه به دلیل تعارض پایگاه داده انجام نشد؛ وضعیت دستگاه را بررسی کنید." });
            }

            return Results.Created($"/api/gamenet/sessions/{session.Id}",
                new { session.Id, session.DeviceId, session.CustomerId, session.StartedAtUtc, session.HourlyRate, session.BillingIntervalMinutes });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapPost("/sessions/{sessionId:guid}/close", async (Guid sessionId, CloseSessionRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });
            if (!Enum.IsDefined(request.PaymentMethod)
                || (request.AmountPaid > 0m && request.PaymentMethod == PaymentMethod.OnAccount)
                || (request.AmountPaid == 0m && request.PaymentMethod != PaymentMethod.OnAccount))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش پرداخت با مبلغ پرداختی سازگار نیست."] });
            if (request.AmountPaid < 0m || decimal.Round(request.AmountPaid, 2) != request.AmountPaid)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["amountPaid"] = ["مبلغ پرداخت باید غیرمنفی و حداکثر دارای دو رقم اعشار باشد."] });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var session = await db.GameSessions.SingleOrDefaultAsync(x =>
                    x.TenantId == tenantId && x.BranchId == branchId && x.Id == sessionId
                    && x.Status == GameSessionStatus.Active, cancellationToken);
                if (session is null) return Results.NotFound();

                var device = await db.GameDevices.SingleAsync(x =>
                    x.TenantId == tenantId && x.Id == session.DeviceId, cancellationToken);
                var serviceProduct = await db.Products.SingleAsync(x =>
                    x.TenantId == tenantId && x.Id == device.ServiceProductId && !x.TracksInventory, cancellationToken);
                var revenue = await FindAccount(db, tenantId, request.RevenueAccountId, AccountType.Revenue, cancellationToken);
                if (revenue is null) return Results.BadRequest(new { message = "حساب درآمد معتبر نیست." });

                session.Close(DateTimeOffset.UtcNow);
                var number = $"G-{DateTime.UtcNow:yyyyMMdd}-{Guid.NewGuid():N}"[..19].ToUpperInvariant();
                var line = new SalesOrderLine(tenantId, serviceProduct.Id, serviceProduct.Name, serviceProduct.Sku,
                    1m, session.TotalAmount!.Value, 0m, 0m);
                var order = new SalesOrder(tenantId, branchId, number, session.CustomerId, [line], 0m);
                order.SetPaidAmount(request.AmountPaid);
                if (order.BalanceDue > 0m && session.CustomerId is null)
                    return Results.BadRequest(new { message = "برای تسویه ناقص یا نسیه سانس، باید هنگام شروع سانس مشتری انتخاب شده باشد." });
                if (order.BalanceDue > 0m &&
                    await FindAccount(db, tenantId, request.ReceivableAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافتنی معتبر نیست." });
                if (order.PaidAmount > 0m &&
                    await FindAccount(db, tenantId, request.PaymentAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافت وجه معتبر نیست." });

                var journalLines = new List<JournalEntryLine>();
                if (order.PaidAmount > 0m)
                    journalLines.Add(new JournalEntryLine(tenantId, request.PaymentAccountId!.Value, order.PaidAmount, 0m, "Game session receipt"));
                if (order.BalanceDue > 0m)
                    journalLines.Add(new JournalEntryLine(tenantId, request.ReceivableAccountId!.Value, order.BalanceDue, 0m, "Game session receivable"));
                journalLines.Add(new JournalEntryLine(tenantId, revenue.Id, 0m, order.Total, "Game session revenue"));

                db.SalesOrders.Add(order);
                db.JournalEntries.Add(new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                    $"Game session {number}", journalLines, number));
                if (order.PaidAmount > 0m)
                    db.SalesPayments.Add(new SalesPayment(tenantId, branchId, order.Id, order.PaidAmount,
                        request.PaymentMethod, request.PaymentReference));

                session.LinkSalesOrder(order.Id);
                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);

                return Results.Ok(new
                {
                    session.Id,
                    session.DeviceId,
                    session.StartedAtUtc,
                    session.EndedAtUtc,
                    session.BilledMinutes,
                    session.HourlyRate,
                    session.TotalAmount,
                    salesOrderId = order.Id,
                    order.PaidAmount,
                    order.BalanceDue
                });
            }
            catch (ArgumentException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["session"] = [exception.Message] });
            }
            catch (InvalidOperationException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = exception.Message });
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "بستن جلسه به دلیل تعارض داده‌ها انجام نشد." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان رخ داد؛ عملیات را دوباره انجام دهید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    private static Task<Account?> FindAccount(BusinessDbContext db, Guid tenantId, Guid? accountId,
        AccountType type, CancellationToken cancellationToken) =>
        accountId is Guid id
            ? db.Accounts.SingleOrDefaultAsync(x => x.TenantId == tenantId && x.Id == id && x.Type == type && x.IsActive, cancellationToken)
            : Task.FromResult<Account?>(null);

    public sealed record CreateDeviceRequest(Guid ServiceProductId, string Code, string Name, decimal HourlyRate, int BillingIntervalMinutes = 15);
    public sealed record StartSessionRequest(Guid DeviceId, Guid? CustomerId);
    public sealed record CloseSessionRequest(decimal AmountPaid, PaymentMethod PaymentMethod, Guid RevenueAccountId,
        Guid? PaymentAccountId, Guid? ReceivableAccountId, string? PaymentReference);
    public sealed record SettlementAccountResponse(Guid Id, string Code, string Name, string Type);
    public sealed record SessionQuoteResponse(Guid SessionId, Guid? CustomerId, DateTimeOffset QuotedAtUtc,
        int BilledMinutes, decimal TotalAmount);
    public sealed record DeviceResponse(Guid Id, string Code, string Name, decimal HourlyRate, int BillingIntervalMinutes, Guid BranchId, bool HasActiveSession);
    public sealed record ActiveSessionResponse(Guid Id, Guid DeviceId, string DeviceName, Guid? CustomerId,
        DateTimeOffset StartedAtUtc, decimal HourlyRate, int BillingIntervalMinutes);
}
