using System.Data;
using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class SalesEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "Cashier"];

    public static IEndpointRouteBuilder MapSalesEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/sales").RequireAuthorization("TenantMember");

        group.MapGet("/orders", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var query = db.SalesOrders.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId
                    && (tenant.BranchId == null || x.BranchId == tenant.BranchId));
            var orderRows = db.Database.IsSqlite()
                ? (await query.ToListAsync(cancellationToken)).OrderByDescending(x => x.CreatedAtUtc).Take(100).ToList()
                : await query.OrderByDescending(x => x.CreatedAtUtc).Take(100).ToListAsync(cancellationToken);
            var orders = orderRows.Select(x => new SalesOrderResponse(x.Id, x.Number, x.CustomerId, x.Subtotal,
                x.LineDiscountTotal, x.Discount, x.Total, x.PaidAmount, x.BalanceDue, x.Status.ToString(), x.CreatedAtUtc));
            return Results.Ok(orders);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/orders", async (CreateSaleRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا کسب‌وکار و شعبه فعال را انتخاب کنید." });
            if (request.Lines is null || request.Lines.Count == 0 || request.Lines.Any(line => line is null))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["فاکتور باید حداقل یک قلم معتبر داشته باشد و ردیف خالی مجاز نیست."] });
            if (!Enum.IsDefined(request.PaymentMethod))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش پرداخت معتبر نیست."] });
            if ((request.AmountPaid > 0m && request.PaymentMethod == PaymentMethod.OnAccount)
                || (request.AmountPaid == 0m && request.PaymentMethod != PaymentMethod.OnAccount))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش پرداخت با مبلغ پرداختی سازگار نیست."] });
            if (request.AmountPaid < 0m || decimal.Round(request.AmountPaid, 2) != request.AmountPaid)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["amountPaid"] = ["مبلغ پرداخت معتبر نیست."] });
            if (request.Discount < 0m || decimal.Round(request.Discount, 2) != request.Discount)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["discount"] = ["تخفیف فاکتور باید غیرمنفی و حداکثر تا دو رقم اعشار باشد."] });
            if (request.Lines.Any(line => line.Quantity <= 0m || decimal.Round(line.Quantity, 3) != line.Quantity))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["تعداد هر قلم باید مثبت و حداکثر تا سه رقم اعشار باشد."] });
            if (request.Lines.Any(line => line.UnitPrice is decimal price && (price < 0m || decimal.Round(price, 2) != price)))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["قیمت واحد باید غیرمنفی و حداکثر تا دو رقم اعشار باشد."] });
            if (request.Lines.Any(line => line.Discount < 0m || decimal.Round(line.Discount, 2) != line.Discount))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["تخفیف هر قلم باید غیرمنفی و حداکثر تا دو رقم اعشار باشد."] });
            if (request.PaymentReference?.Length > 100)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentReference"] = ["شناسه پرداخت نباید بیش از ۱۰۰ نویسه باشد."] });

            if (request.CustomerId is Guid customerId &&
                !await db.Customers.AnyAsync(x => x.TenantId == tenantId && x.Id == customerId && x.IsActive, cancellationToken))
                return Results.BadRequest(new { message = "مشتری معتبر نیست یا به این کسب‌وکار تعلق ندارد." });

            var productIds = request.Lines.Select(x => x.ProductId).Distinct().ToArray();
            var products = await db.Products.AsNoTracking()
                .Where(x => x.TenantId == tenantId && x.IsActive && productIds.Contains(x.Id))
                .ToDictionaryAsync(x => x.Id, cancellationToken);
            if (products.Count != productIds.Length)
                return Results.BadRequest(new { message = "یک یا چند کالا نامعتبر یا متعلق به کسب‌وکار دیگری است." });

            var revenueAccount = await FindAccount(db, tenantId, request.RevenueAccountId, AccountType.Revenue, cancellationToken);
            if (revenueAccount is null)
                return Results.BadRequest(new { message = "حساب درآمد معتبر نیست." });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var valuations = new Dictionary<Guid, InventoryValuation>();
                foreach (var lineGroup in request.Lines.Where(x => products[x.ProductId].TracksInventory).GroupBy(x => x.ProductId))
                {
                    var history = await db.StockMovements.AsNoTracking()
                        .Where(x => x.TenantId == tenantId && x.BranchId == branchId && x.ProductId == lineGroup.Key)
                        .ToListAsync(cancellationToken);
                    var valuation = InventoryValuation.Calculate(history);
                    var requestedQuantity = lineGroup.Sum(x => x.Quantity);
                    if (requestedQuantity <= 0m || requestedQuantity > valuation.QuantityOnHand)
                    {
                        await transaction.RollbackAsync(cancellationToken);
                        return Results.Conflict(new { message = $"موجودی کالای {products[lineGroup.Key].Name} کافی نیست.", available = valuation.QuantityOnHand });
                    }
                    valuations[lineGroup.Key] = valuation;
                }

                var orderLines = request.Lines.Select(line =>
                {
                    var product = products[line.ProductId];
                    var unitCost = product.TracksInventory ? valuations[line.ProductId].AverageUnitCost : 0m;
                    var unitPrice = line.UnitPrice ?? product.DefaultSalePrice;
                    return new SalesOrderLine(tenantId, product.Id, product.Name, product.Sku,
                        line.Quantity, unitPrice, line.Discount, unitCost);
                }).ToList();

                var orderNumber = $"S-{DateTime.UtcNow:yyyyMMdd}-{Guid.NewGuid():N}"[..19].ToUpperInvariant();
                var order = new SalesOrder(tenantId, branchId, orderNumber, request.CustomerId, orderLines, request.Discount);
                if (request.AmountPaid > order.Total)
                    return Results.ValidationProblem(new Dictionary<string, string[]> { ["amountPaid"] = ["مبلغ پرداختی نمی‌تواند از مبلغ نهایی فاکتور بیشتر باشد."] });
                if (request.AmountPaid < order.Total && request.CustomerId is null)
                    return Results.ValidationProblem(new Dictionary<string, string[]> { ["customerId"] = ["برای فروش نسیه یا تسویه ناقص، انتخاب مشتری الزامی است."] });
                order.SetPaidAmount(request.AmountPaid);

                var dueAccountId = request.ReceivableAccountId;
                if (order.BalanceDue > 0m &&
                    await FindAccount(db, tenantId, dueAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافتنی معتبر نیست." });

                if (order.PaidAmount > 0m &&
                    await FindAccount(db, tenantId, request.PaymentAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافت وجه معتبر نیست." });

                var totalCost = order.Lines.Sum(x => x.CostTotal);
                if (totalCost > 0m &&
                    (await FindAccount(db, tenantId, request.InventoryAccountId, AccountType.Asset, cancellationToken) is null
                    || await FindAccount(db, tenantId, request.CostOfGoodsSoldAccountId, AccountType.Expense, cancellationToken) is null))
                    return Results.BadRequest(new { message = "حساب موجودی کالا یا بهای تمام‌شده معتبر نیست." });

                db.SalesOrders.Add(order);

                foreach (var lineGroup in request.Lines.Where(x => products[x.ProductId].TracksInventory).GroupBy(x => x.ProductId))
                {
                    var valuation = valuations[lineGroup.Key];
                    var quantity = lineGroup.Sum(x => x.Quantity);
                    db.StockMovements.Add(new StockMovement(tenantId, branchId, lineGroup.Key,
                        StockMovementType.Sale, quantity, valuation.AverageUnitCost, order.Number));
                }

                var revenueLines = new List<JournalEntryLine>();
                if (order.PaidAmount > 0m)
                    revenueLines.Add(new JournalEntryLine(tenantId, request.PaymentAccountId!.Value, order.PaidAmount, 0m, "Sales receipt"));
                if (order.BalanceDue > 0m)
                    revenueLines.Add(new JournalEntryLine(tenantId, dueAccountId!.Value, order.BalanceDue, 0m, "Customer receivable"));
                revenueLines.Add(new JournalEntryLine(tenantId, revenueAccount.Id, 0m, order.Total, "Sales revenue"));

                var revenueEntry = new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                    $"Sales {order.Number}", revenueLines, order.Number);
                db.JournalEntries.Add(revenueEntry);

                if (totalCost > 0m)
                {
                    var costEntry = new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                        $"Cost of goods sold {order.Number}",
                        [
                            new JournalEntryLine(tenantId, request.CostOfGoodsSoldAccountId!.Value, totalCost, 0m, "Cost of goods sold"),
                            new JournalEntryLine(tenantId, request.InventoryAccountId!.Value, 0m, totalCost, "Inventory reduction")
                        ], order.Number);
                    db.JournalEntries.Add(costEntry);
                }

                if (order.PaidAmount > 0m)
                    db.SalesPayments.Add(new SalesPayment(tenantId, branchId, order.Id,
                        order.PaidAmount, request.PaymentMethod, request.PaymentReference));

                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);

                return Results.Created($"/api/sales/orders/{order.Id}", new
                {
                    order.Id,
                    order.Number,
                    order.Subtotal,
                    order.LineDiscountTotal,
                    order.Discount,
                    order.Total,
                    order.PaidAmount,
                    order.BalanceDue,
                    CostOfGoodsSold = totalCost,
                    GrossProfit = order.Total - totalCost,
                    status = order.Status.ToString(),
                    order.CreatedAtUtc
                });
            }
            catch (ArgumentException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["sale"] = [exception.Message] });
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "ثبت فروش به دلیل تعارض داده‌ها انجام نشد؛ موجودی و اطلاعات حساب‌ها را بررسی کنید." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان اطلاعات رخ داد؛ فروش را دوباره بررسی کنید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapPost("/orders/{orderId:guid}/payments", async (Guid orderId, RecordPaymentRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا کسب‌وکار و شعبه فعال را انتخاب کنید." });
            if (request.Amount <= 0m || decimal.Round(request.Amount, 2) != request.Amount)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["amount"] = ["مبلغ دریافت باید مثبت و حداکثر تا دو رقم اعشار باشد."] });
            if (!Enum.IsDefined(request.PaymentMethod) || request.PaymentMethod == PaymentMethod.OnAccount)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش دریافت وجه معتبر نیست."] });
            if (request.PaymentReference?.Length > 100)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentReference"] = ["شناسه پرداخت نباید بیش از ۱۰۰ نویسه باشد."] });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var order = await db.SalesOrders.SingleOrDefaultAsync(x =>
                    x.TenantId == tenantId && x.BranchId == branchId && x.Id == orderId, cancellationToken);
                if (order is null)
                    return Results.NotFound(new { message = "فاکتور در شعبه فعال پیدا نشد." });
                if (request.Amount > order.BalanceDue)
                    return Results.Conflict(new { message = "مبلغ دریافت از مانده فاکتور بیشتر است.", balanceDue = order.BalanceDue });

                var paymentAccount = await FindAccount(db, tenantId, request.PaymentAccountId, AccountType.Asset, cancellationToken);
                if (paymentAccount is null)
                    return Results.BadRequest(new { message = "حساب دریافت وجه معتبر نیست." });
                var receivableAccount = await FindAccount(db, tenantId, request.ReceivableAccountId, AccountType.Asset, cancellationToken);
                if (receivableAccount is null)
                    return Results.BadRequest(new { message = "حساب دریافتنی معتبر نیست." });

                order.SetPaidAmount(order.PaidAmount + request.Amount);
                db.SalesPayments.Add(new SalesPayment(tenantId, branchId, order.Id,
                    request.Amount, request.PaymentMethod, request.PaymentReference));
                db.JournalEntries.Add(new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                    $"Payment for {order.Number}",
                    [
                        new JournalEntryLine(tenantId, paymentAccount.Id, request.Amount, 0m, "Customer payment received"),
                        new JournalEntryLine(tenantId, receivableAccount.Id, 0m, request.Amount, "Customer receivable settled")
                    ], request.PaymentReference));

                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);
                return Results.Ok(new { order.Id, order.Number, order.Total, order.PaidAmount, order.BalanceDue });
            }
            catch (ArgumentException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["payment"] = [exception.Message] });
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "ثبت دریافت با تعارض داده‌ها انجام نشد؛ مانده فاکتور را دوباره بررسی کنید." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان اطلاعات رخ داد؛ مانده فاکتور را دوباره بررسی کنید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    private static Task<Account?> FindAccount(BusinessDbContext db, Guid tenantId, Guid? accountId,
        AccountType type, CancellationToken cancellationToken)
    {
        if (accountId is not Guid id)
            return Task.FromResult<Account?>(null);

        return db.Accounts.SingleOrDefaultAsync(x =>
            x.TenantId == tenantId && x.Id == id && x.Type == type && x.IsActive, cancellationToken);
    }

    public sealed record RecordPaymentRequest(decimal Amount, PaymentMethod PaymentMethod, Guid PaymentAccountId, Guid ReceivableAccountId, string? PaymentReference);
    public sealed record CreateSaleRequest(Guid? CustomerId, decimal Discount, decimal AmountPaid,
        PaymentMethod PaymentMethod, Guid RevenueAccountId, Guid? PaymentAccountId, Guid? ReceivableAccountId,
        Guid? InventoryAccountId, Guid? CostOfGoodsSoldAccountId, string? PaymentReference, List<SaleLineRequest> Lines);
    public sealed record SaleLineRequest(Guid ProductId, decimal Quantity, decimal? UnitPrice, decimal Discount);
    public sealed record SalesOrderResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal,
        decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue,
        string Status, DateTimeOffset CreatedAtUtc);
}
